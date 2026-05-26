"""Performance metrics for backtest results.

Takes a DataFrame of trades and returns a dict of canonical stats:
total return, Sharpe, max DD, hit rate, profit factor, etc.

Also exposes equity_curve() which returns a per-day cumulative-PnL series
suitable for plotting.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    hit_rate: float = 0.0
    total_pnl_usd: float = 0.0
    total_pnl_pct: float = 0.0     # vs (n_trades × notional)
    avg_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0     # gross gains / gross losses
    sharpe_annual: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_hours_held: float = 0.0
    # Exit reasons distribution (top 4)
    pct_tp: float = 0.0
    pct_sl: float = 0.0
    pct_funding_normalized: float = 0.0
    pct_other: float = 0.0


def compute_metrics(trades_df: pd.DataFrame) -> Metrics:
    if trades_df is None or trades_df.empty:
        return Metrics()

    m = Metrics()
    m.n_trades = len(trades_df)
    wins = trades_df[trades_df["net_pnl_usd"] > 0]
    losses = trades_df[trades_df["net_pnl_usd"] < 0]
    m.n_wins = len(wins)
    m.n_losses = len(losses)
    m.hit_rate = m.n_wins / m.n_trades * 100 if m.n_trades else 0

    m.total_pnl_usd = float(trades_df["net_pnl_usd"].sum())
    total_notional = float(trades_df["notional_usd"].sum())
    m.total_pnl_pct = (m.total_pnl_usd / total_notional * 100) if total_notional else 0
    m.avg_pnl_pct = float(trades_df["net_pnl_pct"].mean())
    m.avg_win_pct = float(wins["net_pnl_pct"].mean()) if not wins.empty else 0
    m.avg_loss_pct = float(losses["net_pnl_pct"].mean()) if not losses.empty else 0

    gross_wins = float(wins["net_pnl_usd"].sum()) if not wins.empty else 0
    gross_losses = abs(float(losses["net_pnl_usd"].sum())) if not losses.empty else 0
    m.profit_factor = gross_wins / gross_losses if gross_losses > 0 else (
        float("inf") if gross_wins > 0 else 0
    )

    m.avg_hours_held = float(trades_df["hours_held"].mean())

    # Sharpe + Max DD: build equity curve over daily aggregation
    eq = equity_curve(trades_df)
    if len(eq) > 1:
        daily_pnl = eq["pnl_usd"].diff().dropna()
        if daily_pnl.std() > 0:
            m.sharpe_annual = float(
                daily_pnl.mean() / daily_pnl.std() * math.sqrt(365)
            )
        # Max drawdown vs cumulative peak
        cum = eq["cum_pnl_usd"]
        peak = cum.cummax()
        # In % of notional (using max trade size as base)
        # Better: use cum + initial capital baseline
        baseline = total_notional / m.n_trades if m.n_trades else 1
        dd_pct = (peak - cum) / (peak.abs().clip(lower=baseline)) * 100
        m.max_drawdown_pct = float(dd_pct.max())

    # Exit reasons
    if "exit_reason" in trades_df.columns:
        n = m.n_trades
        rc = trades_df["exit_reason"].value_counts()
        tp_count = rc.get("hl_tp_triggered", 0) + rc.get("take_profit", 0)
        sl_count = rc.get("hl_sl_triggered", 0) + rc.get("stop_loss", 0)
        fn_count = rc.get("funding_normalized", 0)
        m.pct_tp = tp_count / n * 100
        m.pct_sl = sl_count / n * 100
        m.pct_funding_normalized = fn_count / n * 100
        m.pct_other = 100 - m.pct_tp - m.pct_sl - m.pct_funding_normalized

    return m


def equity_curve(trades_df: pd.DataFrame, freq: str = "D") -> pd.DataFrame:
    """Return cumulative PnL aggregated at ``freq`` (default daily).

    Output columns: ts, pnl_usd (per-period), cum_pnl_usd (running sum).
    """
    if trades_df is None or trades_df.empty:
        return pd.DataFrame(columns=["ts", "pnl_usd", "cum_pnl_usd"])
    df = trades_df.copy()
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True)
    df = df.set_index("exit_time").sort_index()
    grouped = df["net_pnl_usd"].resample(freq).sum().fillna(0)
    eq = pd.DataFrame({
        "ts": grouped.index,
        "pnl_usd": grouped.values,
        "cum_pnl_usd": grouped.cumsum().values,
    }).reset_index(drop=True)
    return eq


def per_coin_breakdown(trades_df: pd.DataFrame) -> pd.DataFrame:
    """Per-coin: n_trades, hit_rate, total_pnl, avg_pnl."""
    if trades_df is None or trades_df.empty:
        return pd.DataFrame()
    g = trades_df.groupby("coin").agg(
        n_trades=("net_pnl_usd", "size"),
        wins=("net_pnl_usd", lambda x: int((x > 0).sum())),
        total_pnl_usd=("net_pnl_usd", "sum"),
        avg_pnl_pct=("net_pnl_pct", "mean"),
        avg_hours_held=("hours_held", "mean"),
    ).reset_index()
    g["hit_rate"] = g["wins"] / g["n_trades"] * 100
    return g.sort_values("total_pnl_usd", ascending=False)


def metrics_to_dict(m: Metrics) -> Dict:
    return asdict(m)
