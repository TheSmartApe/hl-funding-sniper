"""Historical data ingestion from Dune `hyperliquid.market_data`.

Pulls hourly aggregates per coin with intra-hour hi/lo so the backtest engine
can detect SL/TP triggers on wicks. Caches as Parquet locally.

Schema of the cached Parquet:
    hour          datetime    (UTC, top of hour)
    coin          str
    funding_apr   float       (annualized %, signed)
    open_interest_usd float
    mark_px       float       (avg over hour)
    mark_hi       float       (max over hour — for SL of shorts / TP of longs)
    mark_lo       float       (min over hour — for TP of shorts / SL of longs)
    oracle_px     float
    mid_px        float
    spread_bps    float
    premium_bps   float
    day_vlm_usd   float
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional  # noqa: F401

import pandas as pd

log = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class FetchSpec:
    """Specification of a Dune data fetch.

    - `coins`: explicit list of coins. Empty/None = ALL coins on HL.
    - `min_oi_filter_usd`: SQL-level filter to drop rows where OI is too small,
      to keep the dataset reasonable for "all coins" mode.
    """
    coins: Optional[List[str]] = None     # None or [] = all coins
    start_date: str = ""                  # 'YYYY-MM-DD' inclusive
    end_date: str = ""                    # 'YYYY-MM-DD' exclusive
    min_oi_filter_usd: float = 1_000_000  # SQL pre-filter on OI
    cache_file: Path = None

    def __post_init__(self):
        if self.cache_file is None:
            if self.coins:
                coins_tag = "_".join(sorted(self.coins))[:60]
            else:
                coins_tag = f"all_oi{int(self.min_oi_filter_usd/1e6)}M"
            self.cache_file = CACHE_DIR / (
                f"hl_market_{self.start_date}_{self.end_date}_{coins_tag}.parquet"
            )


# Sensible default universe: top liquid majors + active midcaps on HL.
# These are the coins that typically have OI > $5M and tight spreads.
DEFAULT_COINS = [
    "BTC", "ETH", "SOL", "BNB", "AVAX", "DOGE", "LINK", "SUI",
    "AAVE", "LTC", "ADA", "HYPE", "XRP", "ARB", "OP", "NEAR",
    "INJ", "TIA", "BCH", "TRX", "ZRO", "ZEC", "ENA", "WLD",
    "RENDER", "PEPE", "kPEPE", "kBONK", "TAO", "ATOM",
]


# ─── Dune query template ─────────────────────────────────────────────────


_QUERY_TEMPLATE = """
WITH window_data AS (
    SELECT *
    FROM hyperliquid.market_data
    WHERE time >= TIMESTAMP '{start}'
      AND time <  TIMESTAMP '{end}'
      {coin_filter}
),
hourly AS (
    SELECT
        date_trunc('hour', time)                                AS hour,
        coin,
        AVG(funding) * 24 * 365 * 100                           AS funding_apr,
        AVG(open_interest * mark_px)                            AS open_interest_usd,
        AVG(mark_px)                                            AS mark_px,
        MAX(mark_px)                                            AS mark_hi,
        MIN(mark_px)                                            AS mark_lo,
        AVG(oracle_px)                                          AS oracle_px,
        AVG(mid_px)                                             AS mid_px,
        AVG((impact_ask_px - impact_bid_px)
            / NULLIF(mid_px, 0) * 10000)                        AS spread_bps,
        AVG((mark_px - oracle_px)
            / NULLIF(oracle_px, 0) * 10000)                     AS premium_bps,
        AVG(day_ntl_vlm)                                        AS day_vlm_usd
    FROM window_data
    GROUP BY 1, 2
),
coins_with_liquidity AS (
    -- Keep only coins that had OI > min at least 20% of the hours in window
    SELECT coin
    FROM hourly
    GROUP BY coin
    HAVING SUM(CASE WHEN open_interest_usd >= {min_oi} THEN 1 ELSE 0 END)
           > 0.2 * COUNT(*)
)
SELECT h.*
FROM hourly h
JOIN coins_with_liquidity c ON h.coin = c.coin
ORDER BY h.hour, h.coin
"""


def _build_query(spec: FetchSpec) -> str:
    if spec.coins:
        coin_list = ", ".join(f"'{c}'" for c in spec.coins)
        coin_filter = f"AND coin IN ({coin_list})"
    else:
        coin_filter = ""    # all coins
    return _QUERY_TEMPLATE.format(
        start=spec.start_date,
        end=spec.end_date,
        coin_filter=coin_filter,
        min_oi=spec.min_oi_filter_usd,
    )


# ─── Dune CLI runner ────────────────────────────────────────────────────


def _run_dune_query(sql: str) -> List[dict]:
    """Execute SQL via the Dune CLI and parse the JSON result."""
    log.info("Submitting Dune query (%d chars)...", len(sql))
    proc = subprocess.run(
        ["dune", "query", "run-sql", "--sql", sql, "-o", "json",
         "--performance", "medium"],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"dune CLI failed: {proc.stderr[:500]}")
    payload = json.loads(proc.stdout)
    if payload.get("state") != "QUERY_STATE_COMPLETED":
        raise RuntimeError(f"Query not completed: {payload.get('state')}")
    rows = payload.get("result", {}).get("rows", [])
    log.info("Dune returned %d rows", len(rows))
    return rows


# ─── Public API ─────────────────────────────────────────────────────────


def fetch(spec: FetchSpec, force: bool = False) -> pd.DataFrame:
    """Fetch (or load from cache) the historical hourly data for ``spec``.

    Returns a DataFrame with the schema documented at the top of this module.
    """
    if spec.cache_file.exists() and not force:
        log.info("Loading cached data from %s", spec.cache_file)
        return pd.read_parquet(spec.cache_file)

    sql = _build_query(spec)
    rows = _run_dune_query(sql)
    if not rows:
        raise RuntimeError("Dune returned 0 rows — check date range and coins")

    df = pd.DataFrame(rows)
    # Normalise types
    df["hour"] = pd.to_datetime(df["hour"], utc=True)
    numeric_cols = [
        "funding_apr", "open_interest_usd", "mark_px", "mark_hi", "mark_lo",
        "oracle_px", "mid_px", "spread_bps", "premium_bps", "day_vlm_usd",
    ]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.sort_values(["hour", "coin"]).reset_index(drop=True)
    df.to_parquet(spec.cache_file, index=False)
    log.info("Cached %d rows to %s", len(df), spec.cache_file)
    return df


def summary(df: pd.DataFrame) -> str:
    """Human-readable summary of an ingested DataFrame."""
    if df.empty:
        return "empty"
    coins = df["coin"].nunique()
    hours = df["hour"].nunique()
    lines = [
        f"  Coins:       {coins}",
        f"  Hours:       {hours} ({df['hour'].min()} → {df['hour'].max()})",
        f"  Rows:        {len(df):,}",
        f"  Mean OI:     ${df['open_interest_usd'].mean()/1e6:.1f}M",
        f"  Mean spread: {df['spread_bps'].mean():.1f} bps",
    ]
    return "\n".join(lines)
