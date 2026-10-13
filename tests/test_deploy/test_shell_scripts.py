"""scripts/spark/*.sh: syntax, line endings, dry runs and refusals."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
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


COPIES_FROM_THE_SPARK = ["pull_snapshot.sh", "mirror_live.sh", "rollback.sh"]
MARKER_ENTRY = "/$MARKER"
HOT_SQLITE = ["*-journal", "*.db-wal", "*.db-shm"]


def exclude_patterns(name: str) -> list[str]:
    """The patterns of the EXCLUDES=(--exclude PATTERN ...) array a script defines."""
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    match = re.search(r"^EXCLUDES=\((.*?)\)", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name} defines no EXCLUDES array"
    tokens = shlex.split(match.group(1))
    assert tokens[0::2] == ["--exclude"] * (len(tokens) // 2), tokens
    return tokens[1::2]


def test_the_exclude_lists_of_the_four_copy_scripts_cannot_drift_apart():
    lists = {name: exclude_patterns(name) for name in [*COPIES_FROM_THE_SPARK, "push_snapshot.sh"]}
    assert all(MARKER_ENTRY in lists[name] for name in COPIES_FROM_THE_SPARK)
    volatile = {name: [p for p in patterns if p != MARKER_ENTRY] for name, patterns in lists.items()}
    shared = volatile["pull_snapshot.sh"]
    assert volatile["mirror_live.sh"] == volatile["push_snapshot.sh"] == shared, volatile
    assert len(shared) >= 5  # state/, *.tmp, the journal, wal and shm files
    assert [p for p in HOT_SQLITE if p not in shared] == []
    assert volatile["rollback.sh"] == [p for p in shared if p not in HOT_SQLITE], volatile
    assert lists["pull_snapshot.sh"] == lists["mirror_live.sh"]


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


def test_push_cutover_refuses_a_source_that_is_a_mirror(mirror, tmp_path):
    snapshot = tmp_path / "snapshot"
    done = run_script("push_snapshot.sh", "--cutover", "--dry-run", QUANT_DATA_SRC=mirror.as_posix(),
                      QUANT_DATA_SNAPSHOT=snapshot.as_posix())
    assert done.returncode == 3
    assert MARKER in done.stderr and "never pushed" in done.stderr
    assert "DRY-RUN" not in done.stdout  # it refused before it printed a single command
    assert not snapshot.exists()
    assert sorted(p.name for p in mirror.iterdir()) == [MARKER]


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


NOT_TOUCHED = "the Spark was not touched and may still hold the session; --no-copy releases it"


def test_rollback_refuses_a_missing_destination(tmp_path):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=(tmp_path / "absent").as_posix())
    assert done.returncode == 3
    assert "does not exist" in done.stderr
    assert NOT_TOUCHED in done.stderr


def test_rollback_refuses_a_destination_that_is_a_mirror(mirror):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=mirror.as_posix())
    assert done.returncode == 3
    assert "it is a mirror" in done.stderr
    assert NOT_TOUCHED in done.stderr


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


def test_rollback_copies_the_journal_wal_and_shm_files_with_their_databases(unmarked):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=unmarked.as_posix())
    assert done.returncode == 0, done.stderr
    (copy_line,) = [line for line in done.stdout.splitlines() if line.startswith("DRY-RUN: rsync ")]
    for pattern in HOT_SQLITE:
        assert f"--exclude {pattern}" not in copy_line, copy_line
    assert "--exclude ibkr_ticks/state/" in copy_line
    assert "--exclude *.tmp" in copy_line
    assert f"--exclude /{MARKER}" in copy_line
    assert copy_line.split().count("--exclude") == 3, copy_line  # and nothing else is left out


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


def test_rollback_has_the_restored_book_checked_before_the_kept_root_goes_back(unmarked):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=unmarked.as_posix())
    assert done.returncode == 0, done.stderr
    by_hand = done.stdout[done.stdout.index("== 5/5 by hand"):]
    disable = by_hand.index("Disable-ScheduledTask -TaskPath '\\Spark\\' -TaskName 'Spark Pull'")
    check = by_hand.index("python scripts/spark/restore_check.py --from ~/quant_data_zenbook_final "
                          "--scratch <a fresh directory>")
    undo = by_hand.index("swap_live_root_for_mirror.ps1 -Undo")
    enable = by_hand.index("Enable-ScheduledTask")
    assert disable < check < undo < enable
    assert "RESTORE OK" in by_hand[check:undo]  # what the account holder waits for


def test_the_restore_check_the_rollback_prints_is_one_restore_check_accepts():
    done = subprocess.run([sys.executable, (SCRIPTS / "restore_check.py").as_posix(), "--help"],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "--from" in done.stdout and "--scratch" in done.stdout
    assert "RESTORE OK" in (SCRIPTS / "restore_check.py").read_text(encoding="utf-8")


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


def on_path(tmp_path: Path, name: str, body: str) -> dict[str, str]:
    """The environment in which `name` is a stand-in script found first on PATH."""
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    stand_in(directory, name, body)
    return {"PATH": os.pathsep.join([str(directory), os.environ["PATH"]])}


@pytest.mark.parametrize("status", [1, 3, 4, 23, 127])
def test_a_failed_copy_exits_5_and_says_the_session_is_released_and_to_run_again(
        unmarked, tmp_path, status):
    ssh = stand_in(tmp_path, "ssh_ok", SSH_THAT_OBEYS)
    rsync = on_path(tmp_path, "rsync", f"exit {status}\n")
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh, **rsync)
    assert done.returncode == 5, (done.stdout, done.stderr)
    assert f"copy failed (rsync exit {status}); run this script again" in done.stderr
    assert "Do NOT log in" not in done.stderr
    assert "by hand" not in done.stdout


def test_a_copy_that_succeeds_still_ends_with_the_by_hand_list(unmarked, tmp_path):
    ssh = stand_in(tmp_path, "ssh_ok", SSH_THAT_OBEYS)
    rsync = on_path(tmp_path, "rsync", "exit 0\n")
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh, **rsync)
    assert done.returncode == 0, done.stderr
    assert "== 5/5 by hand" in done.stdout and "copy failed" not in done.stderr


def test_runbook_7_9_tells_the_operator_what_each_exit_and_the_restore_check_mean():
    text = (REPO_ROOT / "docs" / "spark_runbook.md").read_text(encoding="utf-8")
    section = text.split("### 7.9 ", 1)[1].split("### 7.10 ", 1)[0]
    for status in (2, 3, 4, 5):
        assert re.search(rf"\bExit {status}\b", section), f"7.9 does not say what exit {status} is"
    flat = " ".join(section.split())  # the paragraphs are wrapped
    assert "copy failed (rsync exit N)" in flat
    assert "the Spark was not touched, so it may still hold the session" in flat
    assert "`--no-copy` releases it" in flat
    check = section.index("python scripts/spark/restore_check.py --from ~/quant_data_zenbook_final")
    assert "--scratch <a fresh directory>" in section[check:check + 140]
    assert check < section.index("swap_live_root_for_mirror.ps1")
    assert "RESTORE OK" in section[check:]


def test_the_exit_list_in_the_header_names_every_status_the_script_ends_with():
    text = (SCRIPTS / "rollback.sh").read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    used = {int(n) for n in re.findall(r"\bexit (\d+)\b", "\n".join(code))}
    assert used == {0, 2, 3, 4, 5}, used  # 5 is the copy that failed; nothing else uses it
    (exit_list,) = re.findall(r"^# Exit:(.*?)(?=^[^#])", text, re.MULTILINE | re.DOTALL)
    listed = {int(n) for n in re.findall(r"\b(\d)\b(?= [a-zA-Z(])", exit_list)}
    assert used <= listed, (used, exit_list)
    assert "rsync's own status" in exit_list


SSH_OPTS = "-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

SSH_THAT_LOGS = """\
echo "argv: $*" >> "$STAND_IN_LOG"
case "$*" in
  *"python3 -"*)
    cat > "$STAND_IN_STDIN"
    echo "stdin: $(wc -c < "$STAND_IN_STDIN" | tr -d ' ') bytes" >> "$STAND_IN_LOG" ;;
