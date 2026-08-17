"""IBKR venue configuration (env-driven, prefix ``IBKR_``).

Loud-failure requirement (migration spec Stage 7): ``port`` and
``trading_mode`` have NO defaults. A process that does not state which
Gateway port and which mode it expects must fail with a pydantic
ValidationError at construction -- it must never silently attach to
whatever session happens to be listening.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class IBKRSettings(BaseSettings):
    """Connection, data, and verified-fact settings for the IBKR adapter.

    ``commission_per_contract`` / ``fees_verified_on`` and ``coupon_apy`` /
    ``coupon_verified_on`` hold runtime-verified venue facts (whatIf probe,
    account statements -- see docs/venue_semantics.md). They stay None until
    verified; the adapter refuses to build fee/carry models from Nones.
    """

    model_config = SettingsConfigDict(env_prefix="IBKR_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    # REQUIRED, no default: Gateway 4001 live / 4002 paper, TWS 7496 / 7497.
    port: int
    # REQUIRED, no default: cross-checked against the account prefix on connect.
    trading_mode: Literal["paper", "live"]
    data_root: str = ""
    commission_per_contract: float | None = None
    fees_verified_on: date | None = None
    coupon_apy: float | None = None
    coupon_verified_on: date | None = None
    max_data_lines: int = 100
    record_interval_minutes: int = 10
    market_data_type: int = 4  # 4 = delayed-frozen; works without paid subscriptions

    @property
    def resolved_data_root(self) -> Path:
        """Local tick/cache directory -- NEVER inside the OneDrive-synced repo.

        OneDrive sync corrupts high-churn files (SQLite journals, append-only
        tick logs) mid-write; same convention as weather/paths.py. Default is
        %LOCALAPPDATA%/ibkr_ticks, with a ~/.local/share fallback off Windows.
        """
        if self.data_root.strip():
            return Path(self.data_root).expanduser().resolve()
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "ibkr_ticks"
        return Path.home() / ".local" / "share" / "ibkr_ticks"
