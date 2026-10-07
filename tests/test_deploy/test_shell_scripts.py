"""scripts/spark/*.sh: syntax, line endings, dry runs and refusals.

Every script is run by a real bash in its dry-run mode, which prints the
commands and calls neither ssh nor rsync. On Windows that bash is Git Bash;
System32\\bash.exe is the WSL launcher and is never used here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "spark"
UNIT_DIR = REPO_ROOT / "deploy" / "spark" / "systemd"
SUDOERS = REPO_ROOT / "deploy" / "spark" / "sudoers" / "forecast-runtime"
MARKER = ".mirror-of-spark"
ALL_SCRIPTS = ["deploy.sh", "env.sh", "mirror_live.sh", "pull_snapshot.sh", "push_snapshot.sh",
               "research_run.sh", "rollback.sh"]
WITH_OPTIONS = [name for name in ALL_SCRIPTS if name != "env.sh"]


def _bash() -> str | None:
    if sys.platform == "win32":
        git_bash = (Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                    / "Git" / "bin" / "bash.exe")
        return str(git_bash) if git_bash.is_file() else None
    return shutil.which("bash")


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash on this machine")


def run_script(name: str, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, (SCRIPTS / name).as_posix(), *args],
                          env={**os.environ, **env}, capture_output=True, text=True, timeout=120)


def sudo_allowed() -> set[tuple[str, str]]:
    """(verb, unit) pairs the sudoers drop-in lets the operator run without a password."""
    text = SUDOERS.read_text(encoding="utf-8")
    return set(re.findall(r"/usr/bin/systemctl (\S+) (\S+?)(?:,|\s|$)", text))


@pytest.fixture
def mirror(tmp_path) -> Path:
    """A root that carries the marker."""
    root = tmp_path / "mirror"
    root.mkdir()
    (root / MARKER).write_text("mirror of spark:/srv/quant_data\n", encoding="utf-8")
    return root


@pytest.fixture
def unmarked(tmp_path) -> Path:
    """A root that does not: it stands for a live root."""
    root = tmp_path / "live"
    root.mkdir()
    (root / "keep.txt").write_text("live state", encoding="utf-8")
    return root


# ----------------------------------------------------------------- every script


def test_the_directory_holds_exactly_these_shell_scripts():
    assert sorted(p.name for p in SCRIPTS.glob("*.sh")) == ALL_SCRIPTS


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_the_script_parses(name):
    done = subprocess.run([BASH, "-n", (SCRIPTS / name).as_posix()],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_the_script_is_lf(name):
    assert b"\r" not in (SCRIPTS / name).read_bytes()


@pytest.mark.parametrize("name", WITH_OPTIONS)
def test_help_prints_the_usage_and_exits_0(name):
    done = run_script(name, "--help")
    assert done.returncode == 0, done.stderr
    assert f"scripts/spark/{name}" in done.stdout


@pytest.mark.parametrize("name", WITH_OPTIONS)
def test_an_unknown_argument_is_a_usage_error(name):
    done = run_script(name, "--no-such-option")
    assert done.returncode == 2
    assert "unknown argument" in done.stderr


# ----------------------------------------------------------------- push_snapshot.sh


def test_push_refuses_a_source_that_is_a_mirror(mirror, tmp_path):
    snapshot = tmp_path / "snapshot"
    done = run_script("push_snapshot.sh", QUANT_DATA_SRC=mirror.as_posix(),
                      QUANT_DATA_SNAPSHOT=snapshot.as_posix())
    assert done.returncode == 3
    assert MARKER in done.stderr and "never pushed" in done.stderr
    assert not snapshot.exists()


def test_push_dry_run_feeds_the_research_mirror_by_default(unmarked, tmp_path):
    done = run_script("push_snapshot.sh", "--dry-run", QUANT_DATA_SRC=unmarked.as_posix(),
                      QUANT_DATA_SNAPSHOT=(tmp_path / "snapshot").as_posix())
    assert done.returncode == 0, done.stderr
    freeze, push, verify = [line for line in done.stdout.splitlines()
                            if line.startswith("DRY-RUN: ")]
    assert "--delete" in freeze and "--delete" not in push
    assert push.endswith(" spark:quant_data/")
    assert " spark cd ~/work/forecast-model " in verify
    assert verify.endswith("scripts/spark/verify_mirror.py --root ~/quant_data")
    assert "climate_data/raw/" in push
    assert not (tmp_path / "snapshot").exists()


def test_push_cutover_goes_into_the_live_root_as_trading(unmarked, tmp_path):
    done = run_script("push_snapshot.sh", "--cutover", "--include-raw", "--dry-run",
                      QUANT_DATA_SRC=unmarked.as_posix(),
                      QUANT_DATA_SNAPSHOT=(tmp_path / "snapshot").as_posix())
    assert done.returncode == 0, done.stderr
    _freeze, push, verify = [line for line in done.stdout.splitlines()
                             if line.startswith("DRY-RUN: ")]
    assert push.endswith(" spark-rt:/srv/quant_data/")
    assert " spark-rt cd /home/trading/forecast-model " in verify
    assert verify.endswith("scripts/spark/verify_mirror.py --root /srv/quant_data")
    assert "climate_data/raw/" not in push


# ----------------------------------------------------------------- pull_snapshot.sh


def test_pull_refuses_a_root_without_the_marker(unmarked):
    done = run_script("pull_snapshot.sh", "--dry-run", QUANT_DATA_MIRROR=unmarked.as_posix())
    assert done.returncode == 3
    assert "does not carry" in done.stderr and MARKER in done.stderr
    assert sorted(p.name for p in unmarked.iterdir()) == ["keep.txt"]


def test_pull_dry_run_copies_the_root_and_the_backups_and_never_deletes(mirror, tmp_path):
    backups = tmp_path / "backups"
    done = run_script("pull_snapshot.sh", "--dry-run", QUANT_DATA_MIRROR=mirror.as_posix(),
                      QUANT_DATA_BACKUPS_MIRROR=backups.as_posix())
    assert done.returncode == 0, done.stderr
    root_line, backups_line = [line for line in done.stdout.splitlines()
                               if line.startswith("DRY-RUN: rsync ")]
    assert f" spark:/srv/quant_data/ {mirror.as_posix()}/" in root_line
    assert f" spark:/srv/quant_data_backups/ {backups.as_posix()}/" in backups_line
    assert "--delete" not in done.stdout
    assert "BatchMode=yes" in root_line and f"/{MARKER}" in root_line
    assert "climate_data/raw/" in root_line
    assert not backups.exists()


def test_pull_include_raw_drops_the_raw_exclude(mirror):
    done = run_script("pull_snapshot.sh", "--dry-run", "--include-raw",
                      QUANT_DATA_MIRROR=mirror.as_posix())
    assert done.returncode == 0, done.stderr
    assert "climate_data/raw/" not in done.stdout


# ----------------------------------------------------------------- mirror_live.sh


def test_mirror_refuses_a_destination_without_the_marker(unmarked, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    done = run_script("mirror_live.sh", "--dry-run", LIVE_ROOT=source.as_posix(),
                      MIRROR_ROOT=unmarked.as_posix())
    assert done.returncode == 3
    assert "does not carry" in done.stderr


def test_mirror_refuses_a_source_that_is_a_mirror(mirror, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / MARKER).write_text("", encoding="utf-8")
    done = run_script("mirror_live.sh", "--dry-run", LIVE_ROOT=other.as_posix(),
                      MIRROR_ROOT=mirror.as_posix())
    assert done.returncode == 3
    assert "not the live root" in done.stderr


def test_mirror_dry_run_reads_the_live_root_and_never_deletes(mirror, unmarked):
    done = run_script("mirror_live.sh", "--dry-run", LIVE_ROOT=unmarked.as_posix(),
                      MIRROR_ROOT=mirror.as_posix())
    assert done.returncode == 0, done.stderr
    (line,) = [line for line in done.stdout.splitlines() if line.startswith("DRY-RUN: rsync ")]
    assert line.endswith(f" {unmarked.as_posix()}/ {mirror.as_posix()}/")
    assert "--delete" not in line and "ibkr_ticks/state/" in line


# ----------------------------------------------------------------- deploy.sh


def test_deploy_dry_run_pushes_then_compares_then_restarts():
    done = run_script("deploy.sh", "--dry-run")
    assert done.returncode == 0, done.stderr
    out = done.stdout
    push = out.index("DRY-RUN: git push spark-rt main")
    compare = out.index("cmp -s")
    wait = out.index("recorder_state.json")
    restart = out.index("sudo -n /usr/bin/systemctl restart ibkr-recorder.service")
    assert push < compare < wait < restart
    assert "/etc/systemd/system/" in out


def test_deploy_no_restart_leaves_the_recorder_alone():
    done = run_script("deploy.sh", "--dry-run", "--no-restart")
    assert done.returncode == 0, done.stderr
    assert "DRY-RUN: git push spark-rt main" in done.stdout
    assert "systemctl restart" not in done.stdout
    assert "--no-restart" in done.stdout


def test_the_restart_a_deploy_asks_for_is_one_sudo_allows():
    done = run_script("deploy.sh", "--dry-run")
    calls = set(re.findall(r"sudo -n /usr/bin/systemctl (\S+) (\S+)", done.stdout))
    assert calls == {("restart", "ibkr-recorder.service")}
    assert calls <= sudo_allowed()


# ----------------------------------------------------------------- rollback.sh


def test_rollback_refuses_a_missing_destination(tmp_path):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=(tmp_path / "absent").as_posix())
    assert done.returncode == 3
    assert "does not exist" in done.stderr


def test_rollback_refuses_a_destination_that_is_a_mirror(mirror):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=mirror.as_posix())
    assert done.returncode == 3
    assert "it is a mirror" in done.stderr


def test_rollback_dry_run_gives_the_session_up_before_it_copies(unmarked):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=unmarked.as_posix())
    assert done.returncode == 0, done.stderr
    out = done.stdout
    stop_units = out.index("/usr/bin/systemctl stop ")
    stop_gateway = out.index("docker compose stop")
    check = out.index("--port 4002 --container ibgw")
    copy = out.index("DRY-RUN: rsync ")
    by_hand = out.index("by hand")
    assert stop_units < stop_gateway < check < copy < by_hand
    copy_line = out[copy:].splitlines()[0]
    assert copy_line.endswith(f" spark:/srv/quant_data/ {unmarked.as_posix()}/")
    assert "--delete" not in copy_line
    assert "session_check.py" in out
    assert sorted(p.name for p in unmarked.iterdir()) == ["keep.txt"]


def test_rollback_stops_every_runtime_unit_with_a_command_sudo_allows(unmarked):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=unmarked.as_posix())
    calls = re.findall(r"sudo -n /usr/bin/systemctl (\S+) (\S+)", done.stdout)
    assert calls and {verb for verb, _unit in calls} == {"stop"}
    assert set(calls) <= sudo_allowed()
    runtime_units = {p.name for p in UNIT_DIR.iterdir()} - {"ibgw-owner-rule.service"}
    assert {unit for _verb, unit in calls} == runtime_units
    timers = [i for i, (_verb, unit) in enumerate(calls) if unit.endswith(".timer")]
    services = [i for i, (_verb, unit) in enumerate(calls) if unit.endswith(".service")]
    assert max(timers) < min(services)  # nothing may start a service we have just stopped


# ----------------------------------------------------------------- real runs against stand-ins
#
# A dry run shows the commands; these run the scripts for real, with a stand-in for ssh
# (and for git), to see what they do when the other machine answers badly.


def stand_in(tmp_path: Path, name: str, body: str) -> str:
    """An executable script bash can run in place of ssh or git; returns its POSIX path."""
    path = tmp_path / name
    path.write_bytes(("#!/usr/bin/env bash\n" + body).encode("utf-8"))
    path.chmod(0o755)
    return path.as_posix()


SSH_THAT_OBEYS = 'case "$*" in *"python3 -"*) cat >/dev/null ;; esac\nexit 0\n'


def test_rollback_says_not_to_log_in_when_the_spark_cannot_be_reached(unmarked, tmp_path):
    ssh = stand_in(tmp_path, "ssh_down", "exit 255\n")
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode == 4
    assert "Do NOT log in on the Zenbook" in done.stderr
    assert "rsync" not in done.stdout
    assert sorted(p.name for p in unmarked.iterdir()) == ["keep.txt"]


def test_rollback_says_not_to_log_in_when_the_gateway_cannot_be_stopped(unmarked, tmp_path):
    ssh = stand_in(tmp_path, "ssh_docker_refuses",
                   'case "$*" in *"docker compose stop"*) exit 1 ;; esac\nexit 0\n')
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode == 4
    assert "container could not be stopped" in done.stderr
    assert "Do NOT log in on the Zenbook" in done.stderr


def test_rollback_says_not_to_log_in_when_the_session_check_fails(unmarked, tmp_path):
    ssh = stand_in(tmp_path, "ssh_gateway_still_there",
                   'case "$*" in *"python3 -"*) cat >/dev/null; echo "a Gateway is here"; exit 1 ;; esac\n'
                   "exit 0\n")
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode == 4
    assert "session check on the Spark did not pass" in done.stderr
    assert "Do NOT log in on the Zenbook" in done.stderr


def test_rollback_no_copy_ends_after_the_session_check(tmp_path):
    ssh = stand_in(tmp_path, "ssh_ok", SSH_THAT_OBEYS)
    done = run_script("rollback.sh", "--no-copy", SPARK_SSH=ssh,
                      ROLLBACK_DEST=(tmp_path / "absent").as_posix())  # not needed without a copy
    assert done.returncode == 0, done.stderr
    assert "nothing was copied" in done.stdout
    assert "rsync" not in done.stdout and "by hand" not in done.stdout


def test_a_failed_copy_says_the_session_is_released_and_to_run_again(unmarked, tmp_path):
    # the stand-in obeys but speaks no rsync protocol (and Git Bash has no rsync at all)
    ssh = stand_in(tmp_path, "ssh_ok", SSH_THAT_OBEYS)
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode not in (0, 4)
    assert "run this script again" in done.stderr
    assert "Do NOT log in" not in done.stderr
    assert "by hand" not in done.stdout


DEPLOY_SSH = """\
echo "$*" >> "$STAND_IN_LOG"
case "$*" in
  *"log --oneline"*) echo "abc1234 a commit" ;;
  *"cmp -s"*) [ -z "${STAND_IN_CHANGED:-}" ] || echo "$STAND_IN_CHANGED" ;;
  *"recorder_state.json"*) echo "heartbeat is 30s old" ;;
  *"systemctl restart"*) ;;
  *"is-active ibkr-recorder.service") echo "${STAND_IN_STATE:-active}"; [ "${STAND_IN_STATE:-active}" = active ] || exit 3 ;;