esac
exit 0
"""


def run_logged_rollback(tmp_path: Path) -> tuple[list[str], Path]:
    log, taken = tmp_path / "calls.log", tmp_path / "stdin.bin"
    ssh = stand_in(tmp_path, "ssh_that_logs", SSH_THAT_LOGS)
    done = run_script("rollback.sh", "--no-copy", SPARK_SSH=ssh,
                      STAND_IN_LOG=log.as_posix(), STAND_IN_STDIN=taken.as_posix())
    assert done.returncode == 0, done.stderr
    return log.read_text(encoding="utf-8").splitlines(), taken


def test_rollback_sends_the_session_check_once_with_this_machines_copy_on_stdin(tmp_path):
    lines, taken = run_logged_rollback(tmp_path)
    calls = [line.removeprefix("argv: ") for line in lines if line.startswith("argv: ")]
    assert len(calls) == 3  # stop the units, stop the container, check
    assert [call for call in calls if "python3 -" in call] == [
        f"{SSH_OPTS} spark python3 - --port 4002 --container ibgw"]
    assert calls[2].endswith(" python3 - --port 4002 --container ibgw")  # and it comes last
    shipped = (SCRIPTS / "session_check.py").read_bytes()
    assert f"stdin: {len(shipped)} bytes" in lines
    assert hashlib.sha256(taken.read_bytes()).hexdigest() == hashlib.sha256(shipped).hexdigest()


def test_every_direct_ssh_call_of_a_rollback_carries_the_connection_options(tmp_path):
    lines, _taken = run_logged_rollback(tmp_path)
    calls = [line.removeprefix("argv: ") for line in lines if line.startswith("argv: ")]
    assert len(calls) == 3
    assert all(call.startswith(f"{SSH_OPTS} spark ") for call in calls), calls


SSH_THAT_GETS_INTERRUPTED = """\
kill -"$STAND_IN_SIGNAL" "$PPID"
[ "$STAND_IN_SIGNAL" != INT ] || kill -INT $$
sleep 2
exit 0
"""


@pytest.mark.parametrize("signal_name", ["TERM", "HUP", "INT"])
def test_rollback_interrupted_before_the_check_says_not_to_log_in(unmarked, tmp_path, signal_name):
    ssh = stand_in(tmp_path, "ssh_interrupted", SSH_THAT_GETS_INTERRUPTED)
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh,
                      STAND_IN_SIGNAL=signal_name)
    assert done.returncode != 0
    assert done.stderr.count("Do NOT log in on the Zenbook") == 1
    assert "could not be stopped" not in done.stderr  # no failed step spoke: the exit trap did
    assert "== 2/5" not in done.stdout and "rsync" not in done.stdout
    assert sorted(p.name for p in unmarked.iterdir()) == ["keep.txt"]


def test_rollback_warns_once_not_twice_when_a_step_fails(unmarked, tmp_path):
    ssh = stand_in(tmp_path, "ssh_down", "exit 255\n")
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode == 4
    assert done.stderr.count("Do NOT log in on the Zenbook") == 1  # the step spoke; the trap adds nothing
    assert "stopped before the session check passed" not in done.stderr


def test_rollback_stays_quiet_when_the_spark_was_never_asked(mirror, unmarked, tmp_path):
    runs = [
        run_script("rollback.sh", "--help"),
        run_script("rollback.sh", "--no-such-option"),
        run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=(tmp_path / "absent").as_posix()),
        run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=mirror.as_posix()),
        run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=unmarked.as_posix()),
    ]
    assert [done.returncode for done in runs] == [0, 2, 3, 3, 0]
    for done in runs:
        assert "Do NOT log in" not in done.stderr


DEPLOY_SSH = """\
echo "$*" >> "$STAND_IN_LOG"
case "$*" in
  *"log --oneline"*) echo "abc1234 a commit" ;;
  *"cmp -s"*) [ -z "${STAND_IN_CHANGED:-}" ] || echo "$STAND_IN_CHANGED" ;;
  *"recorder_state.json"*) echo "heartbeat is 30s old" ;;
  *"systemctl restart"*) ;;
  *"is-active ibkr-recorder.service")
    # the first question and the one asked again before the restart may be answered differently
    if [ "$(grep -c 'is-active ibkr-recorder.service' "$STAND_IN_LOG")" -lt 2 ]; then
      state="${STAND_IN_STATE:-active}"; code="${STAND_IN_STATE_EXIT:-}"
    else
      state="${STAND_IN_STATE_AFTER:-${STAND_IN_STATE:-active}}"; code="${STAND_IN_STATE_EXIT_AFTER:-}"
    fi
    [ -z "$code" ] || exit "$code"
    echo "$state"
    [ "$state" = active ] || exit 3 ;;
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


