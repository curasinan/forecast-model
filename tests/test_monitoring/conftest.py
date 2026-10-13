"""Safety net for the monitoring tests: no test here may ever send a real alert."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import requests

_ALERT_ENV_VARS = ("ALERT_METHOD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


@pytest.fixture(autouse=True)
def _no_real_alerts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the alert environment and trip on any unpatched network call."""
    for name in _ALERT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    attempts: list[str] = []

    def _tripwire(*args: object, **kwargs: object) -> None:
        attempts.append("requests.post")
        raise RuntimeError("tripwire: a monitoring test must patch requests.post")

    def _adapter_tripwire(self: object, *args: object, **kwargs: object) -> None:
        attempts.append("HTTPAdapter.send")
        raise RuntimeError("tripwire: a monitoring test must not touch the network")

    monkeypatch.setattr(requests, "post", _tripwire)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _adapter_tripwire)
    yield
    assert not attempts, f"a monitoring test reached the network unpatched: {attempts}"
