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

Numeric fields are bounded (``max_tokens`` > 0, both timeouts > 0 and finite,
``temperature`` >= 0 and finite) so a typo fails at construction, not as a server
400 or a hang mid-run; an infinite timeout would end in an OverflowError at the
socket layer. ``api_key`` is a ``SecretStr`` and validation errors hide their input,
so a configured key never reaches a log through ``repr(settings)`` or a
``ValidationError``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LocalLLMSettings(BaseSettings):
    # env_ignore_empty: an empty "LLM_TEMPERATURE=" line means "unset", as
    # .env.example documents, instead of a ValidationError on a float field.
    # hide_input_in_errors: a ValidationError (a missing LLM_MODEL, say) would
    # otherwise echo the whole input dict, api_key included.
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
    # vLLM ignores it; sent as a bearer token for API parity. A SecretStr, so a real
    # key never shows in repr(settings); the client reads it via get_secret_value().
    api_key: SecretStr = SecretStr("EMPTY")
    max_tokens: int = Field(default=8192, gt=0)
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    # None -> omitted from the request (server default); 0.0 is valid (greedy decoding).
    temperature: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    connect_timeout_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    read_timeout_s: float = Field(default=900.0, gt=0, allow_inf_nan=False)
