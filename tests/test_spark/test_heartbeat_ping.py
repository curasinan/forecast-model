"""scripts/spark/heartbeat_ping.py: run a command, signal that it ran, never print the URL."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import heartbeat_ping  # noqa: E402

URL = "https://hc.example.invalid/ping/11111111-2222-3333-4444-555555555555"


class Opener:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.urls: list[str] = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if len(self.urls) <= self.fail_times:
            raise OSError(f"cannot reach {url}")  # the URL is in the error text on purpose
        return None


def _exit_with(code: int) -> list[str]:
    return [sys.executable, "-c", f"raise SystemExit({code})"]


@pytest.fixture(autouse=True)
def url_in_env(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL_TEST", URL)


def test_a_command_that_ran_is_signalled_and_its_exit_status_is_kept(capsys):
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener)
    assert rc == 0
    assert opener.urls == [URL]
    assert "heartbeat: sent" in capsys.readouterr().out


def test_an_exit_status_outside_the_ok_list_sends_nothing(capsys):
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(1)],
                             opener=opener)
    assert rc == 1
    assert opener.urls == []
    assert "not sent (command exit 1)" in capsys.readouterr().out


def test_the_ok_list_can_name_more_than_one_status():
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--ok-exit", "0", "1", "--",
                              *_exit_with(1)], opener=opener)
    assert rc == 1 and opener.urls == [URL]


def test_a_failed_ping_is_retried_and_never_changes_the_exit_status(capsys):
    opener = Opener(fail_times=2)
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener, pause=lambda seconds: None)
    assert rc == 0 and len(opener.urls) == 3
    assert "heartbeat: sent" in capsys.readouterr().out


def test_the_url_never_reaches_the_output_even_when_the_error_text_holds_it(capsys):
    opener = Opener(fail_times=99)
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener, pause=lambda seconds: None)
    captured = capsys.readouterr()
    assert rc == 0
    assert "heartbeat: failed (OSError)" in captured.out
    assert URL not in captured.out + captured.err
    assert "11111111" not in captured.out + captured.err


def test_a_missing_variable_is_said_by_name_and_sends_nothing(monkeypatch, capsys):
    monkeypatch.delenv("HEARTBEAT_URL_TEST")
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener)
    assert rc == 0 and opener.urls == []
    assert "HEARTBEAT_URL_TEST is not set" in capsys.readouterr().out


def test_only_an_https_url_is_fetched(monkeypatch, capsys):
    monkeypatch.setenv("HEARTBEAT_URL_TEST", "file:///etc/passwd")
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener)
    assert rc == 0 and opener.urls == []
    assert "not an https URL" in capsys.readouterr().out


def test_no_command_is_a_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST"])
    assert excinfo.value.code == 2


def test_a_command_that_carries_its_own_double_dash_is_run_whole():
    """The Zenbook's pull is `... -- wsl.exe -d Ubuntu -- bash ...`: only the first -- is ours."""
    opener = Opener()
    counts_its_arguments = [sys.executable, "-c", "import sys; raise SystemExit(len(sys.argv))",
                            "--", "x"]
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--ok-exit", "3", "--",
                              *counts_its_arguments], opener=opener)
    assert rc == 3 and opener.urls == [URL]  # the command saw ['-c', '--', 'x']: nothing was eaten
