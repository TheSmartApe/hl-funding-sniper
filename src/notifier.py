"""Logging + Telegram + delegation to rich UI for visible events.

setup_logging() wires:
  - RichHandler for the console (colored, structured logs)
  - RotatingFileHandler for logs/bot.log (plain text, grep-friendly)

The Notifier class is the single gateway for all bot events. It:
  - prints rich panels via the ui module (always)
  - sends Telegram alerts (if enabled)
  - relies on RichHandler for the log line
"""
from __future__ import annotations

import logging
import logging.handlers
import urllib.parse
import urllib.request
from pathlib import Path

from rich.logging import RichHandler

from . import ui
from .config import Config

log = logging.getLogger(__name__)


# ─── Logging setup ────────────────────────────────────────────────────────


def setup_logging(cfg: Config) -> None:
    """Configure root logger: rich for console, plain rotating file for disk."""
    log_path = Path(cfg.notifications.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, cfg.notifications.log_level)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    # --- Console: rich, no timestamps (rich adds them via its formatter) ---
    rich_h = RichHandler(
        console=ui.console,
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        markup=True,
        omit_repeated_times=False,
        log_time_format="[%H:%M:%S]",
    )
    rich_h.setLevel(level)
    rich_h.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(rich_h)

    # --- File: plain text rotating, full format ---
    file_h = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8",
    )
    file_h.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    file_h.setLevel(level)
    root.addHandler(file_h)

    # Silence noisy third-party loggers regardless of level
    for noisy in ("urllib3", "hyperliquid", "rlp", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ─── Notifier ─────────────────────────────────────────────────────────────


class Notifier:
    """Single gateway for all bot-visible events."""

    def __init__(self, cfg: Config):
        tg = cfg.notifications.telegram
        self.telegram_enabled = tg.enabled
        self.chat_id = tg.chat_id
        self.token = cfg.telegram_bot_token
        self.observe_only = getattr(cfg.execution, "observe_only", False)
        self.dry_run = cfg.execution.dry_run
        if self.observe_only:
            self.mode_tag = "OBSERVE-ONLY"
        elif self.dry_run:
            self.mode_tag = "DRY-RUN"
        else:
            self.mode_tag = "LIVE"

    # ── Telegram helper ────────────────────────────────────────────────

    def _telegram(self, text: str) -> None:
        if not self.telegram_enabled or not self.token or not self.chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status != 200:
                    log.warning("Telegram non-200: %s", r.status)
        except Exception as e:
            log.warning("Telegram send failed: %s", e)

    # ── Event API ──────────────────────────────────────────────────────

    def boot(self, cfg: Config) -> None:
        ui.print_banner(cfg)
        self._telegram(
            f"*Bot started* mode=`{self.mode_tag}` network=`{cfg.hyperliquid.network}`"
        )

    def entry(
        self,
        coin: str,
        notional: float,
        leverage: int,
        entry_price: float,
        funding_apr: float,
        zscore: float,
        reason: str,
    ) -> None:
        ui.print_entry(
            coin, notional, leverage, entry_price,
            funding_apr, zscore, reason, mode_tag=self.mode_tag,
        )
        self._telegram(
            f"*ENTRY* `{coin}` SHORT\n"
            f"  notional: `${notional:,.2f}`\n"
            f"  leverage: `{leverage}x`\n"
            f"  funding: `{funding_apr:+.1f}%` APR\n"
            f"  z-score: `{zscore:+.2f}`\n"
            f"  reason: {reason}"
        )

    def exit(
        self,
        coin: str,
        reason: str,
        price_pnl: float,
        funding_collected: float,
    ) -> None:
        ui.print_exit(coin, reason, price_pnl, funding_collected, mode_tag=self.mode_tag)
        total = price_pnl + funding_collected
        emoji = "🟢" if total >= 0 else "🔴"
        self._telegram(
            f"{emoji} *EXIT* `{coin}` ({reason})\n"
            f"  price PnL: `${price_pnl:+,.2f}`\n"
            f"  funding: `${funding_collected:+,.4f}`\n"
            f"  *total: `${total:+,.2f}`*"
        )

    def potential_entry(
        self,
        coin: str,
        notional: float,
        leverage: int,
        entry_price: float,
        funding_apr: float,
        zscore: float,
        reason: str,
    ) -> None:
        ui.print_potential_entry(
            coin, notional, leverage, entry_price, funding_apr, zscore, reason,
        )
        self._telegram(
            f"👁 *[OBSERVE]* would SHORT `{coin}` "
            f"${notional:,.0f} {leverage}x — funding {funding_apr:+.1f}% z={zscore:+.2f}"
        )

    def potential_exit(
        self, coin: str, reason: str, est_price_pnl: float, est_funding: float,
    ) -> None:
        ui.print_potential_exit(coin, reason, est_price_pnl, est_funding)
        self._telegram(
            f"👁 *[OBSERVE]* would close `{coin}` ({reason}) "
            f"est PnL ${est_price_pnl + est_funding:+,.2f}"
        )

    def heartbeat(
        self,
        n_eligible: int,
        n_open: int,
        n_signals: int,
        capital: float,
        halted: bool,
        halt_reason: str = None,
    ) -> None:
        ui.print_heartbeat(n_eligible, n_open, n_signals, capital, halted, halt_reason)
        status = "HALTED" if halted else "OK"
        self._telegram(
            f"_heartbeat — {status}_\n"
            f"  eligible: `{n_eligible}` open: `{n_open}` "
            f"signals: `{n_signals}` capital: `${capital:,.2f}`"
        )

    def halt(self, reason: str) -> None:
        ui.print_halt(reason)
        self._telegram(f"🛑 *CIRCUIT BREAKER* — {reason}")

    def error(self, where: str, err: str) -> None:
        ui.err(f"[{where}] {err}")
        self._telegram(f"❌ *ERROR* in `{where}`\n  `{err}`")
