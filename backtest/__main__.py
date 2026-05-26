"""CLI entry point for the backtest module.

Usage:
    python -m backtest run     # full pipeline: fetch + sweep + report
    python -m backtest fetch   # only fetch data
    python -m backtest sweep   # only run sweep (requires cached data)
    python -m backtest report  # only generate report (requires cached results)

Quick variants:
    python -m backtest run --quick     # tiny coin set, last 60 days, small grid
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# Bootstrap (same as main.py — UTF-8 on Windows, COLUMNS for rich)
import os
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("COLUMNS", "120")
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .data import DEFAULT_COINS, FetchSpec, fetch, summary
from .engine import BacktestConfig, backtest, trades_to_dataframe
from .metrics import compute_metrics, equity_curve, per_coin_breakdown
from .report import generate_report
from .sweep import DEFAULT_GRID, run_sweep, save_sweep, _build_config


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Silence noisy loggers
    for noisy in ("urllib3", "hyperliquid"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ─── Quick / full preset ─────────────────────────────────────────────────


def _spec_quick() -> FetchSpec:
    """Quick: all coins (with OI > $1M), 60 days. Tiny grid for fast iteration."""
    today = date.today()
    return FetchSpec(
        coins=None,   # all coins
        start_date=(today - timedelta(days=60)).isoformat(),
        end_date=(today - timedelta(days=2)).isoformat(),  # avoid Dune lag tail
        min_oi_filter_usd=1_000_000,
    )


def _spec_full() -> FetchSpec:
    """Full: all coins (with OI > $1M), 6 months."""
    today = date.today()
    return FetchSpec(
        coins=None,   # all coins
        start_date=(today - timedelta(days=180)).isoformat(),
        end_date=(today - timedelta(days=2)).isoformat(),
        min_oi_filter_usd=1_000_000,
    )


def _grid_quick() -> dict:
    """Quick grid focused on TP > SL ("let winners run") — ~96 valid combos."""
    return {
        "min_funding_apr_pct": [25, 50, 100],
        "min_funding_zscore": [1.0, 2.0],
        "persistence_hours": [1, 3],
        # TP > SL pairs: 4 TP × 2 SL = 8 valid pairs (all valid since min TP=10 > max SL=8)
        "take_profit_pct": [10, 15, 20, 25],
        "stop_loss_pct": [5, 8],
        "timeout_hours": [168],
        "funding_apr_exit_threshold_ratio": [0.5],
    }


# ─── Sub-commands ────────────────────────────────────────────────────────


def cmd_fetch(args) -> int:
    spec = _spec_quick() if args.quick else _spec_full()
    df = fetch(spec, force=args.force)
    print("Data fetched:")
    print(summary(df))
    return 0


def cmd_sweep(args) -> int:
    spec = _spec_quick() if args.quick else _spec_full()
    if not spec.cache_file.exists():
        print(f"No cached data at {spec.cache_file}. Run `fetch` first.")
        return 1
    df = pd.read_parquet(spec.cache_file)
    grid = _grid_quick() if args.quick else DEFAULT_GRID
    print(f"Running sweep on {len(df)} rows × {len(df['coin'].unique())} coins...")
    sweep_results = run_sweep(df, grid=grid)
    save_sweep(sweep_results, "data/cache/sweep_results.parquet")
    print(f"Sweep done. {len(sweep_results)} combinations tested.")
    return 0


def cmd_report(args) -> int:
    sweep_path = Path("data/cache/sweep_results.parquet")
    if not sweep_path.exists():
        print(f"No sweep results at {sweep_path}. Run `sweep` first.")
        return 1
    sweep_df = pd.read_parquet(sweep_path)
    if sweep_df.empty:
        print("Sweep results are empty.")
        return 1

    # Re-run the best Sharpe config on the data to get trades + equity curve
    spec = _spec_quick() if args.quick else _spec_full()
    if not spec.cache_file.exists():
        print(f"No cached data at {spec.cache_file}.")
        return 1
    data = pd.read_parquet(spec.cache_file)

    best_row = sweep_df.loc[sweep_df["sharpe_annual"].idxmax()]
    best_params = {k: best_row[k] for k in DEFAULT_GRID.keys() if k in best_row}
    # Coerce int-like
    for k in ("persistence_hours", "timeout_hours"):
        if k in best_params:
            best_params[k] = int(best_params[k])
    cfg = _build_config(best_params)
    print(f"Re-running best Sharpe config: {best_params}")
    trades = backtest(data, cfg)
    trades_df = trades_to_dataframe(trades)
    eq_df = equity_curve(trades_df) if not trades_df.empty else pd.DataFrame()
    per_coin_df = per_coin_breakdown(trades_df)

    data_summary = summary(data)
    out_path = generate_report(
        sweep_df=sweep_df,
        best_trades_df=trades_df,
        best_eq_df=eq_df,
        per_coin_df=per_coin_df,
        data_summary=data_summary,
        output_path="data/reports/backtest_report.html",
    )
    print(f"Report written: {out_path.resolve()}")
    return 0


def cmd_run(args) -> int:
    rc = cmd_fetch(args)
    if rc != 0:
        return rc
    rc = cmd_sweep(args)
    if rc != 0:
        return rc
    return cmd_report(args)


# ─── Argparse ───────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="HL Funding Sniper backtest")
    parser.add_argument("--quick", action="store_true",
                        help="Use tiny dataset and grid for fast iteration")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch data even if cached")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING"])

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="Pull data from Dune to local Parquet")
    sub.add_parser("sweep", help="Run grid search (requires cached data)")
    sub.add_parser("report", help="Generate HTML report (requires sweep results)")
    sub.add_parser("run", help="Run full pipeline: fetch + sweep + report")

    args = parser.parse_args()
    _setup_logging(args.log_level)

    handlers = {
        "fetch": cmd_fetch, "sweep": cmd_sweep,
        "report": cmd_report, "run": cmd_run,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
