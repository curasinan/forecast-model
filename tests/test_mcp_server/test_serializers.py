"""JSON-safe serializers for MCP responses."""

from __future__ import annotations

import math

from mcp_server.serializers import clamp_limit, err, json_safe, ok


def test_json_safe_nan_inf():
    assert json_safe(float("nan")) is None
    assert json_safe(float("inf")) is None
    assert json_safe(math.nan) is None
    assert json_safe(1.5) == 1.5


def test_ok_err_shape():
    assert ok({"a": 1}, n=1) == {
        "ok": True,
        "error": None,
        "data": {"a": 1},
        "meta": {"n": 1},
    }
    e = err("x", "msg", data=[1], hint="h")
    assert e["ok"] is False
    assert e["error"] == "x"
    assert e["message"] == "msg"
    assert e["meta"]["hint"] == "h"


def test_clamp_limit():
    assert clamp_limit(None) == 50
    assert clamp_limit(0) == 1
    assert clamp_limit(9999) == 500
