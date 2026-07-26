"""Previous Runs client request shape + retry path. Injected fake transport."""

from __future__ import annotations

import json

import httpx
import pytest

from weather.open_meteo import (
    CUSTOMER_PREVIOUS_RUNS_BASE,
    PREVIOUS_RUNS_BASE,
    OpenMeteoClient,
    resolve_open_meteo_previous_runs_base_url,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.responses.pop(0)


def _client(session: _FakeSession) -> OpenMeteoClient:
    return OpenMeteoClient(for_trading=False, session=session)


def test_request_shape():
    session = _FakeSession([_FakeResponse(200, {"hourly": {}})])
    _client(session).fetch_previous_runs(
        40.78, -73.87,
        start_date="2024-06-01",
        end_date="2024-06-30",
        model="gfs_seamless",
        retries=0,
    )
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"].startswith(PREVIOUS_RUNS_BASE)
    params = call["params"]
    hourly = params["hourly"].split(",")
    assert hourly == [f"temperature_2m_previous_day{n}" for n in range(1, 8)]
    assert params["timezone"] == "UTC"
    assert params["models"] == "gfs_seamless"
    assert params["start_date"] == "2024-06-01"
    assert params["end_date"] == "2024-06-30"
    assert "daily" not in params
    assert call["headers"]["User-Agent"].startswith("polymarket-trader/")


def test_retry_on_429_then_success():
    payload = {"hourly": {"time": []}}
    session = _FakeSession([_FakeResponse(429), _FakeResponse(200, payload)])
    out = _client(session).fetch_previous_runs(
        40.78, -73.87,
        start_date="2024-06-01",
        end_date="2024-06-30",
        model="gfs_seamless",
        retries=2,
        retry_delay=0.0,
    )
    assert out == payload
    assert len(session.calls) == 2


def test_no_retry_by_default_raises():
    session = _FakeSession([_FakeResponse(500)])
    with pytest.raises(httpx.HTTPStatusError):
        _client(session).get_json(f"{PREVIOUS_RUNS_BASE}/v1/forecast", {})
    assert len(session.calls) == 1


def test_retries_exhausted_raises():
    session = _FakeSession([_FakeResponse(429), _FakeResponse(429), _FakeResponse(429)])
    with pytest.raises(httpx.HTTPStatusError):
        _client(session).get_json(
            f"{PREVIOUS_RUNS_BASE}/v1/forecast", {}, retries=2, retry_delay=0.0
        )
    assert len(session.calls) == 3


def test_previous_runs_host_resolution(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "open_meteo_api_key", "")
    monkeypatch.setattr(settings, "open_meteo_base_url", "")
    assert resolve_open_meteo_previous_runs_base_url() == PREVIOUS_RUNS_BASE
    monkeypatch.setattr(settings, "open_meteo_api_key", "k")
    assert resolve_open_meteo_previous_runs_base_url() == CUSTOMER_PREVIOUS_RUNS_BASE


def test_self_hosted_base_not_routed_to_public(monkeypatch):
    # A self-hosted base must be used as-is: falling back to the public host
    # would silently bypass the licensing gate.
    from src.config import settings

    monkeypatch.setattr(settings, "open_meteo_api_key", "")
    monkeypatch.setattr(settings, "open_meteo_base_url", "http://my-selfhost:8080/")
    assert resolve_open_meteo_previous_runs_base_url() == "http://my-selfhost:8080"
    monkeypatch.setattr(settings, "open_meteo_base_url", "https://customer-api.open-meteo.com")
    assert resolve_open_meteo_previous_runs_base_url() == CUSTOMER_PREVIOUS_RUNS_BASE
