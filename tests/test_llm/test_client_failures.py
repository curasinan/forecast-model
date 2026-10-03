"""LocalLLMClient failure side: every way a server can go wrong must surface as an LLMError.

Transport errors, HTTP statuses, bodies that are not JSON objects and, the part the brief's
tests never touched, HTTP-200 payloads of the wrong shape. Each test names the regression
it catches. MockTransport only.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.llm.client import LLMConfigError, LLMError, LLMHTTPError, LLMOutputError, LocalLLMClient
from tests.test_llm.test_client import _client, _completion

KEY = "sk-live-9f3a"  # distinctive on purpose: no error message may ever contain it
MESSAGES = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object"}


def _raw_completion(message, **extra) -> dict:
    """A completion whose ``message`` (and top-level extras) are taken exactly as given."""
    return {"model": "qwen-test", "choices": [{"index": 0, "message": message}], **extra}


def _with_finish_reason(value) -> dict:
    """A completion whose ``choices[0].finish_reason`` is taken exactly as given (any JSON)."""
    payload = _raw_completion({"content": "x"})
    payload["choices"][0]["finish_reason"] = value
    return payload


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
    "payload",
    [
        {},
        {"object": "list"},
        {"data": None},
        {"data": "abc"},
        {"data": 5},
        {"data": {"id": "x"}},
        {"data": ["str"]},
        {"data": [1]},
        {"data": [None]},
        {"data": [{}]},
        {"data": [{"id": None}]},
        {"data": [{"id": 5}]},
        {"data": [{"id": "ok"}, "the-second-entry-is-not-an-object"]},
    ],
    ids=[
        "payload-empty",
        "payload-without-data",
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
def test_malformed_models_listing_is_an_llm_error(payload):
    """Kills: a wrong-shaped listing escaping as TypeError/AttributeError, an entry without a
    string id being skipped or stringified, a payload without 'data' reading as an empty list."""
    client = _client(lambda request: httpx.Response(200, json=payload), api_key=KEY)
    with pytest.raises(LLMError) as info:
        client.models()
    _assert_names_the_request_only(info.value, "GET /models", "'data'")


def test_models_payload_without_data_is_not_an_empty_listing():
    """A server answering /models with {} is broken, not serving nothing: assert_model_served
    must not report it as "not served, the server lists []"."""
    client = _client(lambda request: httpx.Response(200, json={}), api_key=KEY)
    with pytest.raises(LLMError) as info:
        client.assert_model_served()
    assert not isinstance(info.value, LLMConfigError)
    assert "not served" not in str(info.value)
    assert "got missing" in str(info.value)  # an absent key is named as such, not as NoneType
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
    # The reasoning text lives under ``reasoning`` (the Spark) or ``reasoning_content``
    # (OpenAI-style servers); whichever is chosen must be a string or null, never dropped
    # silently as None.
    *(
        _case(
            f"{key}-{json.dumps(value)}",
            _raw_completion({"content": "x", key: value}),
            None,
            f"'{key}'",
        )
        for key in ("reasoning", "reasoning_content")
        for value in (5, ["part"], {"text": "x"}, True)
    ),
    _case(
        "null-reasoning-content-then-int-reasoning",
        _raw_completion({"content": "x", "reasoning_content": None, "reasoning": 5}),
        None,
        "'reasoning'",
    ),
    # finish_reason is surfaced on every reply, so it is validated with or without a schema:
    # a string or null only, never dropped silently as None.
    *(
        _case(
            f"finish_reason-{json.dumps(value)}-{kind}",
            _with_finish_reason(value),
            schema,
            "'finish_reason'",
        )
        for value in (5, 1.5, True, ["length"], {"reason": "length"})
        for kind, schema in (("plain", None), ("schema", SCHEMA))
    ),
]


@pytest.mark.parametrize("payload, schema, fragment", MALFORMED_COMPLETIONS)
def test_malformed_completion_is_an_llm_error(payload, schema, fragment):
    """Kills: a wrong-shaped completion escaping as TypeError/AttributeError, a list or int
    ``content`` slipping into LLMReply.content (with or without a schema), a non-object usage,
    a non-string reasoning silently becoming None."""
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


# NaN, Infinity and 1e400 cannot be built with httpx's strict ``json=`` encoder (it refuses
# out-of-range floats), so these usage objects are written as raw JSON text; Python's json
# parses all three. The bad value goes either into a count or into reasoning_tokens.
BAD_USAGE_VALUES = ["NaN", "Infinity", "1e400", '"12"', "true"]
USAGE_PLACES = {
    "count": '{"prompt_tokens": %s}',
    "reasoning": '{"completion_tokens_details": {"reasoning_tokens": %s}}',
}


@pytest.mark.parametrize(
    "literal", BAD_USAGE_VALUES, ids=["nan", "infinity", "1e400", "string", "bool"]
)
@pytest.mark.parametrize("place", sorted(USAGE_PLACES))
def test_usage_values_that_are_not_finite_numbers_are_an_llm_error(place, literal):
    """Kills: int() choking on NaN or Infinity (ValueError/OverflowError escaping chat()), a
    string count skipped silently, a bool counted as 1; for the counts and reasoning_tokens."""
    usage = USAGE_PLACES[place] % literal
    body = ('{"choices": [{"message": {"content": "x"}}], "usage": ' + usage + "}").encode()
    client = _client(lambda request: httpx.Response(200, content=body), api_key=KEY)
    with pytest.raises(LLMError) as info:
        client.chat(MESSAGES)
    _assert_names_the_request_only(info.value, "POST /chat/completions", "finite number")


def test_usage_counts_are_ints_and_detail_blocks_are_skipped():
    """What the finite-number check must keep accepting: int and whole-float counts (always
    returned as int), an int too large for a float (math.isfinite overflows on it, so it
    must only be applied to floats), and null or object-valued detail blocks (the brief's
    own fixture has completion_tokens_details as an object)."""
    usage = {
        "prompt_tokens": 7,
        "completion_tokens": 5.0,
        "total_tokens": 10**400,
        "prompt_tokens_details": None,
        "other_details": {"cached_tokens": 1},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    payload = _raw_completion({"content": "x"}, usage=usage)
    client = _client(lambda request: httpx.Response(200, json=payload), api_key=KEY)
    reply_usage = client.chat(MESSAGES).usage
    assert reply_usage == {
        "prompt_tokens": 7,
        "completion_tokens": 5,
        "total_tokens": 10**400,
        "reasoning_tokens": 2,
    }
    assert {type(count) for count in reply_usage.values()} == {int}  # 5.0 == 5, so check types


# ---- a pathologically nested body or content: RecursionError is not an LLMError by itself -------

# Parses to nothing, but the decoder recurses once per bracket. Far deeper than any platform's
# limit (about 17k levels on Python 3.14.2 / Windows), so the RecursionError branch is reached
# everywhere, not only where the stack happens to be small.
NESTED = "[" * 1_000_000


def test_a_pathologically_nested_200_body_is_an_llm_error():
    """Kills: RecursionError escaping ``response.json()`` (it is not a ValueError), so a hostile
    or broken server could crash the caller instead of failing as an LLMError."""
    client = _client(lambda request: httpx.Response(200, content=NESTED.encode()), api_key=KEY)
    with pytest.raises(LLMError) as info:
        client.models()
    assert not isinstance(info.value, LLMHTTPError)
    _assert_names_the_request_only(info.value, "GET /models", "non-JSON body")


def test_pathologically_nested_schema_content_is_an_output_error_carrying_the_raw_text():
    """Kills: RecursionError escaping ``json.loads`` of the schema content."""
    client = _client(lambda request: httpx.Response(200, json=_completion(NESTED)), api_key=KEY)
    with pytest.raises(LLMOutputError) as info:
        client.chat(MESSAGES, json_schema=SCHEMA)
    assert info.value.raw == NESTED
    assert "not valid JSON" in str(info.value)
    assert len(str(info.value)) < 500  # the 100k brackets are in raw, not in the message


def test_a_malformed_completion_too_deep_to_print_is_still_an_llm_error(monkeypatch):
    """Kills: RecursionError escaping the excerpt of the "malformed completion" message.
    ``json.loads`` accepts nesting that ``json.dumps`` refuses (on Python 3.14.2 / Windows the
    decoder takes about 17k levels and the encoder fewer), so printing a body the decoder took
    can itself raise. The body is built in memory, far deeper than any platform's encoder limit,
    because the depth the decoder takes is platform-bound."""
    deep: list = []
    for _ in range(200_000):
        deep = [deep]
    payload = {"choices": [1], "x": deep}  # choices[0] is not an object: the malformed path
    monkeypatch.setattr(LocalLLMClient, "_request", lambda self, method, path, **kw: payload)
    with pytest.raises(LLMError) as info:
        _client(lambda request: httpx.Response(500)).chat(MESSAGES)
    assert "malformed completion" in str(info.value)


# ---- finish_reason: why generation ended -----------------------------------------------------


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls", "abort"])
def test_the_finish_reason_string_is_surfaced_on_the_reply_without_a_schema(reason):
    """Any string the server sends reaches LLMReply.finish_reason, on the plain path too."""
    client = _client(
        lambda request: httpx.Response(200, json=_completion("text", finish_reason=reason)),
        api_key=KEY,
    )
    assert client.chat(MESSAGES).finish_reason == reason


@pytest.mark.parametrize(
    "payload",
    [_completion("x", finish_reason=None), _raw_completion({"content": "x"})],
    ids=["null", "absent"],
)
def test_a_null_or_absent_finish_reason_is_none(payload):
    """Servers may omit it: that is "unknown", not a malformed reply."""
    client = _client(lambda request: httpx.Response(200, json=payload), api_key=KEY)
    assert client.chat(MESSAGES).finish_reason is None


def test_complete_json_is_parsed_whatever_the_finish_reason_says():
    """finish_reason only explains a parse failure; it never fails a reply that parsed."""
    client = _client(
        lambda request: httpx.Response(
            200, json=_completion('{"answer": 42}', finish_reason="length")
        ),
        api_key=KEY,
    )
    reply = client.chat(MESSAGES, json_schema=SCHEMA)
    assert reply.parsed == {"answer": 42}
    assert reply.finish_reason == "length"


@pytest.mark.parametrize(
    "content",
    ['{"city": "Izmir", "yea', None],
    ids=["truncated-json", "budget-spent-in-reasoning"],
)
def test_unparsable_content_cut_off_at_length_says_so(content):
    """The first thing a caller must tell apart: the reply was cut off (raise max_tokens) or the
    model wrote invalid JSON. Kills: a bare "content is not valid JSON" for both."""
    client = _client(
        lambda request: httpx.Response(200, json=_completion(content, finish_reason="length")),
        api_key=KEY,
    )
    with pytest.raises(LLMOutputError) as info:
        client.chat(MESSAGES, json_schema=SCHEMA)
    assert info.value.raw == (content or "")
    assert "content is not valid JSON" in str(info.value)
    assert "finish_reason=length: the reply was cut off before it finished" in str(info.value)


def test_another_unusual_finish_reason_is_named_without_claiming_a_cut_off():
    """Kills: calling every non-stop ending a truncation."""
    client = _client(
        lambda request: httpx.Response(
            200, json=_completion("I cannot help", finish_reason="content_filter")
        ),
        api_key=KEY,
    )
    with pytest.raises(LLMOutputError) as info:
        client.chat(MESSAGES, json_schema=SCHEMA)
    assert "finish_reason=content_filter" in str(info.value)
    assert "cut off" not in str(info.value)


@pytest.mark.parametrize("reason", ["stop", None], ids=["stop", "unknown"])
def test_invalid_json_after_a_normal_or_unknown_ending_does_not_blame_the_ending(reason):
    """The model finished and still wrote invalid JSON (or the server said nothing): the message
    must not send the caller after the token budget."""
    client = _client(
        lambda request: httpx.Response(200, json=_completion("not json", finish_reason=reason)),
        api_key=KEY,
    )
    with pytest.raises(LLMOutputError) as info:
        client.chat(MESSAGES, json_schema=SCHEMA)
    assert info.value.raw == "not json"
    assert "finish_reason" not in str(info.value)
    assert "cut off" not in str(info.value)
