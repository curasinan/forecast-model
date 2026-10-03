"""Safety net for the monitoring tests: no test here may ever send a real alert.

On the operator's machine ALERT_METHOD=telegram plus a real bot token and chat
id are set in the environment, and ``monitoring.heartbeat.send_alert`` reads
them straight from ``os.environ``. A test that reaches ``send_alert`` without
pinning those variables and without patching ``requests.post`` would message
the operator's phone. The fixture below makes that impossible for every test in
this package, whatever the shell looks like.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import requests

_ALERT_ENV_VARS = ("ALERT_METHOD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


@pytest.fixture(autouse=True)
def _no_real_alerts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the alert environment and trip on any unpatched network call.

    * The three variables are removed, so a test that forgot to pin them runs
      the ``log_only`` path. A test that wants the Telegram path sets them
      itself, with fake values.
    * ``requests.post`` -- the call send_alert makes -- is replaced by a
      tripwire, and so is the adapter underneath every requests call
      (``HTTPAdapter.send``), in case send_alert ever reaches the network by
      another route. ``send_alert`` contains every exception, so raising inside
      a tripwire alone would be swallowed and the test would stay green: the
      call is recorded and the test fails at teardown instead. The URL is
      deliberately not recorded (it carries the bot token). A test that patches
      ``requests.post`` itself simply replaces the first tripwire.
    """
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
