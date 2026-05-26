"""Grid search over strategy parameter combinations.

Builds Config objects on the fly from a parameter dict, runs backtest, captures
metrics, returns a DataFrame with one row per combination. Persists results
to data/cache for incremental re-runs.
"""
from __future__ import annotations

import copy
import itertools
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

from src.config import (
    AccountConfig,
    Config,
    EntryConfig,
    ExecutionConfig,
    ExitConfig,
    HyperliquidConfig,
    NotificationsConfig,
    RiskConfig,
    SchedulerConfig,
    SizingConfig,
    UniverseConfig,
)
from .engine import BacktestConfig, backtest, trades_to_dataframe
from .metrics import compute_metrics, metrics_to_dict

log = logging.getLogger(__name__)


# ─── Default grid (override via API) ──────────────────────────────────────


DEFAULT_GRID: Dict[str, List] = {
    "min_funding_apr_pct": [15, 25, 40, 60, 100],
    "min_funding_zscore": [0.5, 1.0, 2.0, 3.0],
    "persistence_hours": [1, 3],
    # TP > SL pairs (let winners run, cut losers fast) — invalid pairs filtered out
    "take_profit_pct": [8, 12, 15, 20, 25, 30],
    "stop_loss_pct": [4, 6, 8, 10],
    "timeout_hours": [48, 168],
    "funding_apr_exit_threshold_ratio": [0.3, 0.5],
}


# ─── Config builder ──────────────────────────────────────────────────────


def _base_config() -> Config:
    """Build a minimal Config with safe defaults (independent of any YAML)."""
    return Config(
        account=AccountConfig(
            capital_usdc=10000,
            max_concurrent_positions=4,
            max_total_exposure_pct=30,
            max_position_pct=8,
        ),
        universe=UniverseConfig(
            min_open_interest_usd=1_000_000,    # widened: include midcaps + smallcaps
            max_spread_bps=15,                  # widened: smallcaps have wider spreads
            exclude_coins=[],
            include_only=[],
        ),
        entry=EntryConfig(
            min_funding_apr_pct=50,
            persistence_hours=3,
            min_funding_zscore=2.0,
            zscore_lookback_days=30,
            max_premium_bps=200,
            direction_mode="short_high_funding",
        ),
        sizing=SizingConfig(
            leverage_majors=3,
            leverage_midcaps=2,
            majors_list=["BTC", "ETH", "SOL"],
            method="equal",
        ),
        exit=ExitConfig(
            funding_apr_exit_threshold=15,
            take_profit_pct=8,
            stop_loss_pct=10,
            timeout_hours=168,
            exit_on_zscore_below=0.5,
            reentry_cooldown_hours=6,
            post_stop_size_multiplier=0.5,
        ),
        risk=RiskConfig(
            daily_loss_halt_pct=3,
            total_drawdown_kill_pct=15,
            margin_ratio_warning=0.40,
            margin_ratio_critical=0.25,
            max_pct_of_coin_oi=1.0,
        ),
        execution=ExecutionConfig(
            observe_only=False,
            dry_run=True,        # irrelevant in backtest
            order_type="market",
            slippage_tolerance=0.005,
            limit_timeout_seconds=30,
            retry_attempts=3,
            retry_delay_seconds=5,
            use_cross_margin=False,
            use_native_triggers=True,
            tls_verify=True,
        ),
        scheduler=SchedulerConfig(
            tick_interval_seconds=3600,
            tick_offset_seconds_before_hour=60,
        ),
        hyperliquid=HyperliquidConfig(
            network="mainnet",
            wallet_address="",
        ),
        notifications=NotificationsConfig(),
    )


def _build_config(params: Dict) -> Config:
    cfg = _base_config()
    e = cfg.entry
    x = cfg.exit
    e.min_funding_apr_pct = params["min_funding_apr_pct"]
    e.min_funding_zscore = params["min_funding_zscore"]
    e.persistence_hours = params["persistence_hours"]
    x.take_profit_pct = params["take_profit_pct"]
    x.stop_loss_pct = params["stop_loss_pct"]
    x.timeout_hours = params["timeout_hours"]
    # Exit funding threshold is a ratio of entry threshold to keep them coherent
    x.funding_apr_exit_threshold = max(
        1, params["min_funding_apr_pct"] * params["funding_apr_exit_threshold_ratio"]
    )
    # Pydantic cross-check: entry > exit must hold
    if e.min_funding_apr_pct <= x.funding_apr_exit_threshold:
        x.funding_apr_exit_threshold = e.min_funding_apr_pct - 1
    return cfg


# ─── Grid iteration ──────────────────────────────────────────────────────


def iter_combinations(grid: Dict[str, List]) -> Iterable[Dict]:
    """Cartesian product of all grid axes, filtered to enforce TP > SL.

    Constraint: we only test configurations where take_profit_pct > stop_loss_pct
    (asymmetric "let winners run" pattern for short funding harvest).
    """
    keys = list(grid.keys())
    for combo in itertools.product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        tp = params.get("take_profit_pct")
        sl = params.get("stop_loss_pct")
        if tp is not None and sl is not None and tp <= sl:
            continue
        yield params


# ─── Sweep entry point ───────────────────────────────────────────────────


def run_sweep(
    data: pd.DataFrame,
    grid: Dict[str, List] = None,
    bt_cfg: BacktestConfig = None,
    progress_every: int = 10,
) -> pd.DataFrame:
    """Run the strategy for every combination in ``grid``. Returns one row per combo."""
    if grid is None:
        grid = DEFAULT_GRID
    if bt_cfg is None:
        bt_cfg = BacktestConfig()

    combos = list(iter_combinations(grid))
    log.info("Running sweep over %d parameter combinations...", len(combos))

    rows = []
    t_start = time.time()
    for i, params in enumerate(combos, 1):
        cfg = _build_config(params)
        trades = backtest(data, cfg, bt_cfg)
        trades_df = trades_to_dataframe(trades)
        metrics = compute_metrics(trades_df)
        row = {**params, **metrics_to_dict(metrics)}
        rows.append(row)
        if i % progress_every == 0 or i == len(combos):
            elapsed = time.time() - t_start
            log.info(
                "  [%d/%d] last: n=%d sharpe=%.2f pnl=$%.2f (%.1fs elapsed)",
                i, len(combos), metrics.n_trades, metrics.sharpe_annual,
                metrics.total_pnl_usd, elapsed,
            )

    return pd.DataFrame(rows)


def save_sweep(df: pd.DataFrame, path: str | Path = "data/cache/sweep_results.parquet") -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    log.info("Saved %d sweep rows to %s", len(df), path)
