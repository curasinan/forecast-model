"""deploy/spark/systemd and deploy/spark/sudoers: what the account holder installs with sudo."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_DIR = REPO_ROOT / "deploy" / "spark" / "systemd"
SUDOERS = REPO_ROOT / "deploy" / "spark" / "sudoers" / "forecast-runtime"
CLONE = "/home/trading/forecast-model"

RUNTIME_SERVICES = ["ibkr-recorder.service", "ibkr-health.service", "ibkr-paper-sync.service",
                    "climate-labels.service", "paper-book-backup.service"]
TIMERS = {"ibkr-health.timer": "*-*-* *:00:00 Europe/Istanbul",
          "ibkr-paper-sync.timer": "*-*-* 17:30:00 Europe/Istanbul",
          "climate-labels.timer": "*-*-* 16:30:00 Europe/Istanbul",
          "paper-book-backup.timer": "*-*-* 21:00:00 Europe/Istanbul"}
SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TWS_PASSWORD", "TWS_USERID",
               "VNC_SERVER_PASSWORD", "HEARTBEAT_URL_HEALTH", "HEARTBEAT_URL_LABELS",
               "HEARTBEAT_URL_BACKUP", "HEARTBEAT_URL_OFFBOX")


def parse_unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """A unit file as {section: [(key, value), ...]}; a key may repeat."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = []
            continue
        key, _, value = line.partition("=")
        sections[current].append((key.strip(), value.strip()))
    return sections


def values(unit: dict, section: str, key: str) -> list[str]:
    return [value for name, value in unit.get(section, []) if name == key]


def test_the_expected_units_exist_and_nothing_else():
    names = sorted(p.name for p in UNIT_DIR.iterdir())
    assert names == sorted([*RUNTIME_SERVICES, *TIMERS, "ibgw-owner-rule.service"])


@pytest.mark.parametrize("name", RUNTIME_SERVICES)
def test_a_runtime_service_runs_as_trading_with_the_agreed_protection(name):
    unit = parse_unit(UNIT_DIR / name)
    assert values(unit, "Service", "User") == ["trading"]
    assert values(unit, "Service", "Group") == ["trading"]
    assert values(unit, "Service", "UMask") == ["0027"]
    assert values(unit, "Service", "NoNewPrivileges") == ["yes"]
    assert values(unit, "Service", "OOMScoreAdjust") == ["-900"]
    assert values(unit, "Service", "WorkingDirectory") == [CLONE]


@pytest.mark.parametrize("name", RUNTIME_SERVICES)
def test_a_runtime_service_starts_the_clone_s_python_on_files_that_exist(name):
    (exec_start,) = values(parse_unit(UNIT_DIR / name), "Service", "ExecStart")
    assert exec_start.startswith(f"{CLONE}/.venv/bin/python -u ")
    scripts = re.findall(r"(?<![\w/])(scripts/[\w/]+\.py)", exec_start)
    modules = re.findall(r"-m ([\w.]+)", exec_start)
    assert scripts or modules, exec_start
    for script in scripts:
        assert (REPO_ROOT / script).is_file(), script
    for module in modules:
        assert (REPO_ROOT / (module.replace(".", "/") + ".py")).is_file(), module


@pytest.mark.parametrize(("name", "calendar"), sorted(TIMERS.items()))
def test_a_timer_names_its_zone_catches_up_and_has_its_service(name, calendar):
    unit = parse_unit(UNIT_DIR / name)
    assert values(unit, "Timer", "OnCalendar") == [calendar]
    assert values(unit, "Timer", "Persistent") == ["true"]
    (service,) = values(unit, "Timer", "Unit")
    assert (UNIT_DIR / service).is_file()
    assert values(unit, "Install", "WantedBy") == ["timers.target"]