esac
exit 0
"""


def run_deploy(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    log = tmp_path / "calls.log"
    done = run_script("deploy.sh", SSH=stand_in(tmp_path, "ssh_for_deploy", DEPLOY_SSH),
                      DEPLOY_GIT=stand_in(tmp_path, "git_ok", 'echo "git $*"\nexit 0\n'),
                      STAND_IN_LOG=log.as_posix(), **env)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done, calls


def test_deploy_restarts_a_running_recorder_once_after_the_unit_files_agree(tmp_path):
    done, calls = run_deploy(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "git push spark-rt main" in done.stdout
    restarts = [i for i, call in enumerate(calls) if "systemctl restart ibkr-recorder.service" in call]
    compares = [i for i, call in enumerate(calls) if "cmp -s" in call]
    assert len(restarts) == 1 and compares and compares[0] < restarts[0]
    assert calls[restarts[0]].startswith("spark ")  # the operator's host, not the runtime user's


def test_deploy_does_not_start_a_recorder_that_is_not_running(tmp_path):
    done, calls = run_deploy(tmp_path, STAND_IN_STATE="inactive")
    assert done.returncode == 0, done.stderr
    assert "a deploy does not start it" in done.stdout
    assert not [call for call in calls if "systemctl restart" in call]


def test_deploy_stops_for_a_unit_file_that_must_be_installed_by_hand(tmp_path):
    done, calls = run_deploy(tmp_path, STAND_IN_CHANGED="ibkr-health.timer")
    assert done.returncode == 3
    assert ("sudo install -m 644 -o root -g root /home/trading/forecast-model/deploy/spark/systemd/"
            "ibkr-health.timer /etc/systemd/system/ibkr-health.timer") in done.stdout
    assert "sudo systemctl daemon-reload" in done.stdout
    assert not [call for call in calls if "systemctl restart" in call or "recorder_state.json" in call]


# ----------------------------------------------------------------- research_run.sh


def test_research_run_sets_the_ceiling_and_the_kill_priority():
    done = run_script("research_run.sh", "--dry-run", "--", "python", "train.py", "--epochs", "3")
    assert done.returncode == 0, done.stderr
    (line,) = done.stdout.splitlines()
    assert line.startswith("DRY-RUN: systemd-run --user --scope ")
    assert "-p MemoryMax=15G" in line and "-p MemorySwapMax=0" in line
    assert "echo 500 > /proc/self/oom_score_adj" in line
    assert line.endswith(" python train.py --epochs 3")


def test_research_run_takes_a_larger_ceiling_when_told():
    done = run_script("research_run.sh", "--memory", "60G", "--dry-run", "--", "true")
    assert done.returncode == 0, done.stderr
    assert "-p MemoryMax=60G" in done.stdout and "MemoryMax=15G" not in done.stdout


def test_research_run_refuses_a_missing_command_and_a_ceiling_without_a_unit():
    assert run_script("research_run.sh", "--dry-run").returncode == 2
    done = run_script("research_run.sh", "--memory", "15", "--dry-run", "--", "true")
    assert done.returncode == 2
    assert "15G" in done.stderr
