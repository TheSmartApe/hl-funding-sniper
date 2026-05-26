"""Backtest simulation engine.

Replays the live bot's signal logic (src.signal_engine) on historical hourly
data. Detects SL/TP triggers using intra-hour hi/lo (mark_hi, mark_lo)
ingested from Dune, so we don't miss wicks.

Key design choices:
- Stateless per-position: each trade is fully encapsulated in a Trade record.
- Iterates by hour (the live bot's tick cadence). Processes ALL coins at each
  hour: first exits, then entries (ranked by score, top N picked under cap).
- Uses the same signal_engine functions as the live bot → strategy parity.
- Fees + slippage are configurable and applied per round-trip.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.config import Config
from src.data_client import MarketSnapshot
from src.signal_engine import (
    evaluate_entry,
    evaluate_exit,
    is_eligible,
    rank_entry_candidates,
    PositionState,
)

log = logging.getLogger(__name__)


# ─── Trade record ────────────────────────────────────────────────────────


@dataclass
class Trade:
    coin: str
    direction: str          # 'short' | 'long'
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str
    notional_usd: float
    leverage: int
    hours_held: int
    entry_funding_apr: float
    entry_zscore: float
    price_pnl_usd: float
    funding_pnl_usd: float
    fees_usd: float
    slippage_usd: float
    net_pnl_usd: float
    net_pnl_pct: float       # of notional


# ─── Internal position state ─────────────────────────────────────────────


@dataclass
class _OpenPosition:
    coin: str
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    notional_usd: float
    leverage: int
    entry_funding_apr: float
    entry_zscore: float
    funding_pnl_accrued: float = 0.0

    def to_position_state(self) -> PositionState:
        return PositionState(
            coin=self.coin,
            direction=self.direction,
            entry_price=self.entry_price,
            entry_timestamp_iso=self.entry_time.isoformat(),
            entry_funding_apr=self.entry_funding_apr,
        )


# ─── Snapshot builder ────────────────────────────────────────────────────


def _row_to_snapshot(row) -> MarketSnapshot:
    return MarketSnapshot(
        coin=row["coin"],
        funding_apr_pct=float(row["funding_apr"]),
        open_interest_usd=float(row["open_interest_usd"]),
        mark_px=float(row["mark_px"]),
        oracle_px=float(row["oracle_px"]) if not pd.isna(row["oracle_px"]) else float(row["mark_px"]),
        mid_px=float(row["mid_px"]) if not pd.isna(row["mid_px"]) else float(row["mark_px"]),
        premium_bps=float(row["premium_bps"]) if not pd.isna(row["premium_bps"]) else 0.0,
        spread_bps=float(row["spread_bps"]) if not pd.isna(row["spread_bps"]) else 0.0,
        day_ntl_volume_usd=float(row["day_vlm_usd"]) if not pd.isna(row["day_vlm_usd"]) else 0.0,
    )


# ─── Main backtest function ──────────────────────────────────────────────


@dataclass
class BacktestConfig:
    """Trading frictions for the backtest."""
    notional_per_trade_usd: float = 1000.0
    fee_pct_per_leg: float = 0.045   # HL taker, % (0.045 = 4.5 bps)
    slippage_pct_per_leg: float = 0.05   # estimated, % (5 bps)
    leverage_majors: int = 3
    leverage_midcaps: int = 2
    majors_list: Tuple[str, ...] = ("BTC", "ETH", "SOL")
    # tick cadence (hours) — should match cfg.scheduler. Default = 1 (hourly)
    tick_hours: int = 1


def backtest(
    data: pd.DataFrame,
    strategy_cfg: Config,
    bt_cfg: BacktestConfig = None,
) -> List[Trade]:
    """Run a single backtest over ``data`` with the given strategy config.

    Returns a list of completed Trades.
    """
    if bt_cfg is None:
        bt_cfg = BacktestConfig()

    if data.empty:
        return []

    data = data.sort_values(["hour", "coin"]).reset_index(drop=True)
    coins = data["coin"].unique().tolist()
    lookback_h = strategy_cfg.entry.zscore_lookback_days * 24

    # Rolling funding history per coin (in APR %)
    funding_hist: Dict[str, deque] = {
        c: deque(maxlen=lookback_h + 1) for c in coins
    }
    open_positions: Dict[str, _OpenPosition] = {}
    cooldown_until: Dict[str, pd.Timestamp] = {}
    last_stop_loss: Dict[str, bool] = defaultdict(lambda: False)
    trades: List[Trade] = []

    hours = data["hour"].unique()
    for hour in hours:
        group = data[data["hour"] == hour]

        # ── 1. Pass: update funding history for all coins (always) ───────
        for _, row in group.iterrows():
            funding_hist[row["coin"]].append(float(row["funding_apr"]))

        # ── 2. Manage open positions: exits ──────────────────────────────
        for _, row in group.iterrows():
            coin = row["coin"]
            if coin not in open_positions:
                continue
            pos = open_positions[coin]
            snap = _row_to_snapshot(row)

            # Accrue funding for this hour (always, even if we exit this hour)
            hourly_rate = snap.funding_apr_pct / (24 * 365 * 100)
            sign = -1 if pos.direction == "short" else 1
            pos.funding_pnl_accrued += sign * hourly_rate * pos.notional_usd

            # Compute SL/TP trigger prices
            sl_pct = strategy_cfg.exit.stop_loss_pct / 100
            tp_pct = strategy_cfg.exit.take_profit_pct / 100
            if pos.direction == "short":
                sl_px = pos.entry_price * (1 + sl_pct)
                tp_px = pos.entry_price * (1 - tp_pct)
                # Check wicks (intra-hour): SL fires if hi crossed, TP if lo crossed
                sl_hit = float(row["mark_hi"]) >= sl_px
                tp_hit = float(row["mark_lo"]) <= tp_px
            else:
                sl_px = pos.entry_price * (1 - sl_pct)
                tp_px = pos.entry_price * (1 + tp_pct)
                sl_hit = float(row["mark_lo"]) <= sl_px
                tp_hit = float(row["mark_hi"]) >= tp_px

            # Priority order matches live: SL is most urgent (price-based, sub-second)
            close_at: Optional[Tuple[float, str]] = None
            if sl_hit and tp_hit:
                # Both touched within the same hour — assume SL first (worst case)
                close_at = (sl_px, "hl_sl_triggered")
            elif sl_hit:
                close_at = (sl_px, "hl_sl_triggered")
            elif tp_hit:
                close_at = (tp_px, "hl_tp_triggered")
            else:
                # Check signal-based exits
                history_list = list(funding_hist[coin])[:-1]  # exclude current
                decision = evaluate_exit(
                    pos.to_position_state(), snap, history_list, strategy_cfg,
                )
                if decision.exit:
                    close_at = (snap.mark_px, decision.reason)

            if close_at is not None:
                close_px, reason = close_at
                _close_position(
                    pos, hour, close_px, reason, bt_cfg, trades,
                )
                del open_positions[coin]
                if reason in ("hl_sl_triggered", "stop_loss"):
                    last_stop_loss[coin] = True
                    cooldown_until[coin] = hour + pd.Timedelta(
                        hours=strategy_cfg.exit.reentry_cooldown_hours,
                    )
                else:
                    last_stop_loss[coin] = False
                    cooldown_until[coin] = hour + pd.Timedelta(
                        hours=strategy_cfg.exit.reentry_cooldown_hours,
                    )

        # ── 3. Scan entries: collect candidates ──────────────────────────
        max_concurrent = strategy_cfg.account.max_concurrent_positions
        free_slots = max_concurrent - len(open_positions)
        if free_slots <= 0:
            continue

        candidates = []
        for _, row in group.iterrows():
            coin = row["coin"]
            if coin in open_positions:
                continue
            if coin in cooldown_until and hour < cooldown_until[coin]:
                continue
            snap = _row_to_snapshot(row)
            if not is_eligible(snap, strategy_cfg):
                continue
            history_list = list(funding_hist[coin])[:-1]
            decision = evaluate_entry(snap, history_list, strategy_cfg)
            if decision.enter:
                candidates.append((decision, snap))

        # Rank and open top N
        candidates.sort(key=lambda x: x[0].score, reverse=True)
        for decision, snap in candidates[:free_slots]:
            multiplier = 0.5 if last_stop_loss[snap.coin] else 1.0
            notional = bt_cfg.notional_per_trade_usd * multiplier
            leverage = (
                bt_cfg.leverage_majors
                if snap.coin in bt_cfg.majors_list
                else bt_cfg.leverage_midcaps
            )
            open_positions[snap.coin] = _OpenPosition(
                coin=snap.coin,
                direction="short",
                entry_time=hour,
                entry_price=snap.mark_px,
                notional_usd=notional,
                leverage=leverage,
                entry_funding_apr=decision.funding_apr,
                entry_zscore=decision.zscore,
            )

    # ── End: force-close remaining open positions at last available price ──
    last_hour = data["hour"].max()
    last_group = data[data["hour"] == last_hour]
    for coin, pos in list(open_positions.items()):
        row = last_group[last_group["coin"] == coin]
        if row.empty:
            continue
        close_px = float(row.iloc[0]["mark_px"])
        _close_position(pos, last_hour, close_px, "backtest_end", bt_cfg, trades)
        del open_positions[coin]

    return trades


# ─── Close position helper ───────────────────────────────────────────────


def _close_position(
    pos: _OpenPosition,
    exit_time: pd.Timestamp,
    exit_price: float,
    exit_reason: str,
    bt_cfg: BacktestConfig,
    trades: List[Trade],
) -> None:
    sign = -1 if pos.direction == "short" else 1
    price_pnl = (
        sign * (exit_price - pos.entry_price) / pos.entry_price * pos.notional_usd
    )
    fees = pos.notional_usd * (bt_cfg.fee_pct_per_leg / 100) * 2
    slippage = pos.notional_usd * (bt_cfg.slippage_pct_per_leg / 100) * 2
    net = price_pnl + pos.funding_pnl_accrued - fees - slippage
    hours_held = max(0, int((exit_time - pos.entry_time).total_seconds() // 3600))
    trades.append(Trade(
        coin=pos.coin,
        direction=pos.direction,
        entry_time=pos.entry_time,
        entry_price=pos.entry_price,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=exit_reason,
        notional_usd=pos.notional_usd,
        leverage=pos.leverage,
        hours_held=hours_held,
        entry_funding_apr=pos.entry_funding_apr,
        entry_zscore=pos.entry_zscore,
        price_pnl_usd=price_pnl,
        funding_pnl_usd=pos.funding_pnl_accrued,
        fees_usd=fees,
        slippage_usd=slippage,
        net_pnl_usd=net,
        net_pnl_pct=net / pos.notional_usd * 100,
    ))


# ─── Convenience: DataFrame conversion ──────────────────────────────────


def trades_to_dataframe(trades: List[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