def test_deploy_asks_again_right_before_the_restart(tmp_path):
    done, calls = run_deploy(tmp_path)
    assert done.returncode == 0, done.stderr
    asks = [i for i, call in enumerate(calls) if call.endswith("is-active ibkr-recorder.service")]
    waits = [i for i, call in enumerate(calls) if "recorder_state.json" in call]
    restarts = [i for i, call in enumerate(calls) if "systemctl restart" in call]
    assert len(asks) == 2 and len(waits) == 1 and len(restarts) == 1
    assert asks[0] < waits[0] < asks[1] == restarts[0] - 1  # nothing in between the answer and the restart


def test_deploy_stops_when_the_recorders_state_cannot_be_asked(tmp_path):
    done, calls = run_deploy(tmp_path, STAND_IN_STATE_EXIT="255")
    assert done.returncode == 255
    assert "state query" in done.stderr and "is-active ibkr-recorder.service" in done.stderr
    assert "NOT restarted" in done.stderr
    assert "not running" not in done.stdout and "== 4/4" not in done.stdout
    assert not [call for call in calls if "systemctl restart" in call or "recorder_state.json" in call]


def test_deploy_does_not_restart_a_recorder_that_stopped_during_the_wait(tmp_path):
    done, calls = run_deploy(tmp_path, STAND_IN_STATE_AFTER="inactive")
    assert done.returncode == 0, done.stderr
    assert "recorder no longer active; NOT restarted" in done.stdout
    assert [call for call in calls if "recorder_state.json" in call]  # it did wait
    assert not [call for call in calls if "systemctl restart" in call]
    assert "== 4/4" in done.stdout  # and the units' states were still reported


