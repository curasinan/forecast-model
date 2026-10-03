"""LocalLLMClient failure side: every way a server can go wrong must surface as an LLMError.

Transport errors, HTTP statuses, bodies that are not JSON objects and, the part the brief's
tests never touched, HTTP-200 payloads of the wrong shape. Each test names the regression
it catches. MockTransport only.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.llm.client import LLMError, LLMHTTPError, LLMOutputError
from tests.test_llm.test_client import _client, _completion

KEY = "sk-live-9f3a"  # distinctive on purpose: no error message may ever contain it
MESSAGES = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object"}


def _raw_completion(message, **extra) -> dict:
    """A completion whose ``message`` (and top-level extras) are taken exactly as given."""
    return {"model": "qwen-test", "choices": [{"index": 0, "message": message}], **extra}


def _assert_names_the_request_only(error: LLMError, request: str, fragment: str) -> None:
    """The message names the request and the offending field, never the base URL or the key."""
    text = str(error)
    assert request in text
    assert fragment in text
    assert "llm.test" not in text
    assert KEY not in text


# ---- transport, status and body-level failures ---------------------------------------------


@pytest.mark.parametrize(
    "call",
    [lambda client: client.models(), lambda client: client.chat(MESSAGES)],
    ids=["models", "chat"],
)
@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("closed")],
    ids=lambda error: type(error).__name__,
)
def test_transport_failures_become_llm_error_naming_the_exception(call, error):
    """Kills: raw httpx exceptions escaping (tunnel down, read timeout, dropped connection)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(LLMError) as info:
        call(_client(handler, api_key=KEY))
    assert not isinstance(info.value, LLMHTTPError)  # no status: nothing came back
    assert type(error).__name__ in str(info.value)


@pytest.mark.parametrize("status", [400, 401, 404, 422, 500, 503])
def test_http_errors_carry_their_status_and_body(status):
    """Kills: the error threshold moving up (a 404 from a wrong base_url must not pass)."""
    client = _client(lambda request: httpx.Response(status, json={"error": "nope"}), api_key=KEY)
    with pytest.raises(LLMHTTPError) as info:
        client.models()
    assert info.value.status == status
    assert "nope" in info.value.body
    assert str(status) in str(info.value)


@pytest.mark.parametrize(
    "response, fragment",
    [
        (httpx.Response(200, text="<html>nope</html>"), "non-JSON body"),
        (httpx.Response(200, content=b""), "non-JSON body"),
        (httpx.Response(200, json=[1, 2]), "expected a JSON object, got list"),
        (httpx.Response(200, json="text"), "expected a JSON object, got str"),
        (httpx.Response(200, content=b"null"), "expected a JSON object, got NoneType"),
    ],
    ids=["html", "empty", "array", "string", "null"],
)
def test_non_json_and_non_object_200_bodies_are_llm_errors(response, fragment):
    """Kills: a body that is not a JSON object reaching the parsing code (AttributeError)."""
    with pytest.raises(LLMError) as info:
        _client(lambda request: response, api_key=KEY).models()
    _assert_names_the_request_only(info.value, "GET /models", fragment)


def test_null_content_without_a_schema_is_empty_text():
    """Kills: the content=null tolerance going away (None leaking into LLMReply.content)."""
    client = _client(lambda request: httpx.Response(200, json=_completion(None)), api_key=KEY)
    reply = client.chat(MESSAGES)
    assert reply.content == ""
    assert reply.parsed is None


def test_null_content_with_a_schema_is_an_output_error():
    """With a schema, empty content fails closed as LLMOutputError, never as a TypeError."""
    client = _client(lambda request: httpx.Response(200, json=_completion(None)), api_key=KEY)
    with pytest.raises(LLMOutputError) as info:
        client.chat(MESSAGES, json_schema=SCHEMA)
    assert info.value.raw == ""


# ---- HTTP-200 payloads of the wrong shape ----------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        None,
        "abc",
        5,
        {"id": "x"},
        ["str"],
        [1],
        [None],
        [{}],
        [{"id": None}],
        [{"id": 5}],
        [{"id": "ok"}, "the-second-entry-is-not-an-object"],
    ],
    ids=[
        "data-null",
        "data-str",
        "data-int",
        "data-object",
        "entry-str",
        "entry-int",
        "entry-null",
        "entry-without-id",
        "id-null",
        "id-int",
        "second-entry-str",
    ],
)
def test_malformed_models_listing_is_an_llm_error(data):
    """Kills: a wrong-shaped listing escaping as TypeError/AttributeError, or an entry
    without a string id being skipped or stringified instead of reported."""
    client = _client(lambda request: httpx.Response(200, json={"data": data}), api_key=KEY)
    with pytest.raises(LLMError) as info:
        client.models()
    _assert_names_the_request_only(info.value, "GET /models", "'data'")


def _case(label: str, payload: dict, schema, fragment: str):
    return pytest.param(payload, schema, fragment, id=label)


MALFORMED_COMPLETIONS = [
    _case("message-null", _raw_completion(None), None, "'message'"),
    _case("message-str", _raw_completion("text"), None, "'message'"),
    _case("message-int", _raw_completion(5), None, "'message'"),
    _case("message-list", _raw_completion(["x"]), None, "'message'"),
    *(
        _case(
            f"content-{json.dumps(content)}-{kind}",
            _raw_completion({"content": content}),
            schema,
            "'content'",
        )
        for content in (["part"], 5, 0, [], {"text": "x"}, True)
        for kind, schema in (("plain", None), ("schema", SCHEMA))
    ),
    *(
        _case(
            f"usage-{json.dumps(usage)}",
            _raw_completion({"content": "x"}, usage=usage),
            None,
            "'usage'",
        )
        for usage in ([1, 2], [], "abc", 5, 0)
    ),
]


@pytest.mark.parametrize("payload, schema, fragment", MALFORMED_COMPLETIONS)
def test_malformed_completion_is_an_llm_error(payload, schema, fragment):
    """Kills: a wrong-shaped completion escaping as TypeError/AttributeError, a list or int
    ``content`` slipping into LLMReply.content (with or without a schema), a non-object usage."""
    client = _client(lambda request: httpx.Response(200, json=payload), api_key=KEY)
    with pytest.raises(LLMError) as info:
        client.chat(MESSAGES, json_schema=schema)
    _assert_names_the_request_only(info.value, "POST /chat/completions", fragment)


@pytest.mark.parametrize(
    "extra", [{}, {"usage": None}, {"usage": {}}], ids=["absent", "null", "empty"]
)
def test_absent_or_empty_usage_is_an_empty_dict(extra):
    """The usage check must not reject what servers legitimately omit."""
    payload = _raw_completion({"content": "x"}, **extra)
    client = _client(lambda request: httpx.Response(200, json=payload), api_key=KEY)
    assert client.chat(MESSAGES).usage == {}
