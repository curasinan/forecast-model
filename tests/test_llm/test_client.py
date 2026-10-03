"""LocalLLMClient against an httpx.MockTransport: request shape, parsing, errors.

No network. The handler records the request the client sent and answers
with a hand-drawn OpenAI-style chat-completions shape (content +
reasoning_content). The client reads reasoning_content and falls back to
reasoning, the key the Spark's vLLM actually sends; test_real_shapes.py covers
that against replies captured from it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.llm.client import (
    LLMConfigError,
    LLMHTTPError,
    LLMOutputError,
    LocalLLMClient,
)
from src.llm.settings import LocalLLMSettings


def _settings(**overrides) -> LocalLLMSettings:
    base = dict(model="qwen-test", base_url="http://llm.test/v1")
    base.update(overrides)
    return LocalLLMSettings(_env_file=None, **base)


def _completion(
    content: str | None, reasoning: str | None = "thinking", finish_reason: str | None = "stop"
) -> dict:
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "model": "qwen-test",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


def _client(handler, **overrides) -> LocalLLMClient:
    return LocalLLMClient(_settings(**overrides), transport=httpx.MockTransport(handler))


def test_chat_sends_configured_fields_and_parses_a_schema_reply():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion('{"answer": 42}'))

    with _client(handler) as client:
        reply = client.chat([{"role": "user", "content": "hi"}], json_schema={"type": "object"})

    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer EMPTY"
    body = seen["body"]
    assert body["model"] == "qwen-test"
    assert body["max_tokens"] == 8192
    assert body["reasoning_effort"] == "low"
    assert "temperature" not in body
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "reply", "schema": {"type": "object"}},
    }
    assert reply.content == '{"answer": 42}'
    assert reply.parsed == {"answer": 42}
    assert reply.reasoning == "thinking"
    assert reply.usage["completion_tokens"] == 5
    assert reply.usage["reasoning_tokens"] == 3
    assert reply.model == "qwen-test"
    assert reply.finish_reason == "stop"
    assert reply.latency_s >= 0.0


def test_chat_without_schema_sends_no_response_format_and_leaves_parsed_none():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("plain text", reasoning=None))

    reply = _client(handler, temperature=0.2, max_tokens=64).chat(
        [{"role": "user", "content": "hi"}], max_tokens=32
    )
    assert "response_format" not in seen["body"]
    assert seen["body"]["temperature"] == 0.2
    assert seen["body"]["max_tokens"] == 32  # per-call override wins
    assert reply.parsed is None
    assert reply.reasoning is None


def test_chat_with_schema_raises_output_error_carrying_the_raw_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("not json at all"))

    with pytest.raises(LLMOutputError) as info:
        _client(handler).chat([{"role": "user", "content": "hi"}], json_schema={"type": "object"})
    assert info.value.raw == "not json at all"


def test_http_error_is_surfaced_with_status_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(LLMHTTPError) as info:
        _client(handler).chat([{"role": "user", "content": "hi"}])
    assert info.value.status == 500
    assert "boom" in info.value.body


def test_models_lists_served_ids_and_assert_model_served_checks_config():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "other-model"}, {"id": "qwen-test"}]})

    client = _client(handler)
    assert client.models() == ["other-model", "qwen-test"]
    client.assert_model_served()  # configured model is listed: no raise

    with pytest.raises(LLMConfigError) as info:
        _client(handler, model="not-served").assert_model_served()
    assert "other-model" in str(info.value)
