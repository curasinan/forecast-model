"""Settings for the local (vLLM, OpenAI-compatible) LLM."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LocalLLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    # REQUIRED, no default: the id the server lists under /v1/models.
    model: str
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: SecretStr = SecretStr("EMPTY")
    max_tokens: int = Field(default=8192, gt=0)
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    temperature: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    connect_timeout_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    read_timeout_s: float = Field(default=900.0, gt=0, allow_inf_nan=False)
