"""Main tick loop with rich live terminal UI.

Architecture (per tick, runs ~1 min before each hourly funding settlement):

    1. Refresh circuit breakers
    2. Snapshot all HL perp markets (with spinner)
    3. Show eligible + open positions tables
    4. Manage existing positions (progress bar over open positions):
        a. Update funding-collected accrual
        b. Evaluate exit; close if triggered
    5. Scan for new entry candidates (progress bar over eligibles):
        a. Filter by universe gates + cooldown
        b. Evaluate entry rules → ranked list of candidates
        c. Open top N (respecting concurrent cap)
    6. Emit heartbeat
    7. Sleep until next tick (CLI loop only)

Usage:
    python -m src.main [--config config.yaml] [--once]
"""
from __future__ import annotations

# ─── Bootstrap (must run BEFORE other imports) ───────────────────────────
# Centralises everything that previously required PowerShell preamble:
#   1) Force UTF-8 on stdout/stderr for Windows consoles (rich Unicode chars)
#   2) Set COLUMNS default for rich (must happen before ui module is imported)
#   3) Auto-load .env so HL_PRIVATE_KEY etc. are available without manual export
import os
import sys

# 1) UTF-8 stdout/stderr — fixes ─, emojis, accents on Windows cmd/PowerShell.
#    On Python 3.7+, reconfigure() rebinds the encoding at runtime.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 2) Default terminal width for rich. setdefault → respects an explicit COLUMNS
#    if the user already set one in the shell.
os.environ.setdefault("COLUMNS", "120")

# 3) Load .env from current working directory if present.
#    python-dotenv is optional: if missing, fall back to existing env vars.
try:
    from dotenv import load_dotenv  # noqa: WPS433
    load_dotenv()
except ImportError:
    pass
# ─────────────────────────────────────────────────────────────────────────

import argparse
import logging
import signal
import time
from datetime import datetime, timezone
from typing import List

from . import ui
from .config import Config, load_config
from .data_client import HLDataClient
from .executor import HLExecutor
from .notifier import Notifier, setup_logging
from .position_manager import PositionManager
from .risk_manager import RiskManager
from .signal_engine import (
    EntryDecision,
    evaluate_entry,
    evaluate_exit,
    is_eligible,
    rank_entry_candidates,
)

log = logging.getLogger("main")


# ─────────────────────────────────────────────────────────────────────────────
#  Reconciliation: detect positions closed outside the bot (HL native SL/TP fired)
# ─────────────────────────────────────────────────────────────────────────────


def reconcile_positions(
    cfg: Config,
    data: HLDataClient,
    positions: PositionManager,
    notifier: Notifier,
) -> int:
    """Compare DB OPEN positions vs live HL state. Close orphans.

    Returns the number of orphan closes processed.
    Skipped in dry-run (no real positions on HL to compare with).
    """
    if cfg.execution.dry_run or not cfg.hyperliquid.wallet_address:
        return 0

    db_open = positions.list_open()
    if not db_open:
        return 0

    try:
        hl_open = set(data.get_open_position_coins(cfg.hyperliquid.wallet_address))
    except Exception as e:
        log.warning("reconcile: could not fetch HL state: %s", e)
        return 0

    closed_count = 0
    for row in db_open:
        coin = row["coin"]
        if coin in hl_open:
            continue  # still alive on HL
        # ─── Orphan: position is OPEN in our DB but absent from HL ───
        sl_oid = row["sl_oid"] if "sl_oid" in row.keys() else None
        tp_oid = row["tp_oid"] if "tp_oid" in row.keys() else None
        entry_ts_ms = int(
            datetime.fromisoformat(
                row["entry_timestamp"].replace("Z", "+00:00")
            ).timestamp() * 1000
        )
        fill = data.find_close_fill(
            cfg.hyperliquid.wallet_address, coin, entry_ts_ms, sl_oid, tp_oid,
        )
        if fill:
            close_px = float(fill.get("px", row["entry_price"]))
            fill_oid = int(fill.get("oid", 0))
            if fill_oid and fill_oid == sl_oid:
                reason = "hl_sl_triggered"
            elif fill_oid and fill_oid == tp_oid:
                reason = "hl_tp_triggered"
            else:
                reason = "external_close"
        else:
            # Couldn't find a fill — use last known mark for PnL estimate
            close_px = row["entry_price"]
            reason = "external_close_unknown_price"

        size_usd = float(row["size_usd"])
        sign = -1 if row["direction"] == "short" else 1
        price_pnl = sign * (close_px - row["entry_price"]) / row["entry_price"] * size_usd
        funding_collected = float(row["funding_collected_usd"] or 0)

        positions.record_close(
            row["id"], close_px, reason,
            realized_pnl_usd=price_pnl,
            funding_collected_usd=funding_collected,
        )
        notifier.exit(coin, reason, price_pnl, funding_collected)
        closed_count += 1
        log.info("RECONCILE: closed orphan %s reason=%s px=%.6f pnl=%.2f",
                 coin, reason, close_px, price_pnl)

    return closed_count


