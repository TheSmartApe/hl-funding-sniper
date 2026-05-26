"""Hyperliquid market-data client.

Thin wrapper around the official ``hyperliquid-python-sdk`` Info API that
returns clean, strategy-ready data structures. Caches funding history per coin
between ticks to limit API calls.

Funding & open-interest semantics:
    - HL `funding` is a per-hour rate (e.g. 0.0001 = 0.01%/hour → ~87.6% APR)
    - We always work in annualized % (APR) internally: ``funding * 24 * 365 * 100``
    - `openInterest` is in base units of the coin; multiply by mark price for USD
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# NOTE: hyperliquid SDK is imported lazily inside HLDataClient.__init__ so we
# can conditionally disable TLS verification BEFORE the SDK creates its session.

log = logging.getLogger(__name__)


def _disable_tls_verification_globally() -> None:
    """Patch requests + suppress urllib3 warnings BEFORE importing the HL SDK.

    Required on machines behind a corporate MITM proxy. After this runs, every
    requests.Session created from then on will have verify=False by default.
    """
    import requests  # noqa: WPS433
    import urllib3  # noqa: WPS433

    _orig_init = requests.sessions.Session.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        self.verify = False

    requests.sessions.Session.__init__ = _patched_init  # type: ignore[assignment]
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─────────────────────────────────────────────────────────────────────────────
#  Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MarketSnapshot:
    """Per-coin market state at a given moment."""
    coin: str
    funding_apr_pct: float           # annualized %, signed
    open_interest_usd: float
    mark_px: float
    oracle_px: float
    mid_px: float
    premium_bps: float               # (mark - oracle) / oracle * 10000
    impact_bid_px: Optional[float] = None
    impact_ask_px: Optional[float] = None
    spread_bps: Optional[float] = None  # (impact_ask - impact_bid) / mid * 10000
    day_ntl_volume_usd: float = 0.0


@dataclass
class FundingHistoryCache:
    """Per-coin funding history (annualized %) sorted oldest → newest."""
    coin: str
    history_apr: List[float] = field(default_factory=list)
    last_update_ts: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  Client
# ─────────────────────────────────────────────────────────────────────────────


class HLDataClient:
    """Read-only HL market data client."""

    def __init__(self, network: str = "mainnet", tls_verify: bool = True):
        if not tls_verify:
            _disable_tls_verification_globally()
            log.warning(
                "TLS verification DISABLED (config.execution.tls_verify=false). "
                "Use ONLY on machines behind a trusted corporate MITM proxy."
            )
        # Import HL SDK AFTER the optional TLS patch so the Info session
        # inherits verify=False from the patched Session.__init__.
        from hyperliquid.info import Info  # noqa: WPS433
        from hyperliquid.utils import constants  # noqa: WPS433

        url = (
            constants.MAINNET_API_URL if network == "mainnet"
            else constants.TESTNET_API_URL
        )
        self.info = Info(url, skip_ws=True)
        self._funding_cache: Dict[str, FundingHistoryCache] = {}
        self._cache_ttl_seconds = 3300  # refresh ~55 min — well under tick interval

    # ── universe & snapshots ──────────────────────────────────────────────

    def snapshot_all(self) -> List[MarketSnapshot]:
        """Pull a snapshot of all perp markets in one API call."""
        try:
            meta, ctxs = self.info.meta_and_asset_ctxs()
        except Exception as e:
            log.error("meta_and_asset_ctxs failed: %s", e)
            raise

        universe = meta.get("universe", [])
        snapshots: List[MarketSnapshot] = []
        for asset, ctx in zip(universe, ctxs):
            coin = asset.get("name")
            if not coin or not ctx:
                continue
            try:
                snap = self._build_snapshot(coin, ctx)
                if snap is not None:
                    snapshots.append(snap)
            except (ValueError, KeyError, TypeError) as e:
                log.debug("snapshot skip %s: %s", coin, e)
        log.debug("snapshot_all → %d coins", len(snapshots))
        return snapshots

    @staticmethod
    def _build_snapshot(coin: str, ctx: dict) -> Optional[MarketSnapshot]:
        # ctx fields: funding, openInterest, markPx, oraclePx, midPx, impactPxs,
        # dayNtlVlm, premium, ...
        def _f(key: str, default=None) -> Optional[float]:
            v = ctx.get(key)
            return float(v) if v is not None else default

        funding = _f("funding", 0.0)
        mark = _f("markPx", 0.0)
        oracle = _f("oraclePx", 0.0)
        mid = _f("midPx", mark)
        oi_base = _f("openInterest", 0.0)
        day_vlm = _f("dayNtlVlm", 0.0)

        if not mark or mark <= 0:
            return None

        premium_bps = (
            ((mark - oracle) / oracle * 10_000) if (oracle and oracle > 0) else 0.0
        )

        impact_bid, impact_ask, spread_bps = None, None, None
        impact = ctx.get("impactPxs")
        if isinstance(impact, list) and len(impact) >= 2:
            try:
                impact_bid = float(impact[0])
                impact_ask = float(impact[1])
                if mid and mid > 0:
                    spread_bps = (impact_ask - impact_bid) / mid * 10_000
            except (ValueError, TypeError):
                pass

        return MarketSnapshot(
            coin=coin,
            funding_apr_pct=funding * 24 * 365 * 100,
            open_interest_usd=oi_base * mark,
            mark_px=mark,
            oracle_px=oracle,
            mid_px=mid,
            premium_bps=premium_bps,
            impact_bid_px=impact_bid,
            impact_ask_px=impact_ask,
            spread_bps=spread_bps,
            day_ntl_volume_usd=day_vlm,
        )

    # ── funding history (for z-score & persistence checks) ────────────────

    def get_funding_history_apr(
        self, coin: str, lookback_days: int
    ) -> List[float]:
        """Return funding history (in annualized %, oldest → newest)."""
        now = time.time()
        cached = self._funding_cache.get(coin)
        if cached and (now - cached.last_update_ts) < self._cache_ttl_seconds:
            return cached.history_apr

        start_ms = int((now - lookback_days * 86_400) * 1000)
        try:
            raw = self.info.funding_history(name=coin, startTime=start_ms)
        except Exception as e:
            log.warning("funding_history(%s) failed: %s", coin, e)
            return cached.history_apr if cached else []

        # Each row: {"coin": ..., "fundingRate": "0.0001", "time": ms, ...}
        try:
            series_apr = sorted(
                ((int(r["time"]), float(r["fundingRate"])) for r in raw),
                key=lambda x: x[0],
            )
            history_apr = [fr * 24 * 365 * 100 for _, fr in series_apr]
        except (KeyError, ValueError, TypeError) as e:
            log.warning("funding_history parse failed for %s: %s", coin, e)
            history_apr = []

        self._funding_cache[coin] = FundingHistoryCache(
            coin=coin, history_apr=history_apr, last_update_ts=now
        )
        return history_apr

    # ── account / margin (used by risk_manager) ───────────────────────────

    def get_user_state(self, address: str) -> dict:
        """Raw HL user state — assetPositions, marginSummary, withdrawable, ..."""
        try:
            return self.info.user_state(address)
        except Exception as e:
            log.error("user_state failed for %s: %s", address, e)
            raise

    def get_account_value_usd(self, address: str) -> float:
        """Total account equity in USD (marginSummary.accountValue)."""
        state = self.get_user_state(address)
        try:
            return float(state["marginSummary"]["accountValue"])
        except (KeyError, ValueError, TypeError):
            return 0.0

    def get_margin_ratio(self, address: str) -> float:
        """Used margin / account value, in [0, 1]. 0 = no positions."""
        state = self.get_user_state(address)
        try:
            mm = float(state["marginSummary"]["totalMarginUsed"])
            av = float(state["marginSummary"]["accountValue"])
            return mm / av if av > 0 else 0.0
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            return 0.0

    def get_open_position_coins(self, address: str) -> List[str]:
        """List of coins on which the user currently holds a non-zero position."""
        state = self.get_user_state(address)
        out: List[str] = []
        for ap in state.get("assetPositions", []):
            p = ap.get("position", {})
            try:
                if float(p.get("szi", 0)) != 0:
                    coin = p.get("coin")
                    if coin:
                        out.append(coin)
            except (ValueError, TypeError):
                continue
        return out

    def get_user_fills(self, address: str) -> list:
        """Recent fills for the user. Each fill: {coin, px, sz, side, time, oid, dir, ...}.

        Order is newest-first when returned by the HL API.
        """
        try:
            return self.info.user_fills(address)
        except Exception as e:
            log.warning("user_fills failed for %s: %s", address, e)
            return []

    def find_close_fill(
        self, address: str, coin: str, since_ms: int,
        sl_oid: Optional[int] = None, tp_oid: Optional[int] = None,
    ) -> Optional[dict]:
        """Find the fill that closed a position. Prefer the one matching sl_oid/tp_oid.

        Returns the fill dict or None if not found in the recent window.
        """
        fills = self.get_user_fills(address)
        # First try OID match (most accurate)
        for f in fills:
            try:
                oid = int(f.get("oid", 0))
                if oid > 0 and oid in (sl_oid or -1, tp_oid or -1):
                    return f
            except (ValueError, TypeError):
                continue
        # Fallback: latest fill on this coin since since_ms (any close = buy after short)
        for f in fills:
            try:
                if f.get("coin") != coin:
                    continue
                if int(f.get("time", 0)) < since_ms:
                    continue
                return f
            except (ValueError, TypeError):
                continue
        return None