def test_deploy_stops_when_the_second_answer_cannot_be_had(tmp_path):
    done, calls = run_deploy(tmp_path, STAND_IN_STATE_EXIT_AFTER="255")
    assert done.returncode == 255
    assert "state query" in done.stderr and "NOT restarted" in done.stderr
    assert not [call for call in calls if "systemctl restart" in call]


CLONE = "/home/trading/forecast-model"
INSTALLED = "/etc/systemd/system"
HEARTBEAT = "/srv/quant_data/ibkr_ticks/state/recorder_state.json"


@pytest.fixture(scope="module")
def remote() -> dict[str, str]:
    """The remote command lines of a deploy by what they do, as its dry run prints them."""
    done = run_script("deploy.sh", "--dry-run", SSH="ssh")
    assert done.returncode == 0, done.stderr
    by_step: dict[str, list[tuple[str, str]]] = {}
    step = ""
    for line in done.stdout.splitlines():
        header = re.match(r"== (\d)/4 ", line)
        if header:
            step = header.group(1)
        elif line.startswith("DRY-RUN: ssh "):
            host, _, command = line.removeprefix("DRY-RUN: ssh ").partition(" ")
            by_step.setdefault(step, []).append((host, command))
    ((diff_host, diff),) = by_step["2"]
    ((state_host, state), (wait_host, wait), (again_host, again), (restart_host, restart)) = by_step["3"]
    ((status_host, status),) = by_step["4"]
    assert state == again
    assert {diff_host, state_host, wait_host, again_host, status_host} == {"spark-rt"}
    assert restart_host == "spark"
    return {"diff": diff, "state": state, "wait": wait, "restart": restart, "status": status}


def run_command_line(tmp_path: Path, command: str, prologue: str = "",
                     **env: str) -> subprocess.CompletedProcess:
    script = tmp_path / "command_line.sh"
    script.write_bytes((prologue + command + "\n").encode("utf-8"))
    return subprocess.run([BASH, script.as_posix()], env={**os.environ, **env},
                          capture_output=True, text=True, timeout=300)


SLEEP_STUB = ("__sleeps=0\n"
              'sleep() { echo "$1" >> "$SLEEP_LOG"; __sleeps=$((__sleeps + 1)); '
              '[ "$__sleeps" -lt 300 ] || exit 99; }\n')
CLOCK_STUB = ('date() { echo "$FAKE_NOW"; }\n'
              'stat() { [ -n "$FAKE_MTIME" ] || return 1; echo "$FAKE_MTIME"; }\n')


