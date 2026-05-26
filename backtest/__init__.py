"""Backtest module for the HL Funding Spike Sniper strategy.

Replays the live bot's signal logic (src.signal_engine) on historical data
fetched from Dune (hyperliquid.market_data table). Supports parameter grid
sweeps and produces a self-contained HTML report.

Usage:
    python -m backtest fetch    # ingest data from Dune to local Parquet
    python -m backtest sweep    # run the grid search
    python -m backtest report   # generate HTML report
    python -m backtest run      # do all three end-to-end
"""
