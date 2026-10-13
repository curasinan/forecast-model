"""Live round-trip against a reachable vLLM server. Skipped unless LLM_LIVE=1."""

from __future__ import annotations

import os

import pytest

from src.llm.client import LocalLLMClient
from src.llm.settings import LocalLLMSettings

pytestmark = pytest.mark.skipif(
    os.environ.get("LLM_LIVE") != "1", reason="set LLM_LIVE=1 with a reachable vLLM server"
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_live_schema_round_trip():
    settings = LocalLLMSettings()
    with LocalLLMClient(settings) as client:
        client.assert_model_served()
        reply = client.chat(
            [{"role": "user", "content": "Is water wet? Answer as JSON with key answer: yes or no."}],
            json_schema=SCHEMA,
            max_tokens=2048,
        )
    assert reply.parsed["answer"] in ("yes", "no")
    assert reply.usage.get("completion_tokens", 0) > 0
