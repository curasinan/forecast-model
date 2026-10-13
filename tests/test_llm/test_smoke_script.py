"""scripts/llm_smoke.py main(): exit codes and printed lines, through the real client."""

from __future__ import annotations

import json

import httpx
import pytest

from scripts.llm_smoke import SCHEMA, main
from tests.test_llm.test_client import _completion

MODEL = "qwen-test"
COMPLETE = {"city": "Izmir", "year": 2024, "passed": True}
SECRET = "sekrit-value-123"


@pytest.fixture(autouse=True)
def _smoke_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here, whatever the checkout has
    monkeypatch.setenv("LLM_MODEL", MODEL)
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")


def _server(served: list[str], reply: object) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A healthy stand-in: lists ``served`` under /models, answers any chat with ``reply``."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": name} for name in served]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json=_completion(json.dumps(reply)))
        return httpx.Response(404, text=f"{request.method} {request.url.path}")

    return httpx.MockTransport(handler), seen


def test_smoke_exits_0_and_prints_ok_when_the_model_is_served_and_the_reply_is_complete(capsys):
    """Kills: a smoke that cannot pass, a dropped schema constraint, token counts not reported."""
    transport, seen = _server([MODEL, "other-model"], COMPLETE)

    rc = main(transport=transport)
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert "served: ['qwen-test', 'other-model']" in lines
    assert "parsed: {'city': 'Izmir', 'year': 2024, 'passed': True}" in lines
    assert lines[-1] == "OK"
    stats = next((line for line in lines if line.startswith("latency ")), "")
    assert "completion tokens 5," in stats  # usage.completion_tokens of _completion
    assert stats.endswith("reasoning tokens 3")
    (chat,) = [request for request in seen if request.method == "POST"]
    sent = json.loads(chat.content)["response_format"]["json_schema"]
    assert sent == {"name": "extraction", "schema": SCHEMA}


def test_smoke_exits_1_naming_the_missing_key_when_the_reply_lacks_a_required_one(capsys):
    """Kills: a reply that parses but is incomplete passing as OK, or failing without a reason."""
    transport, _ = _server([MODEL], {"city": "Izmir", "year": 2024})

    rc = main(transport=transport)
    captured = capsys.readouterr()

    assert rc == 1
    assert "FAIL: missing keys ['passed']" in captured.err
    lines = captured.out.splitlines()
    assert "parsed: {'city': 'Izmir', 'year': 2024}" in lines  # the operator sees what came back
    assert "OK" not in lines


@pytest.mark.parametrize(
    ("reply", "type_name"),
    [(["city", "year", "passed"], "list"), (5, "int")],
    ids=["list-of-the-key-names", "number"],
)
def test_smoke_exits_1_naming_the_type_when_the_reply_is_not_a_json_object(
    reply, type_name, capsys
):
    transport, _ = _server([MODEL], reply)

    rc = main(transport=transport)
    captured = capsys.readouterr()

    assert rc == 1
    assert f"FAIL: parsed reply is not a JSON object ({type_name})" in captured.err
    lines = captured.out.splitlines()
    assert f"parsed: {reply}" in lines  # the operator sees what came back
    assert "OK" not in lines


def test_smoke_exits_1_naming_the_served_ids_when_the_model_is_not_served(capsys):
    """Kills: a prompt sent to a server that does not serve LLM_MODEL, a FAIL that hides the ids."""
    served = ["alpha-model", "beta-model"]
    transport, seen = _server(served, COMPLETE)  # it could answer a chat, and must not be asked

    rc = main(transport=transport)
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.err.startswith("FAIL:")
    for name in served:
        assert name in captured.err
    assert "OK" not in captured.out.splitlines()
    assert not [request for request in seen if request.method == "POST"]  # no prompt was sent


def test_smoke_exits_1_with_a_settings_line_when_llm_model_is_unset(monkeypatch, capsys):
    monkeypatch.delenv("LLM_MODEL")  # the one required field; the cwd holds no .env
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    transport, seen = _server([MODEL], COMPLETE)

    rc = main(transport=transport)
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.err.startswith("FAIL: settings:")
    assert "model" in captured.err  # the missing field is named
    assert SECRET not in captured.err + captured.out
    assert captured.out == ""  # no banner: nothing was configured
    assert seen == []  # and no request left the process
