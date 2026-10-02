"""Settings for the local (vLLM, OpenAI-compatible) LLM.

``model`` is REQUIRED with no default: the served model name comes from
configuration, never from code (Spark handover decision), and
``LocalLLMClient.assert_model_served`` checks it against what the server
actually lists. Defaults mirror the measured request rules from the Spark
handover: ``max_tokens`` >= 8192 (the reasoning model needs room),
``reasoning_effort`` low (the default effort burns ~1.6k reasoning tokens
per call), a long read timeout (~15 tok/s single-stream, plus queueing).

Same pattern as ``src/venues/ibkr/settings.py``: pydantic-settings with an
env prefix, ``.env`` as the file source, unknown keys ignored.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class LocalLLMSettings(BaseSettings):
    # env_ignore_empty: an empty "LLM_TEMPERATURE=" line means "unset", as
    # .env.example documents, instead of a ValidationError on a float field.
    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # REQUIRED, no default: the id the server lists under /v1/models.
    model: str
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = "EMPTY"  # vLLM ignores it; sent as a bearer token for API parity
    max_tokens: int = 8192
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    temperature: float | None = None  # None -> omitted from the request (server default)
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 900.0
