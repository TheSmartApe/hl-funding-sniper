"""Config loader & validator.

All bot behavior is driven by config.yaml. This module loads it, validates the
schema with Pydantic, and exposes a single ``load_config()`` function.

Secrets (private key, telegram token) are read from environment variables,
NEVER from the YAML file.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
#  Section models
# ─────────────────────────────────────────────────────────────────────────────


class AccountConfig(BaseModel):
    capital_usdc: float = Field(gt=0)
    max_concurrent_positions: int = Field(gt=0, le=20)
    max_total_exposure_pct: float = Field(gt=0, le=100)
    max_position_pct: float = Field(gt=0, le=100)


class UniverseConfig(BaseModel):
    min_open_interest_usd: float = Field(ge=0)
    max_spread_bps: float = Field(gt=0)
    exclude_coins: List[str] = Field(default_factory=list)
    include_only: List[str] = Field(default_factory=list)


class EntryConfig(BaseModel):
    min_funding_apr_pct: float
    persistence_hours: int = Field(ge=1, le=48)
    min_funding_zscore: float
    zscore_lookback_days: int = Field(ge=1, le=365)
    max_premium_bps: float = Field(gt=0)
    direction_mode: Literal["short_high_funding", "both"] = "short_high_funding"


class SizingConfig(BaseModel):
    leverage_majors: int = Field(ge=1, le=50)
    leverage_midcaps: int = Field(ge=1, le=50)
    majors_list: List[str]
    method: Literal["equal", "score_weighted"] = "equal"


class ExitConfig(BaseModel):
    funding_apr_exit_threshold: float
    take_profit_pct: float = Field(gt=0)
    stop_loss_pct: float = Field(gt=0)
    timeout_hours: int = Field(gt=0)
    exit_on_zscore_below: float
    reentry_cooldown_hours: int = Field(ge=0)
    post_stop_size_multiplier: float = Field(gt=0, le=1)


class RiskConfig(BaseModel):
    daily_loss_halt_pct: float = Field(gt=0)
    total_drawdown_kill_pct: float = Field(gt=0)
    margin_ratio_warning: float = Field(gt=0, lt=1)
    margin_ratio_critical: float = Field(gt=0, lt=1)
    max_pct_of_coin_oi: float = Field(gt=0, le=100)


class ExecutionConfig(BaseModel):
    dry_run: bool = True
    order_type: Literal["market", "limit_post"] = "market"
    slippage_tolerance: float = Field(gt=0, lt=1)
    limit_timeout_seconds: int = Field(gt=0)
    retry_attempts: int = Field(ge=1, le=10)
    retry_delay_seconds: int = Field(ge=1)
    use_cross_margin: bool = False
    use_native_triggers: bool = True
    tls_verify: bool = True


class SchedulerConfig(BaseModel):
    tick_interval_seconds: int = Field(ge=60)
    tick_offset_seconds_before_hour: int = Field(ge=0, le=3600)


class HyperliquidConfig(BaseModel):
    network: Literal["mainnet", "testnet"] = "mainnet"
    wallet_address: str = ""


class TelegramConfig(BaseModel):
    enabled: bool = False
    chat_id: str = ""


class NotificationsConfig(BaseModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_file: str = "logs/bot.log"
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    heartbeat: bool = True


# ─────────────────────────────────────────────────────────────────────────────
#  Root config
# ─────────────────────────────────────────────────────────────────────────────


class Config(BaseModel):
    account: AccountConfig
    universe: UniverseConfig
    entry: EntryConfig
    sizing: SizingConfig
    exit: ExitConfig
    risk: RiskConfig
    execution: ExecutionConfig
    scheduler: SchedulerConfig
    hyperliquid: HyperliquidConfig
    notifications: NotificationsConfig

    # Filled at load time from env vars — never present in YAML
    hl_private_key: Optional[str] = Field(default=None, repr=False)
    telegram_bot_token: Optional[str] = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _cross_checks(self) -> "Config":
        # Critical < warning for margin
        if self.risk.margin_ratio_critical >= self.risk.margin_ratio_warning:
            raise ValueError(
                "risk.margin_ratio_critical must be < margin_ratio_warning"
            )
        # Entry funding threshold must be strictly higher than the exit
        # threshold — otherwise every entry would fire the exit rule on the
        # very next tick (open at 11% → exit threshold 15% → close immediately).
        if (
            self.entry.min_funding_apr_pct
            <= self.exit.funding_apr_exit_threshold
        ):
            raise ValueError(
                f"entry.min_funding_apr_pct ({self.entry.min_funding_apr_pct}) "
                f"must be > exit.funding_apr_exit_threshold "
                f"({self.exit.funding_apr_exit_threshold}); otherwise positions "
                f"would be closed on the tick after they open."
            )
        # Total exposure cap should be coherent with per-position cap
        if (
            self.account.max_position_pct
            * self.account.max_concurrent_positions
            < self.account.max_total_exposure_pct
        ):
            # not strictly an error, but bot will never reach total cap
            pass
        # If not dry_run, wallet & private key are required
        if not self.execution.dry_run:
            if not self.hyperliquid.wallet_address:
                raise ValueError(
                    "hyperliquid.wallet_address required when dry_run=false"
                )
            if not self.hl_private_key:
                raise ValueError(
                    "HL_PRIVATE_KEY env var required when dry_run=false"
                )
        # Telegram
        if self.notifications.telegram.enabled and not self.telegram_bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN env var required when telegram.enabled=true"
            )
        return self


# ─────────────────────────────────────────────────────────────────────────────
#  Public loader
# ─────────────────────────────────────────────────────────────────────────────


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load and validate config from YAML, enriched with env-var secrets."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p.resolve()}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Inject secrets from env (never from YAML)
    raw["hl_private_key"] = os.environ.get("HL_PRIVATE_KEY") or None
    raw["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or None

    return Config.model_validate(raw)
