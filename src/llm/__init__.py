"""Local LLM access (vLLM on the DGX Spark, OpenAI-compatible HTTP).

Role decision 2026-09-29 (docs/plans/2026-09-30-spark-research-split-design.md,
section 6): the LLM is a data processor -- text in, structured data out --
never the predictor. The numeric model prices;
tests/test_architecture/test_llm_boundary.py keeps this package out of
climate/, src/trading/, src/venues/, src/edge/, src/domain/, src/math/,
src/models/, monitoring/ and the ordering scripts.
"""

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
