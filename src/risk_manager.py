"""Risk overlay.

Sits between the signal engine and the executor. Vetoes trades that would
breach any portfolio-level constraint. Also computes available capital and
sizing for new entries.

Hard kill switches (drawdown, daily loss) are enforced here — once tripped,
``allow_new_entries()`` returns False until manual reset.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .data_client import HLDataClient, MarketSnapshot
from .position_manager import PositionManager

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Result type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SizingDecision:
    can_enter: bool
    notional_usd: float
    leverage: int
    reason: str


# ─────────────────────────────────────────────────────────────────────────────
#  Risk manager
# ─────────────────────────────────────────────────────────────────────────────


class RiskManager:
    def __init__(
        self,
        cfg: Config,
        data: HLDataClient,
        positions: PositionManager,
    ):
        self.cfg = cfg
        self.data = data
        self.positions = positions
        self._halted = False
        self._halt_reason: Optional[str] = None
        self._peak_capital: Optional[float] = None

    # ── account state ─────────────────────────────────────────────────────

    def current_capital_usd(self) -> float:
        """Real account equity. Falls back to configured baseline in dry-run."""
        if self.cfg.execution.dry_run or not self.cfg.hyperliquid.wallet_address:
            # In dry-run we approximate equity as baseline + cumulative PnL
            return self.cfg.account.capital_usdc + self.positions.total_realized_pnl()
        try:
            return self.data.get_account_value_usd(self.cfg.hyperliquid.wallet_address)
        except Exception as e:
            log.error("current_capital_usd live fetch failed: %s", e)
            return self.cfg.account.capital_usdc

    def total_open_notional_usd(self) -> float:
        return sum(p["size_usd"] for p in self.positions.list_open())

    def open_position_count(self) -> int:
        return len(self.positions.list_open())

    # ── circuit breakers ──────────────────────────────────────────────────

    def check_circuit_breakers(self) -> None:
        """Update halt state. Called at the top of every tick."""
        cap = self.current_capital_usd()
        if self._peak_capital is None or cap > self._peak_capital:
            self._peak_capital = cap

        # Total drawdown kill
        if self._peak_capital and self._peak_capital > 0:
            dd_pct = (self._peak_capital - cap) / self._peak_capital * 100
            if dd_pct >= self.cfg.risk.total_drawdown_kill_pct:
                self._halt(
                    f"total drawdown {dd_pct:.1f}% >= "
                    f"kill {self.cfg.risk.total_drawdown_kill_pct}%"
                )
                return

        # Daily loss halt (only blocks new entries; doesn't force close)
        daily_pnl = self.positions.realized_pnl_since(hours=24)
        daily_loss_pct = -daily_pnl / cap * 100 if cap > 0 else 0
        if daily_loss_pct >= self.cfg.risk.daily_loss_halt_pct:
            self._halt(
                f"24h realized loss {daily_loss_pct:.1f}% >= "
                f"halt {self.cfg.risk.daily_loss_halt_pct}%"
            )

    def _halt(self, reason: str) -> None:
        if not self._halted:
            log.critical("CIRCUIT BREAKER TRIPPED: %s", reason)
        self._halted = True
        self._halt_reason = reason

    def allow_new_entries(self) -> bool:
        return not self._halted

    def reset_halt(self) -> None:
        """Manual reset — call after reviewing the situation."""
        if self._halted:
            log.warning("Halt manually reset (was: %s)", self._halt_reason)
        self._halted = False
        self._halt_reason = None

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    # ── sizing ────────────────────────────────────────────────────────────

    def size_new_entry(
        self,
        snap: MarketSnapshot,
        score: float,
        post_stop_multiplier: float = 1.0,
    ) -> SizingDecision:
        """Decide notional + leverage for a candidate entry. Vetoes if any cap busted."""
        if not self.allow_new_entries():
            return SizingDecision(False, 0, 1, f"halted: {self._halt_reason}")

        n_open = self.open_position_count()
        if n_open >= self.cfg.account.max_concurrent_positions:
            return SizingDecision(False, 0, 1,
                                  f"max_concurrent_positions reached ({n_open})")

        cap = self.current_capital_usd()
        max_total = cap * (self.cfg.account.max_total_exposure_pct / 100)
        max_per_pos = cap * (self.cfg.account.max_position_pct / 100)
        in_use = self.total_open_notional_usd()
        remaining_budget = max(0.0, max_total - in_use)

        notional = min(max_per_pos, remaining_budget) * post_stop_multiplier
        if notional <= 0:
            return SizingDecision(False, 0, 1,
                                  "no budget remaining after caps")

        # OI concentration cap (don't become the market)
        max_by_oi = (
            snap.open_interest_usd * self.cfg.risk.max_pct_of_coin_oi / 100
        )
        if notional > max_by_oi:
            notional = max_by_oi
        if notional < 10:    # HL min notional ~$10
            return SizingDecision(False, 0, 1,
                                  f"notional ${notional:.2f} below HL minimum")

        # Leverage tier
        if snap.coin in self.cfg.sizing.majors_list:
            lev = self.cfg.sizing.leverage_majors
        else:
            lev = self.cfg.sizing.leverage_midcaps

        return SizingDecision(True, notional, lev,
                              f"sized ${notional:.2f} @ {lev}x")

    # ── cooldown check ────────────────────────────────────────────────────

    def in_cooldown(self, coin: str) -> bool:
        """True if coin was exited recently (within reentry_cooldown_hours)."""
        cooldown_h = self.cfg.exit.reentry_cooldown_hours
        if cooldown_h <= 0:
            return False
        last = self.positions.last_exit(coin)
        if last is None:
            return False
        try:
            t = datetime.fromisoformat(last["exit_timestamp"].replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - t).total_seconds() / 3600
            return hours_since < cooldown_h
        except (ValueError, AttributeError):
            return False

    def post_stop_multiplier_for(self, coin: str) -> float:
        """If the last exit on this coin was a stop_loss, downsize the re-entry."""
        last = self.positions.last_exit(coin)
        if last is None:
            return 1.0
        if last["exit_reason"] == "stop_loss":
            return self.cfg.exit.post_stop_size_multiplier
        return 1.0

    # ── margin watchdog ───────────────────────────────────────────────────

    def margin_status(self) -> str:
        """Return 'ok' | 'warning' | 'critical'. Logged at every tick."""
        if self.cfg.execution.dry_run or not self.cfg.hyperliquid.wallet_address:
            return "ok"
        try:
            ratio = self.data.get_margin_ratio(self.cfg.hyperliquid.wallet_address)
        except Exception as e:
            log.warning("margin_status fetch failed: %s", e)
            return "ok"
        if ratio >= (1 - self.cfg.risk.margin_ratio_critical):
            log.critical("margin ratio %.2f — CRITICAL", ratio)
            return "critical"
        if ratio >= (1 - self.cfg.risk.margin_ratio_warning):
            log.warning("margin ratio %.2f — warning", ratio)
            return "warning"
        return "ok"