# ─────────────────────────────────────────────────────────────────────────────
#  Trigger order management
# ─────────────────────────────────────────────────────────────────────────────


def _place_triggers_for_open(
    cfg: Config,
    coin: str,
    direction: str,
    size_base: float,
    entry_px: float,
    position_id: int,
    executor: HLExecutor,
    positions: PositionManager,
) -> None:
    """Place SL + TP triggers on HL right after an open, persist their oids."""
    if not cfg.execution.use_native_triggers:
        return
    sl_pct = cfg.exit.stop_loss_pct / 100
    tp_pct = cfg.exit.take_profit_pct / 100
    if direction == "short":
        sl_trigger = entry_px * (1 + sl_pct)
        tp_trigger = entry_px * (1 - tp_pct)
    else:  # long
        sl_trigger = entry_px * (1 - sl_pct)
        tp_trigger = entry_px * (1 + tp_pct)

    sl_res = executor.place_trigger_sl(coin, direction, size_base, sl_trigger)
    tp_res = executor.place_trigger_tp(coin, direction, size_base, tp_trigger)

    if not sl_res.success:
        log.error("SL trigger failed for %s: %s", coin, sl_res.error)
    if not tp_res.success:
        log.error("TP trigger failed for %s: %s", coin, tp_res.error)

    positions.update_trigger_oids(
        position_id,
        sl_oid=sl_res.oid if sl_res.success else None,
        tp_oid=tp_res.oid if tp_res.success else None,
    )


def _cancel_triggers_before_close(
    cfg: Config,
    coin: str,
    sl_oid,
    tp_oid,
    position_id: int,
    executor: HLExecutor,
    positions: PositionManager,
) -> None:
    """Cancel pending SL/TP triggers before we manually close a position."""
    if not cfg.execution.use_native_triggers:
        return
    sl_res, tp_res = executor.cancel_triggers(coin, sl_oid, tp_oid)
    if not sl_res.success:
        log.warning("Could not cancel SL %s oid=%s: %s", coin, sl_oid, sl_res.error)
    if not tp_res.success:
        log.warning("Could not cancel TP %s oid=%s: %s", coin, tp_oid, tp_res.error)
    positions.clear_trigger_oids(position_id)


# ─────────────────────────────────────────────────────────────────────────────
#  Single tick
# ─────────────────────────────────────────────────────────────────────────────


def run_tick(
    cfg: Config,
    data: HLDataClient,
    positions: PositionManager,
    executor: HLExecutor,
    risk: RiskManager,
    notifier: Notifier,
    tick_num: int,
) -> None:
    t_start = time.time()
    ui.print_tick_start(tick_num)

    # ── 1. Circuit breakers ──────────────────────────────────────────────
    risk.check_circuit_breakers()
    if not risk.allow_new_entries():
        ui.warn(f"Halted: {risk.halt_reason} — only exits will be processed")

    # ── 2. Reconciliation: detect HL-side closes (native SL/TP fired or manual) ──
    n_reconciled = reconcile_positions(cfg, data, positions, notifier)
    if n_reconciled > 0:
        ui.warn(f"Reconciled {n_reconciled} orphan position(s) — closed in DB to match HL state")

    # ── 3. Market snapshot ───────────────────────────────────────────────
    try:
        with ui.step_spinner("📡  Snapshotting Hyperliquid market..."):
            snapshots = data.snapshot_all()
    except Exception as e:
        notifier.error("snapshot_all", str(e))
        return
    ui.ok(f"Snapshot: {len(snapshots)} perps fetched")

    snap_by_coin = {s.coin: s for s in snapshots}
    eligibles = [s for s in snapshots if is_eligible(s, cfg)]
    ui.info(
        f"Universe filter: {len(eligibles)}/{len(snapshots)} pass "
        f"(OI ≥ ${cfg.universe.min_open_interest_usd/1e6:.0f}M, "
        f"spread ≤ {cfg.universe.max_spread_bps:.1f}bps)"
    )

    # ── 3. Manage open positions ─────────────────────────────────────────
    open_rows = positions.list_open()
    ui.print_open_positions(open_rows, snap_by_coin)

    if open_rows:
        for row in ui.iter_with_progress(
            list(open_rows), "🔄  Evaluating exits..."
        ):
            _manage_one_position(
                row, snap_by_coin, cfg, data, executor, positions, notifier,
            )

    # ── 4. Evaluate entry decisions for ALL eligibles (for diagnostic) ───
    candidates = _evaluate_all_candidates(
        cfg, data, snap_by_coin, positions, risk,
    )
    # Show the diagnostic table: per-coin which gate passes/fails
    ui.print_candidates_table(candidates, cfg, top=15)

    # ── 5. Open top candidates (if not halted) ───────────────────────────
    n_signals = 0
    if risk.allow_new_entries():
        n_signals = _open_top_candidates(
            candidates, cfg, snap_by_coin, positions, executor, risk, notifier,
        )
        if n_signals == 0:
            ui.no_signals(
                cfg.entry.min_funding_apr_pct, cfg.entry.min_funding_zscore
            )

    # ── 5. Heartbeat ─────────────────────────────────────────────────────
    if cfg.notifications.heartbeat:
        notifier.heartbeat(
            n_eligible=len(eligibles),
            n_open=len(positions.list_open()),
            n_signals=n_signals,
            capital=risk.current_capital_usd(),
            halted=not risk.allow_new_entries(),
            halt_reason=risk.halt_reason,
        )

    margin_state = risk.margin_status()
    if margin_state != "ok":
        ui.warn(f"Margin state: {margin_state}")

    ui.print_tick_end(time.time() - t_start)


