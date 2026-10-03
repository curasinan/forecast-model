"""LocalLLMClient request side: the exact wire format, the bearer key, the production wiring.

test_client.py checks the happy path; these tests pin what it leaves open, each one
against the single regression it exists to catch (named in its docstring). MockTransport
only: the proxy-environment test builds a real client with no transport but never sends
a request.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.llm.client import LocalLLMClient
from tests.test_llm.test_client import _client, _completion, _settings

MESSAGES = [
    {"role": "system", "content": "Extract the fields."},
    {"role": "user", "content": "hi", "name": "operator"},
]
SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}}


def _recording_client(seen: list[httpx.Request], **overrides) -> LocalLLMClient:
    """A client whose server records every request and answers like a healthy one."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen-test"}]})
        return httpx.Response(200, json=_completion('{"answer": 1}'))

    return _client(handler, **overrides)


def test_chat_request_is_exactly_what_the_server_should_see():
    """Kills: messages dropped from the body, stray body keys, wrong verb or content type."""
    seen: list[httpx.Request] = []
    _recording_client(seen).chat(MESSAGES)

    (request,) = seen
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/json"
    # Whole-dict equality: also proves no temperature and no response_format when not given.
    assert json.loads(request.content) == {
        "model": "qwen-test",
        "messages": MESSAGES,
        "max_tokens": 8192,
        "reasoning_effort": "low",
    }


@pytest.mark.parametrize("temperature", [0.0, 0.7])
def test_chat_request_carries_temperature_effort_and_schema_when_given(temperature):
    """Kills: schema_name ignored, configured effort hardcoded, temperature 0.0 dropped as falsy."""
    seen: list[httpx.Request] = []
    client = _recording_client(
        seen, temperature=temperature, max_tokens=64, reasoning_effort="high"
    )
    client.chat(MESSAGES, json_schema=SCHEMA, schema_name="extraction", max_tokens=32)

    (request,) = seen
    assert json.loads(request.content) == {
        "model": "qwen-test",
        "messages": MESSAGES,
        "max_tokens": 32,
        "reasoning_effort": "high",
        "temperature": temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": SCHEMA},
        },
    }


def test_explicit_zero_max_tokens_is_sent_not_replaced_by_the_default():
    """Kills: ``max_tokens or default`` swallowing an explicit 0 (the server should reject it)."""
    seen: list[httpx.Request] = []
    _recording_client(seen).chat(MESSAGES, max_tokens=0)

    assert json.loads(seen[0].content)["max_tokens"] == 0


def test_api_key_travels_only_as_the_bearer_header():
    """Kills: a constant bearer instead of the configured key, or the key leaking elsewhere."""
    key = "k-123"
    seen: list[httpx.Request] = []
    client = _recording_client(seen, api_key=key)
    client.models()
    client.chat(MESSAGES)

    assert len(seen) == 2
    for request in seen:
        assert request.headers["authorization"] == f"Bearer {key}"
        assert key not in str(request.url)
        assert key not in request.content.decode()
        other_headers = [v for name, v in request.headers.items() if name != "authorization"]
        assert not any(key in value for value in other_headers)


def test_configured_timeouts_reach_the_request():
    """Kills: timeouts not applied, or taken from the wrong settings fields."""
    seen: list[httpx.Request] = []
    _recording_client(seen, connect_timeout_s=3.0, read_timeout_s=7.0).models()

    timeout = seen[0].extensions["timeout"]
    assert timeout["connect"] == 3.0
    assert timeout["read"] == 7.0


def test_exit_closes_the_http_client():
    """Kills: __exit__ no longer closing the connection pool."""
    with _client(lambda request: httpx.Response(200, json={"data": []})) as client:
        assert not client._http.is_closed
    assert client._http.is_closed


def test_exit_closes_the_http_client_and_lets_the_exception_through():
    """Cleanup must also run when the body raises, and must not swallow the exception."""
    client = _client(lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(RuntimeError, match="boom"), client:
        raise RuntimeError("boom")
    assert client._http.is_closed


def test_close_closes_the_http_client():
    """Kills: close() turning into a no-op."""
    client = _client(lambda request: httpx.Response(200, json={"data": []}))
    client.close()
    assert client._http.is_closed


def test_client_without_a_transport_ignores_the_proxy_environment(monkeypatch):
    """Kills: dropping trust_env=False.

    Every other test passes a MockTransport, and httpx switches env proxies off by itself
    then (``allow_env_proxies = trust_env and transport is None``), so only this
    construction path, the one production and the smoke script use, can see the bug: with a
    proxy variable set, the prompts and the bearer for the loopback tunnel would go to the
    proxy. Builds the client and reads httpx's routing table; no request is sent. The
    underscore attributes are httpx internals, used because it has no public routing API.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    client = LocalLLMClient(_settings(base_url="http://127.0.0.1:8000/v1"))
    try:
        assert client._http.trust_env is False
        assert client._http._mounts == {}  # httpx adds one mount per proxy variable otherwise
        loopback = httpx.URL("http://127.0.0.1:8000/v1/models")
        assert client._http._transport_for_url(loopback) is client._http._transport
        assert client._http.follow_redirects is False  # a redirect must never carry the bearer
    finally:
        client.close()