@pytest.mark.parametrize("name", ["diff", "state", "wait", "restart", "status"])
def test_each_remote_command_line_parses(remote, tmp_path, name):
    script = tmp_path / f"{name}.sh"
    script.write_bytes((remote[name] + "\n").encode("utf-8"))
    done = subprocess.run([BASH, "-n", script.as_posix()], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_the_unit_comparison_prints_the_changed_and_missing_units_and_nothing_else(remote, tmp_path):
    assert CLONE in remote["diff"] and INSTALLED in remote["diff"]
    installed = tmp_path / "installed"
    shutil.copytree(UNIT_DIR, installed)
    clone = tmp_path / "clone"
    shutil.copytree(UNIT_DIR, clone / "deploy" / "spark" / "systemd")

    def compare(clone_dir: Path) -> subprocess.CompletedProcess:
        command = remote["diff"].replace(CLONE, clone_dir.as_posix())
        return run_command_line(tmp_path, command.replace(INSTALLED, installed.as_posix()))

    same = compare(clone)
    assert same.returncode == 0 and same.stdout == "", same.stderr  # identical sets: not a word

    with (installed / "ibkr-health.timer").open("ab") as handle:
        handle.write(b"# edited on the Spark\n")
    (installed / "ibkr-recorder.service").unlink()
    differs = compare(clone)
    assert differs.returncode == 0, differs.stderr
    assert sorted(differs.stdout.split()) == ["ibkr-health.timer", "ibkr-recorder.service"]

    bare = tmp_path / "bare"
    (bare / "deploy" / "spark" / "systemd").mkdir(parents=True)
    nothing = compare(bare)
    assert nothing.returncode == 0 and nothing.stdout == "", nothing.stderr


@pytest.mark.parametrize("age", [None, 125], ids=["no-heartbeat-file", "heartbeat-125s-old"])
def test_the_wait_gives_up_after_132_sleeps_of_10_seconds(remote, tmp_path, age):
    assert remote["wait"].startswith(f"f={HEARTBEAT}; ")  # the file the recorder writes
    sleeps = tmp_path / "sleeps.log"
    now = 1_000_000
    done = run_command_line(tmp_path, remote["wait"], SLEEP_STUB + CLOCK_STUB,
                            SLEEP_LOG=sleeps.as_posix(), FAKE_NOW=str(now),
                            FAKE_MTIME="" if age is None else str(now - age))
    assert done.returncode == 1, (done.returncode, done.stderr)
    assert sleeps.read_text(encoding="utf-8").split() == ["10"] * 132  # 22 minutes in 10 s steps
    assert done.stdout.count("waiting for the sweep to finish") == 22  # one line a minute


def test_the_wait_returns_at_once_for_a_heartbeat_100_seconds_old(remote, tmp_path):
    assert remote["wait"].startswith(f"f={HEARTBEAT}; ")
    heartbeat = tmp_path / "recorder_state.json"
    heartbeat.write_text("{}", encoding="utf-8")
    stamp = time.time() - 100
    os.utime(heartbeat, (stamp, stamp))
    sleeps = tmp_path / "sleeps.log"
    command = remote["wait"].replace(HEARTBEAT, heartbeat.as_posix(), 1)
    done = run_command_line(tmp_path, command, SLEEP_STUB, SLEEP_LOG=sleeps.as_posix())
    assert done.returncode == 0, done.stderr
    age = re.fullmatch(r"heartbeat is (\d+)s old", done.stdout.strip())
    assert age and 100 <= int(age.group(1)) <= 105, done.stdout  # the real stat and date
    assert not sleeps.exists()  # not one sleep


def test_the_status_line_reports_the_commit_the_recorder_and_every_timer(remote, tmp_path):
    prologue = 'git() { echo "abc1234 a commit"; }\nsystemctl() { echo active; }\n'
    done = run_command_line(tmp_path, remote["status"], prologue)
    assert done.returncode == 0, done.stderr
    first, *states = done.stdout.splitlines()
    assert first == "abc1234 a commit"
    assert sorted(states) == sorted(
        f"{unit}: active" for unit in {"ibkr-recorder.service"} | {p.name for p in UNIT_DIR.glob("*.timer")})


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


@pytest.mark.parametrize("value", ["15", "0G", "00G", "15g", "60GB", "G"])
def test_research_run_refuses_a_ceiling_that_is_not_a_positive_number_with_a_unit(value):
    done = run_script("research_run.sh", "--memory", value, "--dry-run", "--", "true")
    assert done.returncode == 2
    assert "15G" in done.stderr and "DRY-RUN" not in done.stdout


@pytest.mark.parametrize("value", ["1K", "512M", "10G", "15G", "2T"])
def test_research_run_takes_any_positive_whole_number_with_a_unit(value):
    done = run_script("research_run.sh", "--memory", value, "--dry-run", "--", "true")
    assert done.returncode == 0, done.stderr
    assert f"-p MemoryMax={value} " in done.stdout