def test_the_recorder_is_always_on_and_comes_back_by_itself():
    unit = parse_unit(UNIT_DIR / "ibkr-recorder.service")
    assert values(unit, "Service", "Restart") == ["always"]
    assert values(unit, "Service", "RestartSec") == ["30"]
    assert values(unit, "Install", "WantedBy") == ["multi-user.target"]
    (exec_start,) = values(unit, "Service", "ExecStart")
    assert exec_start.endswith("scripts/record_ibkr_ticks.py --interval-minutes 20")


def test_only_the_health_unit_loads_the_telegram_file():
    alerts = "/home/trading/.config/forecast-model/alerts.env"
    loaders = [name for name in RUNTIME_SERVICES
               if alerts in values(parse_unit(UNIT_DIR / name), "Service", "EnvironmentFile")]
    assert loaders == ["ibkr-health.service"]


def test_each_heartbeat_goes_through_the_wrapper_with_its_own_variable():
    expected = {"ibkr-health.service": ("HEARTBEAT_URL_HEALTH", "--ok-exit 0 1 --"),
                "climate-labels.service": ("HEARTBEAT_URL_LABELS", "--"),
                "paper-book-backup.service": ("HEARTBEAT_URL_BACKUP", "--")}
    for name, (variable, marker) in expected.items():
        unit = parse_unit(UNIT_DIR / name)
        (exec_start,) = values(unit, "Service", "ExecStart")
        assert f"scripts/spark/heartbeat_ping.py --url-env {variable} {marker}" in exec_start
        assert "/home/trading/.config/forecast-model/heartbeat.env" in values(
            unit, "Service", "EnvironmentFile")


def test_an_unhealthy_probe_is_not_a_failed_unit():
    unit = parse_unit(UNIT_DIR / "ibkr-health.service")
    assert values(unit, "Service", "SuccessExitStatus") == ["1"]


def test_a_probe_that_crashed_is_neither_pinged_nor_a_successful_unit():
    """CPython exits 1 on a crash, which this unit calls "found a problem and alerted"; the
    probe's own hook exits with another status, and neither the ping nor the unit takes it."""
    probe = (REPO_ROOT / "scripts" / "check_ibkr_recorder.py").read_text(encoding="utf-8")
    hook = re.search(r"def _crash_exit\(.*?os\._exit\((\d+)\)", probe, re.DOTALL)
    assert hook, "scripts/check_ibkr_recorder.py has no crash hook"
    unit = parse_unit(UNIT_DIR / "ibkr-health.service")
    (exec_start,) = values(unit, "Service", "ExecStart")
    ok_exit = re.search(r"--ok-exit((?: \d+)+) --", exec_start)
    assert ok_exit, exec_start
    pinged = set(ok_exit.group(1).split())
    successes = {code for value in values(unit, "Service", "SuccessExitStatus")
                 for code in value.split()}
    assert pinged == {"0", "1"} and successes == {"1"}  # 0 is a success by default
    assert hook.group(1) not in pinged | successes | {"0"}


def test_the_health_unit_fails_while_the_owner_rule_is_not_active():
    """A failed owner rule at boot leaves Docker to start the Gateway unguarded in silence.
    This run fails instead, so nothing is pinged and the external check goes late."""
    unit = parse_unit(UNIT_DIR / "ibkr-health.service")
    # Any failure of the check exits 3. SuccessExitStatus=1 covers this command too, and a bare
    # is-active exits 1 when the system bus cannot be reached, which would let the run go on.
    assert values(unit, "Service", "ExecStartPre") == [
        "/bin/sh -c '/usr/bin/systemctl is-active --quiet ibgw-owner-rule.service || exit 3'"]
    assert (UNIT_DIR / "ibgw-owner-rule.service").is_file()
    # And it waits for the owner rule to finish starting: after a reboot the persistent catch-up
    # run of the timer can start while that oneshot unit is still activating, and is-active
    # answers 3 for "activating" too, so the run would fail and the next ping wait an hour.
    assert values(unit, "Unit", "After") == ["ibgw-owner-rule.service"]


