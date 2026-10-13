"""scripts/scheduling: the Zenbook's scheduled pull, the root swap, and the unregister script."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULING = REPO_ROOT / "scripts" / "scheduling"
LAUNCHER = SCHEDULING / "run_spark_pull.ps1"
REGISTER = SCHEDULING / "register_spark_pull_task.ps1"
SWAP = SCHEDULING / "swap_live_root_for_mirror.ps1"
UNREGISTER_IBKR = SCHEDULING / "unregister_ibkr_tasks.ps1"
MARKER = ".mirror-of-spark"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell runs these")


@windows_only
@pytest.mark.parametrize("script", [LAUNCHER, REGISTER, SWAP, UNREGISTER_IBKR],
                         ids=lambda p: p.name)
def test_the_script_parses(script):
    command = ("$tokens = $null; $errors = $null; "
               "[void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{script}', [ref]$tokens, [ref]$errors); $errors.Count")
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=120)
    assert done.stdout.strip() == "0", done.stdout + done.stderr


def test_the_launcher_pulls_through_the_ping_wrapper_and_holds_no_url():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "scripts\\spark\\heartbeat_ping.py --url-env HEARTBEAT_URL_OFFBOX -- wsl.exe" in text
    assert "/scripts/spark/pull_snapshot.sh" in text
    assert "spark_pull_task.log" in text
    assert "exit $LASTEXITCODE" in text
    assert "http://" not in text and "https://" not in text


def test_the_task_runs_at_logon_and_every_six_hours_and_catches_up():
    text = REGISTER.read_text(encoding="utf-8")
    assert '"\\Spark\\"' in text and '"Spark Pull"' in text
    assert "-AtLogOn" in text
    assert "-RepetitionInterval (New-TimeSpan -Hours 6)" in text
    for setting in ("-StartWhenAvailable", "-RunOnlyIfNetworkAvailable", "-AllowStartIfOnBatteries",
                    "-MultipleInstances IgnoreNew", "-ExecutionTimeLimit (New-TimeSpan -Hours 1)"):
        assert setting in text, setting
    assert "run_spark_pull.ps1" in text
    assert "http://" not in text and "https://" not in text


def test_the_ibkr_unregister_script_removes_all_three_tasks():
    text = UNREGISTER_IBKR.read_text(encoding="utf-8")
    for name in ('"IBKR Recorder"', '"IBKR Recorder Health"', '"IBKR Paper Sync"'):
        assert name in text, name


def swap(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                           "Bypass", "-File", str(SWAP), "-UserHome", str(home), *args],
                          capture_output=True, text=True, errors="replace", timeout=120)


@pytest.fixture
def home(tmp_path) -> Path:
    """A home directory whose quant_data is a live root with one file of state."""
    lane = tmp_path / "home" / "quant_data" / "ibkr_ticks"
    lane.mkdir(parents=True)
    (lane / "live_state.txt").write_text("live", encoding="utf-8")
    return tmp_path / "home"


@windows_only
def test_the_swap_keeps_the_live_root_and_leaves_an_empty_marked_mirror(home):
    done = swap(home)
    assert done.returncode == 0, done.stdout + done.stderr
    kept = home / "quant_data_zenbook_final" / "ibkr_ticks" / "live_state.txt"
    assert kept.read_text(encoding="utf-8") == "live"
    assert sorted(p.name for p in (home / "quant_data").iterdir()) == [MARKER]


@windows_only
def test_a_rename_windows_refuses_never_puts_the_marker_into_the_live_root(home):
    held = home / "quant_data" / "ibkr_ticks" / "live_state.txt"
    with held.open("rb"):
        done = swap(home)
    assert done.returncode == 1
    assert "swap refused" in done.stdout and "nothing was changed" in done.stdout
    assert not (home / "quant_data" / MARKER).exists()
    assert not (home / "quant_data_zenbook_final").exists()
    assert held.read_text(encoding="utf-8") == "live"


@windows_only
def test_a_second_swap_is_refused_and_changes_nothing(home):
    assert swap(home).returncode == 0
    done = swap(home)
    assert done.returncode == 1 and "swap refused" in done.stdout
    assert sorted(p.name for p in home.iterdir()) == ["quant_data", "quant_data_zenbook_final"]
    assert sorted(p.name for p in (home / "quant_data").iterdir()) == [MARKER]


@windows_only
def test_undo_puts_the_kept_root_back_and_retires_the_mirror(home):
    assert swap(home).returncode == 0
    (home / "quant_data" / "pulled.txt").write_text("a pulled copy", encoding="utf-8")

    done = swap(home, "-Undo")

    assert done.returncode == 0, done.stdout + done.stderr
    assert (home / "quant_data" / "ibkr_ticks" / "live_state.txt").read_text(encoding="utf-8") == "live"
    assert not (home / "quant_data" / MARKER).exists()
    assert (home / "quant_data_mirror_retired" / "pulled.txt").is_file()
    assert not (home / "quant_data_zenbook_final").exists()


@windows_only
def test_an_undo_that_cannot_put_the_kept_root_back_puts_the_mirror_back_too(home):
    assert swap(home).returncode == 0
    (home / "quant_data" / "pulled.txt").write_text("a pulled copy", encoding="utf-8")
    held = home / "quant_data_zenbook_final" / "ibkr_ticks" / "live_state.txt"

    with held.open("rb"):
        done = swap(home, "-Undo")

    assert done.returncode == 1, done.stdout + done.stderr
    assert "swap refused" in done.stdout
    assert "the mirror is back at" in done.stdout and str(home / "quant_data") in done.stdout
    assert (home / "quant_data" / MARKER).is_file()
    assert (home / "quant_data" / "pulled.txt").read_text(encoding="utf-8") == "a pulled copy"
    assert not (home / "quant_data_mirror_retired").exists()
    assert held.read_text(encoding="utf-8") == "live"
    assert sorted(p.name for p in home.iterdir()) == ["quant_data", "quant_data_zenbook_final"]

    again = swap(home, "-Undo")  # the file is released now

    assert again.returncode == 0, again.stdout + again.stderr
    live_file = home / "quant_data" / "ibkr_ticks" / "live_state.txt"
    assert live_file.read_text(encoding="utf-8") == "live"
    assert not (home / "quant_data" / MARKER).exists()
    assert (home / "quant_data_mirror_retired" / MARKER).is_file()
    assert (home / "quant_data_mirror_retired" / "pulled.txt").is_file()
    assert not (home / "quant_data_zenbook_final").exists()


@windows_only
def test_an_undo_with_no_mirror_to_retire_that_cannot_put_the_kept_root_back_changes_nothing(home):
    assert swap(home).returncode == 0
    shutil.rmtree(home / "quant_data")
    held = home / "quant_data_zenbook_final" / "ibkr_ticks" / "live_state.txt"

    with held.open("rb"):
        done = swap(home, "-Undo")

    assert done.returncode == 1, done.stdout + done.stderr
    assert "swap refused" in done.stdout and "nothing was changed" in done.stdout
    assert "the mirror" not in done.stdout  # there was none to put back, so none is mentioned
    assert sorted(p.name for p in home.iterdir()) == ["quant_data_zenbook_final"]
    assert held.read_text(encoding="utf-8") == "live"


@windows_only
def test_undo_refuses_to_move_a_root_that_is_not_the_mirror(home):
    (home / "quant_data_zenbook_final").mkdir()

    done = swap(home, "-Undo")

    assert done.returncode == 1
    assert "is not the mirror" in done.stdout
    assert (home / "quant_data" / "ibkr_ticks" / "live_state.txt").is_file()
    assert (home / "quant_data_zenbook_final").is_dir()
