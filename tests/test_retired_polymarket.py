"""Polymarket runtime is refused unless POLYMARKET_RUNTIME=1."""

from __future__ import annotations

import pytest

from src.retired_polymarket import (
    RETIRED_CODE,
    RETIRED_EXIT,
    polymarket_runtime_allowed,
    refuse_polymarket_runtime,
    retired_mcp_error,
)


def test_refuse_exits_without_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLYMARKET_RUNTIME", raising=False)
    assert polymarket_runtime_allowed() is False
    with pytest.raises(SystemExit) as exc:
        refuse_polymarket_runtime("collectors.collect_markets")
    assert exc.value.code == RETIRED_EXIT


def test_refuse_allows_explicit_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POLYMARKET_RUNTIME", "1")
    refuse_polymarket_runtime("collectors.collect_markets")  # does not raise


def test_mcp_retired_payload():
    out = retired_mcp_error("wx_refresh_gamma")
    assert out["ok"] is False
    assert out["error"] == RETIRED_CODE
    assert "IBKR" in out["message"]


def test_collector_main_is_gated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POLYMARKET_RUNTIME", raising=False)
    from collectors.collect_markets import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == RETIRED_EXIT


def test_mcp_collection_tools_retired():
    from mcp_server.tools import markets, quotes, signals

    gamma = markets.wx_refresh_gamma()
    assert gamma["ok"] is False and gamma["error"] == RETIRED_CODE
    books = quotes.wx_snapshot_books()
    assert books["ok"] is False and books["error"] == RETIRED_CODE
    paper = signals.wx_paper_signals()
    assert paper["ok"] is False and paper["error"] == RETIRED_CODE
    live = quotes.wx_get_quote("m-x", allow_live=True)
    assert live["ok"] is False and live["error"] == RETIRED_CODE
    disc = markets.wx_discover_markets(skip_gamma=False)
    assert disc["ok"] is False and disc["error"] == RETIRED_CODE