def test_the_owner_rule_admits_trading_on_the_api_and_the_operator_on_the_screen():
    unit = parse_unit(UNIT_DIR / "ibgw-owner-rule.service")
    starts = values(unit, "Service", "ExecStart")
    assert len(starts) == 2
    assert any("--dport 4002 -m owner ! --uid-owner trading -j REJECT" in s for s in starts)
    assert any("--dport 5900 -m owner ! --uid-owner scura -j REJECT" in s for s in starts)
    assert all("-o lo -p tcp" in s and "iptables -C" in s and "iptables -I" in s for s in starts)
    assert values(unit, "Service", "RemainAfterExit") == ["yes"]
    assert "docker.service" in values(unit, "Unit", "Before")
    assert values(unit, "Service", "Type") == ["oneshot"]
    assert values(unit, "Install", "WantedBy") == ["multi-user.target"]
    stop_values = values(unit, "Service", "ExecStop")
    assert stop_values and stop_values[0].startswith("-/bin/sh -c ")


def test_the_sudoers_file_names_only_these_units_and_only_three_verbs():
    """One alias of exactly these systemctl commands, one grant that uses it, nothing else."""
    text = SUDOERS.read_text(encoding="utf-8")

    # 1. Blank and comment lines go. But sudo does not read "#include", "#includedir" or a "#"
    # right before a digit (a user ID, as in "#1000 ALL=(root) ...") as a comment.
    kept = []
    for line in text.split("\n"):
        if line.strip().startswith("#"):
            assert not re.match(r"^\s*#(\s*include|-?\d)", line), f"not a comment to sudo: {line!r}"
        elif line.strip():
            kept.append(line)

    # 2. A backslash right before a newline continues the line; each remaining line is a statement.
    statements = "\n".join(kept).replace("\\\n", " ").split("\n")

    # 3. The alias and its grant, so that no other statement can add a rule of its own.
    assert len(statements) == 2, f"expected two statements, got {[s[:48] for s in statements]!r}"

    # 4. Every alias entry is a systemctl command, and there are exactly 19 of them.
    alias = re.match(r"^Cmnd_Alias FORECAST_UNITS = (.+)$", statements[0])
    assert alias, f"the first statement is not the alias: {statements[0][:60]!r}"
    entries = [entry.strip() for entry in alias.group(1).split(",")]
    stray = [entry for entry in entries if not entry.startswith("/usr/bin/systemctl ")]
    assert not stray, f"not systemctl commands: {stray!r}"
    assert len(entries) == 19, f"expected 19 entries, got {len(entries)}"

    # 5. Everything after the binary, compared as sorted lists, so that a duplicate, a wildcard
    # or a second unit name shows up as a difference.
    units = {*RUNTIME_SERVICES, *TIMERS}
    expected = sorted([(verb, unit) for unit in units for verb in ("start", "stop")]
                      + [("restart", "ibkr-recorder.service")])
    granted = sorted(tuple(entry.split()[1:]) for entry in entries)
    got, want = Counter(granted), Counter(expected)
    assert granted == expected, (
        f"extra: {sorted((got - want).elements())}, missing: {sorted((want - got).elements())}")

    # 6. The one grant: the operator, as root, without a password, for exactly that alias.
    assert statements[1] == "scura ALL=(root) NOPASSWD: FORECAST_UNITS", statements[1]


def test_no_file_under_deploy_gives_a_secret_a_value():
    offenders = []
    for path in (REPO_ROOT / "deploy").rglob("*"):
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for key in SECRET_KEYS:
                match = re.search(rf"(?:^|[\s\-'\"=]){key}\s*[=:]\s*(.*)$", line)
                if match and match.group(1).strip() and not match.group(1).strip().startswith("${"):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert offenders == []


def test_files_under_deploy_are_lf_in_every_checkout():
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "deploy/** text eol=lf" in attributes
    files = [path for path in (REPO_ROOT / "deploy").rglob("*") if path.is_file()]
    assert [path for path in files if b"\r" in path.read_bytes()] == []
