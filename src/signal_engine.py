"""Entry & exit signal evaluation.

Stateless utility module: takes a market snapshot + funding history + position
state + config, returns a clear go/no-go decision with the rule that fired.

This is where ALL business logic lives. The executor and main loop should
never embed thresholds — they read decisions from here.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .config import Config
from .data_client import MarketSnapshot

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Decisions
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class EntryDecision:
    enter: bool
    coin: str
    reason: str
    funding_apr: float = 0.0
    zscore: float = 0.0
    score: float = 0.0
    # Per-gate diagnosis (for the UI table — see ui.print_candidates_table)
    funding_ok: bool = False
    direction_ok: bool = False
    premium_ok: bool = False
    persistence_ok: bool = False
    persistence_count: int = 0       # how many of last N samples passed threshold
    persistence_total: int = 0       # = cfg.entry.persistence_hours
    zscore_ok: bool = False
    premium_bps: float = 0.0


@dataclass
class ExitDecision:
    exit: bool
    reason: str            # one of EXIT_REASONS keys
    urgency: str = "normal"  # "normal" | "urgent" — drives market vs limit


EXIT_REASONS = {
    "funding_normalized": "Funding APR dropped below exit threshold",
    "take_profit": "Take profit hit",
    "stop_loss": "Stop loss hit",
    "timeout": "Max holding time reached",
    "zscore_normalized": "Z-score normalized below exit threshold",
    "manual": "Manual close",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Stats helpers
# ─────────────────────────────────────────────────────────────────────────────


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: List[float], mean: Optional[float] = None) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean if mean is not None else _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def compute_zscore(current: float, history: List[float]) -> float:
    """Z-score of `current` vs `history`. Returns 0.0 if history insufficient."""
    if len(history) < 24:    # at least 1 day of hourly samples
        return 0.0
    m = _mean(history)
    s = _stdev(history, mean=m)
    if s == 0:
        return 0.0
    return (current - m) / s


def check_persistence(history: List[float], threshold: float, n_hours: int) -> bool:
    """True iff the last `n_hours` values are ALL above `threshold` (in absolute)."""
    if len(history) < n_hours:
        return False
    tail = history[-n_hours:]
    return all(abs(x) >= threshold for x in tail) and all(
        (x >= 0) == (tail[-1] >= 0) for x in tail  # same sign as latest
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Universe filter
# ─────────────────────────────────────────────────────────────────────────────


def is_eligible(snap: MarketSnapshot, cfg: Config) -> bool:
    """Static eligibility check (liquidity, exclusion lists)."""
    u = cfg.universe
    if u.include_only and snap.coin not in u.include_only:
        return False
    if snap.coin in u.exclude_coins:
        return False
    if snap.open_interest_usd < u.min_open_interest_usd:
        return False
    if snap.spread_bps is None:
        return False
    if snap.spread_bps > u.max_spread_bps:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Entry
# ─────────────────────────────────────────────────────────────────────────────


def evaluate_entry(
    snap: MarketSnapshot,
    funding_history_apr: List[float],
    cfg: Config,
) -> EntryDecision:
    """Decide whether to OPEN a position on this coin right now.

    Returns ``EntryDecision`` with .enter=True only if every entry gate passes.
    All gate flags are populated even on early-fail, so the UI can show a
    per-gate diagnostic table.
    """
    e = cfg.entry
    coin = snap.coin

    # ─── Pre-compute all gate values so they're always available ────────
    funding_ok = abs(snap.funding_apr_pct) >= e.min_funding_apr_pct
    direction_ok = (
        snap.funding_apr_pct > 0
        if e.direction_mode == "short_high_funding"
        else True
    )
    premium_ok = abs(snap.premium_bps) <= e.max_premium_bps

    history_with_current = list(funding_history_apr) + [snap.funding_apr_pct]
    n_target = e.persistence_hours
    tail = (
        history_with_current[-n_target:]
        if len(history_with_current) >= n_target else history_with_current
    )
    # Count how many tail samples are above threshold AND same sign as the latest
    if tail:
        latest_sign = tail[-1] >= 0
        persistence_count = sum(
            1 for v in tail
            if abs(v) >= e.min_funding_apr_pct and (v >= 0) == latest_sign
        )
    else:
        persistence_count = 0
    persistence_ok = persistence_count == n_target and n_target > 0

    z = compute_zscore(snap.funding_apr_pct, funding_history_apr)
    zscore_ok = abs(z) >= e.min_funding_zscore

    # Common diagnosis payload
    base_diag = dict(
        funding_apr=snap.funding_apr_pct,
        zscore=z,
        funding_ok=funding_ok,
        direction_ok=direction_ok,
        premium_ok=premium_ok,
        persistence_ok=persistence_ok,
        persistence_count=persistence_count,
        persistence_total=n_target,
        zscore_ok=zscore_ok,
        premium_bps=snap.premium_bps,
    )

    # ─── Sequential gating, first-fail wins ──────────────────────────────
    if not funding_ok:
        return EntryDecision(
            False, coin,
            f"funding {snap.funding_apr_pct:.1f}% < {e.min_funding_apr_pct}%",
            **base_diag,
        )
    if not direction_ok:
        return EntryDecision(
            False, coin, "wrong direction (need funding > 0)",
            **base_diag,
        )
    if not premium_ok:
        return EntryDecision(
            False, coin,
            f"premium {snap.premium_bps:.0f}bps exceeds max {e.max_premium_bps}bps",
            **base_diag,
        )
    if not persistence_ok:
        return EntryDecision(
            False, coin,
            f"persistence {persistence_count}/{n_target} samples above threshold",
            **base_diag,
        )
    if not zscore_ok:
        return EntryDecision(
            False, coin,
            f"|zscore| {abs(z):.2f} < {e.min_funding_zscore}",
            **base_diag,
        )

    # ── all gates passed ────────────────────────────────────────────────
    liquidity_factor = min(1.0, snap.open_interest_usd / 50_000_000)
    score = abs(snap.funding_apr_pct) * abs(z) * liquidity_factor

    return EntryDecision(
        enter=True,
        coin=coin,
        reason=(
            f"funding {snap.funding_apr_pct:+.1f}%, z={z:+.2f}, "
            f"OI=${snap.open_interest_usd/1e6:.1f}M, spread={snap.spread_bps:.1f}bps"
        ),
        score=score,
        **base_diag,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Exit
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PositionState:
    """Minimal view of an open position for exit evaluation."""
    coin: str
    direction: str         # "short" or "long"
    entry_price: float
    entry_timestamp_iso: str
    entry_funding_apr: float


def _hours_since(iso_ts: str) -> float:
    try:
        t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 0.0


def evaluate_exit(
    position: PositionState,
    snap: MarketSnapshot,
    funding_history_apr: List[float],
    cfg: Config,
) -> ExitDecision:
    """Decide whether to CLOSE an open position. First triggered rule wins."""
    x = cfg.exit
    sign = -1 if position.direction == "short" else 1
    price_pct_move = (snap.mark_px - position.entry_price) / position.entry_price * 100

    # 1. Take profit (favorable move)
    favorable_move_pct = -sign * price_pct_move   # for short: price drop = positive
    if favorable_move_pct >= x.take_profit_pct:
        return ExitDecision(True, "take_profit")

    # 2. Stop loss (adverse move) — URGENT
    adverse_move_pct = sign * price_pct_move      # for short: price rise = positive
    if adverse_move_pct >= x.stop_loss_pct:
        return ExitDecision(True, "stop_loss", urgency="urgent")

    # 3. Funding normalized (in the direction we entered)
    # If we entered on positive funding, we want |funding| to drop
    if abs(snap.funding_apr_pct) < x.funding_apr_exit_threshold:
        return ExitDecision(True, "funding_normalized")

    # 4. Z-score back to normal
    z = compute_zscore(snap.funding_apr_pct, funding_history_apr)
    if abs(z) < x.exit_on_zscore_below:
        return ExitDecision(True, "zscore_normalized")

    # 5. Timeout
    if _hours_since(position.entry_timestamp_iso) >= x.timeout_hours:
        return ExitDecision(True, "timeout")

    return ExitDecision(False, "hold")


# ─────────────────────────────────────────────────────────────────────────────
#  Ranking utility
# ─────────────────────────────────────────────────────────────────────────────


def rank_entry_candidates(
    decisions: List[EntryDecision], top_n: int
) -> List[EntryDecision]:
    """Sort by score desc, return top N."""
    eligible = [d for d in decisions if d.enter]
    eligible.sort(key=lambda d: d.score, reverse=True)
    return eligible[:top_n]
