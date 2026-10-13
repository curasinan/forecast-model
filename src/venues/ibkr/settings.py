"""IBKR venue configuration (env-driven, prefix ``IBKR_``)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class IBKRSettings(BaseSettings):
    """Connection, data, and verified-fact settings for the IBKR adapter."""

    model_config = SettingsConfigDict(env_prefix="IBKR_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    # REQUIRED, no default: Gateway 4001 live / 4002 paper, TWS 7496 / 7497.
    port: int
    trading_mode: Literal["paper", "live"]
    data_root: str = ""
    commission_per_contract: float | None = None
    fees_verified_on: date | None = None
    coupon_apy: float | None = None
    coupon_verified_on: date | None = None
    max_data_lines: int = 100
    record_interval_minutes: int = 10
    market_data_type: int = 4
    restart_window_local: str = "06:45-08:15"

    @property
    def resolved_data_root(self) -> Path:
        """Local tick/cache directory -- NEVER OneDrive, NEVER %LOCALAPPDATA%."""
        if self.data_root.strip():
            return Path(self.data_root).expanduser().resolve()
        return Path.home() / "quant_data" / "ibkr_ticks"