# ─────────────────────────────────────────────────────────────────────────────
#  Per-position management
# ─────────────────────────────────────────────────────────────────────────────


def _manage_one_position(row, snap_by_coin, cfg, data, executor, positions, notifier):
    coin = row["coin"]
    snap = snap_by_coin.get(coin)
    if snap is None:
        log.warning("No snapshot for open position %s — skip", coin)
        return

    # Accrue funding paid to us
    hourly_rate = snap.funding_apr_pct / (24 * 365 * 100)
    size_usd = float(row["size_usd"])
    sign = -1 if row["direction"] == "short" else 1
    funding_pnl_this_hour = sign * hourly_rate * size_usd
    positions.add_funding_collected(row["id"], funding_pnl_this_hour)

    # Exit decision (note: TP/SL are handled natively by HL when use_native_triggers=true,
    # so this evaluation effectively focuses on funding_normalized / zscore / timeout
    # for non-price-based exits)
    history = data.get_funding_history_apr(coin, cfg.entry.zscore_lookback_days)
    decision = evaluate_exit(
        positions.to_position_state(row), snap, history, cfg,
    )
    if not decision.exit:
        return

    log.info("EXIT trigger %s: %s", coin, decision.reason)

    # Cancel pending native triggers BEFORE issuing the manual close so they don't fire
    # against our reduce-only close. Idempotent if oids are NULL.
    sl_oid = row["sl_oid"] if "sl_oid" in row.keys() else None
    tp_oid = row["tp_oid"] if "tp_oid" in row.keys() else None
    _cancel_triggers_before_close(
        cfg, coin, sl_oid, tp_oid, row["id"], executor, positions,
    )

    if cfg.execution.dry_run:
        fill = executor.close_position(coin, snap.mark_px)
        close_price = snap.mark_px
    else:
        fill = executor.with_retries(
            executor.close_position, coin, snap.mark_px,
        )
        close_price = fill.avg_price or snap.mark_px

    if not fill.success:
        notifier.error(f"close_position {coin}", fill.error or "?")
        positions.mark_error(row["id"], fill.error or "close failed")
        return

    sign_pnl = -1 if row["direction"] == "short" else 1
    price_pnl = (
        sign_pnl * (close_price - row["entry_price"]) / row["entry_price"] * size_usd
    )
    funding_collected = float(row["funding_collected_usd"] or 0)

    positions.record_close(
        row["id"], close_price, decision.reason,
        realized_pnl_usd=price_pnl,
        funding_collected_usd=funding_collected,
    )
    notifier.exit(coin, decision.reason, price_pnl, funding_collected)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry scan
# ─────────────────────────────────────────────────────────────────────────────


def _evaluate_all_candidates(
    cfg: Config,
    data: HLDataClient,
    snap_by_coin: dict,
    positions: PositionManager,
    risk: RiskManager,
) -> List[tuple]:
    """Evaluate the entry decision for every eligible+not-already-open coin.

    Returns list of (MarketSnapshot, EntryDecision). Used for both:
      - the diagnostic table in the UI (shows why each coin passes/fails)
      - the entry opener (filters enter=True, ranks, opens top N)
    """
    already_open = set(positions.open_coins())
    to_eval = [
        (coin, snap) for coin, snap in snap_by_coin.items()
        if coin not in already_open
        and is_eligible(snap, cfg)
        and not risk.in_cooldown(coin)
    ]
    if not to_eval:
        return []

    results: List[tuple] = []
    for coin, snap in ui.iter_with_progress(
        to_eval, f"🎯  Scanning {len(to_eval)} eligible coins for entry signals..."
    ):
        history = data.get_funding_history_apr(
            coin, cfg.entry.zscore_lookback_days
        )
        decision = evaluate_entry(snap, history, cfg)
        results.append((snap, decision))
        if decision.enter:
            log.info(
                "CANDIDATE %s: %s (score=%.1f)",
                coin, decision.reason, decision.score,
            )
    return results


