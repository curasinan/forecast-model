"""LocalLLMClient against replies captured from the real server, not drawn by hand."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from tests.test_llm.test_client import _client

FIXTURES = Path(__file__).parent / "fixtures"
REAL_MODEL = "Qwen3.8-Flash-Next-NVFP4"
REAL_REASONING_START = "The user wants me to extract"
MESSAGES = [
    {"role": "user", "content": "In 2024 the city council of Izmir voted on the tram line."}
]
SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "year": {"type": "integer"}},
    "required": ["city", "year"],
    "additionalProperties": False,
}


def _serving(body: bytes):
    """A client whose server answers every request with ``body``, exactly as captured."""
    headers = {"content-type": "application/json"}
    return _client(
        lambda request: httpx.Response(200, content=body, headers=headers), model=REAL_MODEL
    )


def _real_chat_body() -> bytes:
    return (FIXTURES / "vllm_chat_reply_schema.json").read_bytes()


def _real_reply():
    return _serving(_real_chat_body()).chat(MESSAGES, json_schema=SCHEMA)


def _reply_with_edited_message(edit):
    """The captured reply with its ``message`` object edited in memory: derived, not hand-drawn."""
    payload = json.loads(_real_chat_body())
    edit(payload["choices"][0]["message"])
    return _serving(json.dumps(payload).encode()).chat(MESSAGES, json_schema=SCHEMA)


def test_real_reply_reasoning_comes_from_message_reasoning():
    """Kills: reading only ``reasoning_content``."""
    reply = _real_reply()
    assert reply.reasoning is not None  # None here is the silent loss
    assert reply.reasoning.startswith(REAL_REASONING_START)


def test_real_reply_content_is_parsed_through_the_schema_path():
    reply = _real_reply()
    assert reply.content == '{"city":"Izmir","year":2024}'
    assert reply.parsed == {"city": "Izmir", "year": 2024}


def test_real_reply_usage_counts_and_reasoning_tokens():
    reply = _real_reply()
    assert reply.usage["prompt_tokens"] == 81
    assert reply.usage["completion_tokens"] == 78
    assert reply.usage["reasoning_tokens"] == 62
    assert set(reply.usage) == {
        "prompt_tokens",
        "total_tokens",
        "completion_tokens",
        "reasoning_tokens",
    }


def test_real_reply_names_the_served_model():
    assert _real_reply().model == "Qwen3.8-Flash-Next-NVFP4"


def test_real_reply_finish_reason_is_stop():
    """The server says why generation ended; a complete schema reply says ``stop``."""
    assert _real_reply().finish_reason == "stop"


def test_real_models_listing_lists_the_served_model():
    body = (FIXTURES / "vllm_models_reply.json").read_bytes()
    client = _serving(body)
    assert client.models() == ["Qwen3.8-Flash-Next-NVFP4"]
    client.assert_model_served()  # the settings name that id: no raise


def test_openai_style_reasoning_content_key_is_still_read():
    """Kills: dropping ``reasoning_content``, which OpenAI-style servers and older vLLM use."""

    def rename(message):
        message["reasoning_content"] = message.pop("reasoning")

    reply = _reply_with_edited_message(rename)
    assert reply.reasoning is not None
    assert reply.reasoning.startswith(REAL_REASONING_START)


def test_reasoning_content_wins_when_both_keys_are_present():
    def add_reasoning_content(message):
        message["reasoning_content"] = "from-reasoning-content"

    assert _reply_with_edited_message(add_reasoning_content).reasoning == "from-reasoning-content"


def test_null_reasoning_content_falls_back_to_reasoning():
    """Kills: a null ``reasoning_content`` shadowing a real ``reasoning``."""

    def null_reasoning_content(message):
        message["reasoning_content"] = None

    reply = _reply_with_edited_message(null_reasoning_content)
    assert reply.reasoning is not None
    assert reply.reasoning.startswith(REAL_REASONING_START)


def test_reply_without_a_reasoning_key_has_no_reasoning():
    def drop_reasoning(message):
        del message["reasoning"]

    assert _reply_with_edited_message(drop_reasoning).reasoning is None
