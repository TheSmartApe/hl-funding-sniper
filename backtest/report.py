"""Generate a self-contained HTML report from sweep results + best-config trades.

The report has 5 sections:
  1. Summary (best Sharpe & best total return configs)
  2. Heatmaps of Sharpe by every 2-param pair (3 most informative shown)
  3. Top-20 configurations table
  4. Trade-count and metric distribution by parameter (n_trades per value)
  5. Equity curve + trade distribution for the best config

Pure Plotly so the output is a single self-contained .html file you can open
in any browser, share, or commit.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

log = logging.getLogger(__name__)


# ─── Parameter axes (the ones we sweep) ──────────────────────────────────


PARAM_COLS = [
    "min_funding_apr_pct", "min_funding_zscore", "persistence_hours",
    "take_profit_pct", "stop_loss_pct", "timeout_hours",
    "funding_apr_exit_threshold_ratio",
]

METRIC_COLS_TO_SHOW = [
    "n_trades", "hit_rate", "total_pnl_usd", "total_pnl_pct",
    "sharpe_annual", "max_drawdown_pct", "profit_factor", "avg_pnl_pct",
    "pct_tp", "pct_sl", "pct_funding_normalized", "avg_hours_held",
]


# ─── Heatmaps ────────────────────────────────────────────────────────────


def _heatmap_pair(
    df: pd.DataFrame, x_col: str, y_col: str, value_col: str,
    title_suffix: str = "", colorscale: str = "RdYlGn",
) -> go.Figure:
    """For each (x, y) pair, take the BEST value_col across all other params."""
    pivot = df.groupby([y_col, x_col])[value_col].max().unstack()
    fig = go.Figure(go.Heatmap(
        z=pivot.values,
        x=[str(v) for v in pivot.columns],
        y=[str(v) for v in pivot.index],
        colorscale=colorscale,
        zmid=0 if value_col in ("sharpe_annual", "total_pnl_usd", "total_pnl_pct") else None,
        text=[[f"{v:.2f}" if pd.notna(v) else "" for v in row] for row in pivot.values],
        texttemplate="%{text}",
        colorbar=dict(title=value_col),
    ))
    fig.update_layout(
        title=f"Best {value_col} by {x_col} × {y_col}{title_suffix}",
        xaxis_title=x_col,
        yaxis_title=y_col,
        height=420,
        margin=dict(l=80, r=40, t=80, b=60),
    )
    return fig


# ─── Trade count distribution per parameter value ────────────────────────


def _trade_count_per_param(df: pd.DataFrame, param: str) -> go.Figure:
    """Average n_trades by each value of `param` (aggregating other params)."""
    agg = df.groupby(param).agg(
        avg_n_trades=("n_trades", "mean"),
        avg_sharpe=("sharpe_annual", "mean"),
        avg_pnl=("total_pnl_usd", "mean"),
    ).reset_index()
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(x=agg[param].astype(str), y=agg["avg_n_trades"],
               name="Avg # trades", marker_color="#4c78a8"),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(x=agg[param].astype(str), y=agg["avg_sharpe"],
                   name="Avg Sharpe", marker_color="#f58518", mode="lines+markers"),
        secondary_y=True,
    )
    fig.update_layout(
        title=f"Trade count & Sharpe by {param}",
        height=350, margin=dict(l=60, r=40, t=60, b=60),
        xaxis_title=param,
    )
    fig.update_yaxes(title_text="Avg # trades", secondary_y=False)
    fig.update_yaxes(title_text="Avg Sharpe annual", secondary_y=True)
    return fig


# ─── Equity curve & trade distribution for best config ──────────────────


def _equity_curve_fig(eq_df: pd.DataFrame, title: str) -> go.Figure:
    fig = go.Figure()
    if eq_df is None or eq_df.empty:
        fig.update_layout(title=f"{title} — no trades")
        return fig
    fig.add_trace(go.Scatter(
        x=eq_df["ts"], y=eq_df["cum_pnl_usd"],
        mode="lines", line=dict(color="#2ca02c", width=2),
        name="Cum PnL",
    ))
    # Underline drawdown areas (zone below cumulative peak)
    peak = eq_df["cum_pnl_usd"].cummax()
    fig.add_trace(go.Scatter(
        x=eq_df["ts"], y=peak,
        mode="lines", line=dict(color="rgba(0,0,0,0.2)", width=1, dash="dot"),
        name="Peak",
    ))
    fig.update_layout(
        title=title,
        xaxis_title="Date", yaxis_title="Cumulative PnL ($)",
        height=400, margin=dict(l=60, r=40, t=60, b=60),
        hovermode="x unified",
    )
    return fig


def _trade_distribution_fig(trades_df: pd.DataFrame, title: str) -> go.Figure:
    if trades_df is None or trades_df.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{title} — no trades")
        return fig
    fig = px.histogram(
        trades_df, x="net_pnl_pct", nbins=40,
        color_discrete_sequence=["#1f77b4"],
        title=title,
    )
    fig.add_vline(x=0, line_dash="dash", line_color="black")
    fig.update_layout(
        height=350, margin=dict(l=60, r=40, t=60, b=60),
        xaxis_title="Net PnL %", yaxis_title="# trades",
    )
    return fig


def _exit_reasons_fig(trades_df: pd.DataFrame, title: str) -> go.Figure:
    if trades_df is None or trades_df.empty:
        fig = go.Figure()
        fig.update_layout(title=f"{title} — no trades")
        return fig
    counts = trades_df["exit_reason"].value_counts().reset_index()
    counts.columns = ["exit_reason", "count"]
    fig = px.bar(
        counts, x="exit_reason", y="count",
        color="exit_reason",
        title=title,
    )
    fig.update_layout(
        height=350, margin=dict(l=60, r=40, t=60, b=60),
        showlegend=False, xaxis_title="", yaxis_title="# trades",
    )
    return fig


def _per_coin_fig(per_coin_df: pd.DataFrame) -> go.Figure:
    if per_coin_df is None or per_coin_df.empty:
        fig = go.Figure()
        fig.update_layout(title="Per-coin breakdown — no trades")
        return fig
    df = per_coin_df.copy()
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(x=df["coin"], y=df["total_pnl_usd"],
               name="Total PnL ($)",
               marker_color=["#2ca02c" if v >= 0 else "#d62728" for v in df["total_pnl_usd"]]),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(x=df["coin"], y=df["n_trades"],
                   name="# trades", marker_color="#ff7f0e",
                   mode="markers", marker=dict(size=10)),
        secondary_y=True,
    )
    fig.update_layout(
        title="Per-coin: total PnL ($) and trade count",
        height=400, margin=dict(l=60, r=40, t=60, b=60),
        xaxis_title="", hovermode="x unified",
    )
    fig.update_yaxes(title_text="Total PnL ($)", secondary_y=False)
    fig.update_yaxes(title_text="# trades", secondary_y=True)
    return fig


# ─── Top configurations table ────────────────────────────────────────────


def _top_configs_html(sweep_df: pd.DataFrame, n: int = 20) -> str:
    df = sweep_df.copy().sort_values("sharpe_annual", ascending=False).head(n)
    cols_show = PARAM_COLS + ["n_trades", "hit_rate", "total_pnl_usd",
                              "sharpe_annual", "max_drawdown_pct",
                              "profit_factor", "avg_pnl_pct"]
    cols_show = [c for c in cols_show if c in df.columns]
    df = df[cols_show].round(2)
    return df.to_html(index=False, classes="dataframe", escape=False)


# ─── Summary cards ───────────────────────────────────────────────────────


def _summary_html(sweep_df: pd.DataFrame, data_summary: str) -> str:
    if sweep_df.empty:
        return "<p>No sweep results.</p>"
    best_sharpe = sweep_df.loc[sweep_df["sharpe_annual"].idxmax()]
    best_pnl = sweep_df.loc[sweep_df["total_pnl_usd"].idxmax()]

    def _fmt_cfg(row):
        return (
            f"funding≥{row['min_funding_apr_pct']:.0f}% · "
            f"|z|≥{row['min_funding_zscore']:.1f} · "
            f"persist {int(row['persistence_hours'])}h · "
            f"TP {row['take_profit_pct']:.0f}% / "
            f"SL {row['stop_loss_pct']:.0f}% · "
            f"timeout {int(row['timeout_hours'])}h"
        )

    return f"""
    <div class="summary">
      <div class="card">
        <h3>🏆 Best Sharpe</h3>
        <div class="big">{best_sharpe['sharpe_annual']:.2f}</div>
        <div class="small">
          PnL: ${best_sharpe['total_pnl_usd']:,.2f} ({best_sharpe['total_pnl_pct']:.1f}%) ·
          MaxDD: {best_sharpe['max_drawdown_pct']:.1f}% ·
          Hit: {best_sharpe['hit_rate']:.1f}% ·
          N: {int(best_sharpe['n_trades'])}
        </div>
        <div class="config">{_fmt_cfg(best_sharpe)}</div>
      </div>
      <div class="card">
        <h3>💰 Best total PnL</h3>
        <div class="big">${best_pnl['total_pnl_usd']:,.2f}</div>
        <div class="small">
          Sharpe: {best_pnl['sharpe_annual']:.2f} ·
          MaxDD: {best_pnl['max_drawdown_pct']:.1f}% ·
          Hit: {best_pnl['hit_rate']:.1f}% ·
          N: {int(best_pnl['n_trades'])}
        </div>
        <div class="config">{_fmt_cfg(best_pnl)}</div>
      </div>
      <div class="card">
        <h3>📊 Sweep</h3>
        <div class="big">{len(sweep_df)}</div>
        <div class="small">combinations tested</div>
        <div class="config">
          {(sweep_df['sharpe_annual'] > 1).sum()} configs with Sharpe > 1<br/>
          {(sweep_df['sharpe_annual'] > 2).sum()} configs with Sharpe > 2<br/>
          Median Sharpe: {sweep_df['sharpe_annual'].median():.2f}
        </div>
      </div>
      <div class="card">
        <h3>📅 Data window</h3>
        <pre class="small">{data_summary}</pre>
      </div>
    </div>
    """


# ─── Main report builder ────────────────────────────────────────────────


def generate_report(
    sweep_df: pd.DataFrame,
    best_trades_df: pd.DataFrame,
    best_eq_df: pd.DataFrame,
    per_coin_df: pd.DataFrame,
    data_summary: str,
    output_path: str | Path = "data/reports/backtest_report.html",
) -> Path:
    """Write the full HTML report. Returns the output Path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Section 2: Heatmaps — 6 most informative pair views ──────────────
    heatmap_figs = [
        # Funding × Z: the "extremity threshold" sweet spot
        _heatmap_pair(sweep_df, "min_funding_apr_pct", "min_funding_zscore",
                      "sharpe_annual"),
        _heatmap_pair(sweep_df, "min_funding_apr_pct", "min_funding_zscore",
                      "total_pnl_usd"),
        # TP × SL: the asymmetric "let winners run" exploration
        _heatmap_pair(sweep_df, "take_profit_pct", "stop_loss_pct",
                      "sharpe_annual",
                      title_suffix=" (TP > SL only)"),
        _heatmap_pair(sweep_df, "take_profit_pct", "stop_loss_pct",
                      "total_pnl_usd",
                      title_suffix=" (TP > SL only)"),
        _heatmap_pair(sweep_df, "take_profit_pct", "stop_loss_pct",
                      "hit_rate",
                      title_suffix=" — hit rate %",
                      colorscale="Viridis"),
        # n_trades: how often we trade per combo
        _heatmap_pair(sweep_df, "min_funding_apr_pct", "persistence_hours",
                      "n_trades", colorscale="Blues"),
    ]

    # ── Section 4: Per-parameter charts (n_trades + Sharpe) ──────────────
    per_param_figs = [
        _trade_count_per_param(sweep_df, p)
        for p in PARAM_COLS if p in sweep_df.columns
    ]

    # ── Section 5: Best config — equity curve, trade dist, exit reasons, per-coin ──
    eq_fig = _equity_curve_fig(best_eq_df, "Equity curve — best Sharpe config")
    dist_fig = _trade_distribution_fig(best_trades_df, "Trade PnL distribution — best Sharpe config")
    exit_fig = _exit_reasons_fig(best_trades_df, "Exit reasons distribution — best Sharpe config")
    coin_fig = _per_coin_fig(per_coin_df)

    # ── Inline HTML ───────────────────────────────────────────────────────
    def _div(fig: go.Figure) -> str:
        return fig.to_html(full_html=False, include_plotlyjs=False, div_id=None)

    html = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>HL Funding Sniper — Backtest Report</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 1400px; margin: 0 auto; padding: 24px;
      background: #fafafa; color: #222;
    }}
    h1 {{ font-size: 28px; margin-bottom: 6px; }}
    h2 {{ font-size: 20px; margin-top: 36px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
    .meta {{ color: #777; font-size: 13px; margin-bottom: 24px; }}
    .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 16px 0; }}
    .card {{
      background: white; border: 1px solid #e5e5e5; border-radius: 8px;
      padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }}
    .card h3 {{ margin: 0 0 8px 0; font-size: 14px; color: #555; }}
    .card .big {{ font-size: 28px; font-weight: 600; color: #2c5fd6; }}
    .card .small {{ font-size: 12px; color: #777; margin: 4px 0; }}
    .card .config {{ font-size: 11px; color: #444; margin-top: 8px; font-family: monospace; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .grid-3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }}
    table.dataframe {{
      border-collapse: collapse; font-size: 12px; width: 100%; margin: 12px 0;
      background: white;
    }}
    table.dataframe th, table.dataframe td {{
      border: 1px solid #e5e5e5; padding: 6px 10px; text-align: right;
    }}
    table.dataframe th {{ background: #f0f0f0; font-weight: 600; }}
    table.dataframe tr:hover td {{ background: #f9f9f9; }}
    pre.small {{ font-size: 11px; margin: 0; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <h1>🎯 HL Funding Sniper — Backtest Report</h1>
  <div class="meta">Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</div>

  <h2>1. Summary</h2>
  {_summary_html(sweep_df, data_summary)}

  <h2>2. Heatmaps — TP × SL and other axes</h2>
  <p style="color:#666; font-size:13px; margin: 4px 0 12px 0;">
    Each cell = BEST value of the metric across all other parameters for that (x, y) pair.
    TP × SL pairs with TP ≤ SL are excluded ("let winners run" constraint).
  </p>
  <div class="grid-3">
    {''.join(f'<div>{_div(f)}</div>' for f in heatmap_figs)}
  </div>

  <h2>3. Top 20 configurations (by Sharpe)</h2>
  {_top_configs_html(sweep_df, n=20)}

  <h2>4. Sensitivity per parameter</h2>
  <div class="grid-2">
    {''.join(f'<div>{_div(f)}</div>' for f in per_param_figs)}
  </div>

  <h2>5. Best configuration — detailed</h2>
  <div class="grid-2">
    <div>{_div(eq_fig)}</div>
    <div>{_div(dist_fig)}</div>
  </div>
  <div class="grid-2">
    <div>{_div(exit_fig)}</div>
    <div>{_div(coin_fig)}</div>
  </div>

</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    log.info("Report written to %s", output_path)
    return output_path
