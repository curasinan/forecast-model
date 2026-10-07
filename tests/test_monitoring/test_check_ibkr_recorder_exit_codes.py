"""scripts/check_ibkr_recorder.py run as a program: what a crash exits with.

CPython exits 1 on any uncaught exception, an import failure included, and the health unit
(deploy/spark/systemd/ibkr-health.service) reads exit 1 as "found a problem and alerted": a
probe broken by a deploy would be pinged and recorded healthy. The script turns every
uncaught exception into exit 3, which the unit does not accept, so the probe's own failure
leaves no ping and the external check goes late. The other tests of the probe call main()
and never see the exit code of a crash; these run the real file in a subprocess.

Every run is harmless even if the break under test did not take: scratch paths for every
file the probe reads or writes, ALERT_METHOD=log_only, no Telegram or heartbeat variable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "scripts" / "check_ibkr_recorder.py"

# Runs the probe as __main__ (so its hook installs) after breaking the clock main() reads.
CRASH_IN_MAIN = """
import datetime, runpy, sys


class Clock(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        raise RuntimeError("the clock was broken by the test")


datetime.datetime = Clock
script = sys.argv[1]
sys.argv = [script, *sys.argv[2:]]
runpy.run_path(script, run_name="__main__")
"""


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """The test's environment minus anything that could reach a phone, a ping or a data root."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("HEARTBEAT_URL", "TELEGRAM_", "IBKR_", "ALERT_"))}
    env.update({"ALERT_METHOD": "log_only", "IBKR_DATA_ROOT": str(tmp_path / "data_root")})
    env.update(extra)
    return env


def _scratch(tmp_path: Path) -> list[str]:
    """Every path the probe reads or writes, under tmp_path (none of them exists yet)."""
    return ["--state-path", str(tmp_path / "state" / "recorder_state.json"),
            "--events-path", str(tmp_path / "events.jsonl"),
            "--sync-runs-path", str(tmp_path / "paper" / "ibkr_sync_runs.jsonl"),
            "--snapshot-path", str(tmp_path / "universe.json"),
            "--health-log-path", str(tmp_path / "health.log"),
            "--alert-state-path", str(tmp_path / "alert_state.json")]


def _run(tmp_path: Path, command: list[str], **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=120, cwd=tmp_path,
                          env=_env(tmp_path, **extra_env), check=False)


def test_an_import_failure_exits_3_and_leaves_a_traceback(tmp_path):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    # monitoring.heartbeat imports requests at the top, and PYTHONPATH comes before site-packages.
    (shadow / "requests.py").write_text('raise ImportError("requests broken by the test")\n',
                                        encoding="utf-8")
    done = _run(tmp_path, [sys.executable, "-u", str(PROBE), *_scratch(tmp_path)],
                PYTHONPATH=str(shadow))
    assert done.returncode == 3, (done.stdout, done.stderr)
    assert "Traceback (most recent call last)" in done.stderr
    assert "ImportError: requests broken by the test" in done.stderr
    assert done.stdout == ""  # the failure was in the imports: nothing of the probe ran


def test_an_uncaught_exception_in_main_exits_3_and_leaves_a_traceback(tmp_path):
    done = _run(tmp_path, [sys.executable, "-u", "-c", CRASH_IN_MAIN, str(PROBE),
                           *_scratch(tmp_path)])
    assert done.returncode == 3, (done.stdout, done.stderr)
    assert "Traceback (most recent call last)" in done.stderr
    assert "RuntimeError: the clock was broken by the test" in done.stderr
    assert "in main" in done.stderr  # raised from main(), not from the imports
    assert done.stdout == ""


@pytest.mark.parametrize(("extra", "code"), [
    (["--test-alert"], 0),           # delivered: ALERT_METHOD=log_only is the configured path
    ([], 1),                         # no heartbeat file: a problem found, and alerted
    (["--no-such-option"], 2),       # argparse's own exit, a SystemExit and not a crash
])
def test_the_probes_own_exit_codes_are_not_changed(tmp_path, extra, code):
    done = _run(tmp_path, [sys.executable, "-u", str(PROBE), *_scratch(tmp_path), *extra])
    assert done.returncode == code, (done.stdout, done.stderr)
    assert "Traceback" not in done.stderr, done.stderr


def test_importing_the_module_installs_no_hook():
    """The hook is for the program only: the in-process tests import the module."""
    code = ("import sys; before = sys.excepthook; import scripts.check_ibkr_recorder; "
            "print(sys.excepthook is before)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=120, cwd=REPO_ROOT, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True"
