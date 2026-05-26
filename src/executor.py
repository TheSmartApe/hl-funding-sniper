"""Order execution on Hyperliquid.

Wraps the official ``hyperliquid-python-sdk`` Exchange API. Provides:

  - open_short(coin, notional_usd, leverage)
  - close_position(coin)
  - dry_run mode (no real orders, just logs + fake fills)

All public methods return a ``FillResult`` with the realized fill data
(or a synthetic one in dry-run).

Sizing rounding follows HL's per-asset szDecimals from the universe meta.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

# NOTE: hyperliquid SDK is imported lazily inside HLExecutor.__init__ so we
# can conditionally disable TLS verification BEFORE the SDK creates its session.

from .config import Config
from .data_client import _disable_tls_verification_globally

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Result type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FillResult:
    success: bool
    coin: str
    side: str              # "buy" | "sell"
    size_base: float       # filled size in base coin units
    avg_price: float
    notional_usd: float
    fee_usd: float = 0.0
    raw: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class TriggerResult:
    """Result of placing or cancelling a trigger order."""
    success: bool
    coin: str
    oid: Optional[int] = None    # HL order id (None on failure or dry-run)
    raw: Optional[dict] = None
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
#  Executor
# ─────────────────────────────────────────────────────────────────────────────


class HLExecutor:
    """Order placement on Hyperliquid (perp markets only)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dry_run = cfg.execution.dry_run

        if not cfg.execution.tls_verify:
            _disable_tls_verification_globally()
        # Import HL SDK lazily so optional TLS patch applies first
        from hyperliquid.exchange import Exchange  # noqa: WPS433
        from hyperliquid.info import Info  # noqa: WPS433
        from hyperliquid.utils import constants  # noqa: WPS433

        url = (
            constants.MAINNET_API_URL if cfg.hyperliquid.network == "mainnet"
            else constants.TESTNET_API_URL
        )
        self.info = Info(url, skip_ws=True)

        self.exchange = None
        if not self.dry_run:
            # eth_account import is deferred until we actually need to sign
            from eth_account import Account  # noqa: WPS433
            assert cfg.hl_private_key, "HL_PRIVATE_KEY required when dry_run=false"
            wallet = Account.from_key(cfg.hl_private_key)
            self.exchange = Exchange(wallet, url)
            log.info("Exchange initialized for wallet %s on %s",
                     cfg.hyperliquid.wallet_address, cfg.hyperliquid.network)
        else:
            log.warning("DRY-RUN mode: no real orders will be placed")

        # Cache asset metadata for size rounding
        self._asset_meta: dict = {}
        self._refresh_asset_meta()

    # ── asset metadata ─────────────────────────────────────────────────────

    def _refresh_asset_meta(self) -> None:
        try:
            meta = self.info.meta()
            for i, a in enumerate(meta.get("universe", [])):
                self._asset_meta[a["name"]] = {
                    "idx": i,
                    "szDecimals": int(a.get("szDecimals", 4)),
                    "maxLeverage": int(a.get("maxLeverage", 1)),
                }
        except Exception as e:
            log.error("meta refresh failed: %s", e)

    def _round_size(self, coin: str, size_base: float) -> float:
        decimals = self._asset_meta.get(coin, {}).get("szDecimals", 4)
        factor = 10 ** decimals
        return int(size_base * factor) / factor

    def _round_price(self, coin: str, price: float) -> float:
        """Round price to HL's accepted precision.

        HL rule: prices have at most 5 significant figures AND at most
        (6 - szDecimals) decimal places. We apply both constraints.
        """
        if price <= 0:
            return price
        sz_decimals = self._asset_meta.get(coin, {}).get("szDecimals", 4)
        max_decimals = max(0, 6 - sz_decimals)
        # First, round to 5 significant figures
        import math  # noqa: WPS433
        exponent = math.floor(math.log10(abs(price)))
        sig_decimals = max(0, 4 - exponent)   # 5 sig figs → keep (5-1-exp) decimals
        decimals = min(sig_decimals, max_decimals)
        factor = 10 ** decimals
        return round(price * factor) / factor

    def _max_leverage(self, coin: str) -> int:
        return self._asset_meta.get(coin, {}).get("maxLeverage", 1)

    # ── public ─────────────────────────────────────────────────────────────

    def set_leverage(self, coin: str, leverage: int) -> bool:
        """Set per-coin leverage. Idempotent."""
        cap = self._max_leverage(coin)
        lev = min(leverage, cap)
        if lev <= 0:
            return False
        if self.dry_run:
            log.info("[DRY] set_leverage %s -> %dx (cap %dx)", coin, lev, cap)
            return True
        try:
            self.exchange.update_leverage(  # type: ignore[union-attr]
                lev, coin, is_cross=self.cfg.execution.use_cross_margin
            )
            return True
        except Exception as e:
            log.error("update_leverage %s failed: %s", coin, e)
            return False

    def open_short(
        self, coin: str, notional_usd: float, leverage: int, mark_px: float,
    ) -> FillResult:
        """Open a SHORT perp position of size ``notional_usd`` at market."""
        if notional_usd <= 0 or mark_px <= 0:
            return FillResult(False, coin, "sell", 0, 0, 0,
                              error="invalid notional or mark")

        size_base = self._round_size(coin, notional_usd / mark_px)
        if size_base <= 0:
            return FillResult(False, coin, "sell", 0, 0, 0,
                              error="size_base rounds to 0")

        if not self.set_leverage(coin, leverage):
            return FillResult(False, coin, "sell", 0, 0, 0,
                              error="set_leverage failed")

        if self.dry_run:
            # Simulate immediate fill at mark with no slippage
            log.info("[DRY] OPEN SHORT %s size=%.6f notional=$%.2f @ %.6f lev=%dx",
                     coin, size_base, notional_usd, mark_px, leverage)
            return FillResult(
                True, coin, "sell", size_base, mark_px,
                size_base * mark_px,
            )

        try:
            res = self.exchange.market_open(  # type: ignore[union-attr]
                name=coin,
                is_buy=False,
                sz=size_base,
                px=None,
                slippage=self.cfg.execution.slippage_tolerance,
            )
            return self._parse_fill(coin, "sell", res)
        except Exception as e:
            log.error("market_open(short) %s failed: %s", coin, e)
            return FillResult(False, coin, "sell", 0, 0, 0, error=str(e))

    def close_position(self, coin: str, mark_px: float) -> FillResult:
        """Close ANY open perp position on `coin` at market (reduceOnly)."""
        if self.dry_run:
            log.info("[DRY] CLOSE %s @ %.6f", coin, mark_px)
            return FillResult(True, coin, "buy", 0, mark_px, 0)
        try:
            res = self.exchange.market_close(coin)  # type: ignore[union-attr]
            return self._parse_fill(coin, "buy", res)
        except Exception as e:
            log.error("market_close %s failed: %s", coin, e)
            return FillResult(False, coin, "buy", 0, 0, 0, error=str(e))

    # ── trigger orders (native SL/TP) ──────────────────────────────────────

    def place_trigger_sl(
        self,
        coin: str,
        position_direction: str,
        size_base: float,
        trigger_px: float,
    ) -> TriggerResult:
        """Place a reduce-only STOP-LOSS trigger order on HL.

        For a SHORT position: trigger fires when mark crosses ABOVE trigger_px
        and we BUY back at market.
        For a LONG position: trigger fires when mark crosses BELOW trigger_px
        and we SELL at market.
        """
        return self._place_trigger(coin, position_direction, size_base, trigger_px, "sl")

    def place_trigger_tp(
        self,
        coin: str,
        position_direction: str,
        size_base: float,
        trigger_px: float,
    ) -> TriggerResult:
        """Place a reduce-only TAKE-PROFIT trigger order on HL.

        For a SHORT: fires when mark crosses BELOW trigger_px → BUY market.
        For a LONG: fires when mark crosses ABOVE trigger_px → SELL market.
        """
        return self._place_trigger(coin, position_direction, size_base, trigger_px, "tp")

    def _place_trigger(
        self,
        coin: str,
        position_direction: str,
        size_base: float,
        trigger_px: float,
        tpsl: str,
    ) -> TriggerResult:
        # Closing direction
        is_buy_for_close = (position_direction == "short")
        side_label = "buy" if is_buy_for_close else "sell"

        # Limit price on the trigger fill — use the configured slippage tolerance.
        # For a BUY close, we accept paying up to trigger × (1 + slip). For a SELL, the inverse.
        slip = self.cfg.execution.slippage_tolerance
        limit_px_raw = (
            trigger_px * (1 + slip) if is_buy_for_close
            else trigger_px * (1 - slip)
        )
        limit_px = self._round_price(coin, limit_px_raw)
        trigger_px_r = self._round_price(coin, trigger_px)
        size_base_r = self._round_size(coin, size_base)

        if size_base_r <= 0:
            return TriggerResult(False, coin, error="size rounds to zero")

        if self.dry_run:
            log.info(
                "[DRY] %s trigger %s %s size=%.6f trigger=%.6f limit=%.6f",
                tpsl.upper(), side_label, coin, size_base_r, trigger_px_r, limit_px,
            )
            # Dry-run: synthetic oid so DB tracking works the same
            return TriggerResult(True, coin, oid=-1)

        try:
            res = self.exchange.order(  # type: ignore[union-attr]
                name=coin,
                is_buy=is_buy_for_close,
                sz=size_base_r,
                limit_px=limit_px,
                order_type={"trigger": {
                    "isMarket": True,
                    "triggerPx": trigger_px_r,
                    "tpsl": tpsl,
                }},
                reduce_only=True,
            )
            return self._parse_trigger(coin, res)
        except Exception as e:
            log.error("place_trigger_%s %s failed: %s", tpsl, coin, e)
            return TriggerResult(False, coin, error=str(e))

    def cancel_order(self, coin: str, oid) -> TriggerResult:
        """Cancel a single open order by HL order id (or synthetic dry-run id)."""
        if oid is None:
            return TriggerResult(True, coin, oid=oid)
        if self.dry_run:
            # Always log in dry-run so the cancel intent is visible
            log.info("[DRY] CANCEL %s oid=%s", coin, oid)
            return TriggerResult(True, coin, oid=oid)
        if oid <= 0:
            # Live mode: invalid oid, nothing to cancel
            return TriggerResult(True, coin, oid=oid)
        try:
            res = self.exchange.cancel(coin, oid)  # type: ignore[union-attr]
            ok = isinstance(res, dict) and res.get("status") == "ok"
            return TriggerResult(ok, coin, oid=oid, raw=res,
                                 error=None if ok else "cancel non-ok")
        except Exception as e:
            log.error("cancel %s oid=%s failed: %s", coin, oid, e)
            return TriggerResult(False, coin, oid=oid, error=str(e))

    def cancel_triggers(
        self, coin: str, sl_oid: Optional[int], tp_oid: Optional[int],
    ) -> tuple:
        """Cancel both SL and TP triggers for a coin. Returns (sl_result, tp_result)."""
        sl_res = self.cancel_order(coin, sl_oid) if sl_oid else TriggerResult(True, coin)
        tp_res = self.cancel_order(coin, tp_oid) if tp_oid else TriggerResult(True, coin)
        return sl_res, tp_res

    def _parse_trigger(self, coin: str, res: dict) -> TriggerResult:
        """Extract oid from a trigger-order placement response."""
        try:
            if res.get("status") != "ok":
                return TriggerResult(False, coin, raw=res,
                                     error=res.get("response") or "non-ok status")
            statuses = res["response"]["data"]["statuses"]
            for s in statuses:
                # Trigger orders sit in 'resting' state until triggered
                if "resting" in s:
                    return TriggerResult(True, coin, oid=int(s["resting"]["oid"]), raw=res)
                if "filled" in s:
                    # Edge case: triggered immediately on placement
                    return TriggerResult(True, coin, oid=int(s["filled"].get("oid", 0)), raw=res)
            return TriggerResult(False, coin, raw=res,
                                 error="no resting/filled status in response")
        except (KeyError, ValueError, TypeError) as e:
            return TriggerResult(False, coin, raw=res, error=f"parse error: {e}")

    # ── parsing ────────────────────────────────────────────────────────────

    def _parse_fill(self, coin: str, side: str, res: dict) -> FillResult:
        """Parse the HL Exchange response into a clean FillResult."""
        try:
            if res.get("status") != "ok":
                return FillResult(False, coin, side, 0, 0, 0,
                                  raw=res, error=res.get("response") or "non-ok status")
            data = res["response"]["data"]
            statuses = data.get("statuses", [])
            total_sz = 0.0
            total_notional = 0.0
            for s in statuses:
                if "filled" in s:
                    f = s["filled"]
                    sz = float(f["totalSz"])
                    px = float(f["avgPx"])
                    total_sz += sz
                    total_notional += sz * px
            avg_px = (total_notional / total_sz) if total_sz > 0 else 0.0
            return FillResult(
                success=total_sz > 0,
                coin=coin, side=side,
                size_base=total_sz, avg_price=avg_px,
                notional_usd=total_notional, raw=res,
            )
        except (KeyError, ValueError, TypeError) as e:
            return FillResult(False, coin, side, 0, 0, 0,
                              raw=res, error=f"parse error: {e}")

    # ── retry wrapper ──────────────────────────────────────────────────────

    def with_retries(self, fn, *args, **kwargs) -> FillResult:
        """Call a fill-returning fn with the configured retry policy."""
        last: Optional[FillResult] = None
        for attempt in range(self.cfg.execution.retry_attempts):
            res = fn(*args, **kwargs)
            if res.success:
                return res
            last = res
            log.warning("attempt %d failed: %s", attempt + 1, res.error)
            time.sleep(self.cfg.execution.retry_delay_seconds)
        return last or FillResult(False, "?", "?", 0, 0, 0, error="all retries failed")
