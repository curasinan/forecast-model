"""Local LLM access (vLLM on the DGX Spark, OpenAI-compatible HTTP)."""

from src.llm.client import (
    LLMConfigError,
    LLMError,
    LLMHTTPError,
    LLMOutputError,
    LLMReply,
    LocalLLMClient,
)
from src.llm.settings import LocalLLMSettings

__all__ = [
    "LLMConfigError",
    "LLMError",
    "LLMHTTPError",
    "LLMOutputError",
    "LLMReply",
    "LocalLLMClient",
    "LocalLLMSettings",
]