def _open_top_candidates(
    candidates: List[tuple],
    cfg: Config,
    snap_by_coin: dict,
    positions: PositionManager,
    executor: HLExecutor,
    risk: RiskManager,
    notifier: Notifier,
) -> int:
    """Open the top N candidates that pass the entry gates. Returns count opened."""
    already_open = positions.open_coins()
    open_slots = cfg.account.max_concurrent_positions - len(already_open)
    if open_slots <= 0:
        return 0

    decisions = [d for _, d in candidates]
    top = rank_entry_candidates(decisions, top_n=open_slots)

    opened = 0
    for d in top:
        snap = snap_by_coin[d.coin]
        multiplier = risk.post_stop_multiplier_for(d.coin)
        sizing = risk.size_new_entry(
            snap, score=d.score, post_stop_multiplier=multiplier,
        )
        if not sizing.can_enter:
            ui.dim(f"  ↳ skipping {d.coin}: {sizing.reason}")
            continue

        fill = (
            executor.open_short(
                d.coin, sizing.notional_usd, sizing.leverage, snap.mark_px,
            )
            if cfg.execution.dry_run
            else executor.with_retries(
                executor.open_short, d.coin, sizing.notional_usd,
                sizing.leverage, snap.mark_px,
            )
        )
        if not fill.success:
            notifier.error(f"open_short {d.coin}", fill.error or "?")
            continue

        position_id = positions.record_open(
            coin=d.coin, direction="short",
            size_usd=fill.notional_usd, size_base=fill.size_base,
            leverage=sizing.leverage,
            entry_price=fill.avg_price, entry_funding_apr=d.funding_apr,
            entry_zscore=d.zscore, entry_reason=d.reason,
        )
        notifier.entry(
            d.coin, fill.notional_usd, sizing.leverage,
            fill.avg_price, d.funding_apr, d.zscore, d.reason,
        )
        _place_triggers_for_open(
            cfg, d.coin, "short", fill.size_base, fill.avg_price,
            position_id, executor, positions,
        )
        opened += 1
    return opened


# ─────────────────────────────────────────────────────────────────────────────
#  Loop runner
# ─────────────────────────────────────────────────────────────────────────────


def _seconds_until_next_tick(cfg: Config) -> int:
    interval = cfg.scheduler.tick_interval_seconds
    offset = cfg.scheduler.tick_offset_seconds_before_hour
    now = datetime.now(timezone.utc)
    seconds_since_epoch = int(now.timestamp())
    next_boundary = ((seconds_since_epoch // interval) + 1) * interval - offset
    delay = next_boundary - seconds_since_epoch
    return max(5, delay)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--once", action="store_true",
        help="Run one tick and exit (debugging / cron mode)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)

    data = HLDataClient(
        network=cfg.hyperliquid.network,
        tls_verify=cfg.execution.tls_verify,
    )
    positions = PositionManager()
    executor = HLExecutor(cfg)
    risk = RiskManager(cfg, data, positions)
    notifier = Notifier(cfg)

    notifier.boot(cfg)

    # Graceful shutdown
    stop = {"flag": False}

    def _on_sig(signum, _frame):
        log.info("Signal %d received — finishing current tick then exit", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    if args.once:
        try:
            run_tick(cfg, data, positions, executor, risk, notifier, tick_num=1)
            return 0
        except Exception as e:
            log.exception("Fatal in single tick: %s", e)
            notifier.error("tick", str(e))
            return 1

    tick_num = 0
    while not stop["flag"]:
        tick_num += 1
        try:
            run_tick(cfg, data, positions, executor, risk, notifier, tick_num)
        except Exception as e:
            log.exception("Tick failed: %s", e)
            notifier.error("tick", str(e))
        if stop["flag"]:
            break
        delay = _seconds_until_next_tick(cfg)
        # Instead of dumb sleeping, run a live positions monitor that refreshes
        # mark prices and unrealized PnL every second until next tick is due.
        ui.live_monitor_loop(
            rows_provider=positions.list_open,
            mids_provider=lambda: data.info.all_mids(),
            cfg=cfg,
            capital_provider=risk.current_capital_usd,
            duration_seconds=delay,
            stop_flag=stop,
        )

    ui.info("Shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
