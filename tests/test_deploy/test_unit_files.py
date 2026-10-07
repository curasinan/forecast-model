"""deploy/spark/systemd and deploy/spark/sudoers: what the account holder installs with sudo."""

from __future__ import annotations

import re
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


def test_the_sudoers_file_names_only_these_units_and_only_three_verbs():
    text = SUDOERS.read_text(encoding="utf-8")
    assert "scura ALL=(root) NOPASSWD: FORECAST_UNITS" in text

    # Parse continuation lines and extract commands
    joined = text.replace("\\\n", " ")
    lines = [line.strip() for line in joined.split("\n") if line.strip() and not line.strip().startswith("#")]

    # Extract the exact set of (verb, unit) pairs
    commands = re.findall(r"/usr/bin/systemctl (\S+) (\S+?)(?:,|\s|$)", text)
    assert commands, text

    # Expected: 9 units (all except ibgw-owner-rule) with start/stop + restart for ibkr-recorder
    expected_units = {
        "ibkr-recorder.service", "ibkr-health.service", "ibkr-health.timer",
        "ibkr-paper-sync.service", "ibkr-paper-sync.timer",
        "climate-labels.service", "climate-labels.timer",
        "paper-book-backup.service", "paper-book-backup.timer"
    }

    expected_commands = set()
    for unit in expected_units:
        expected_commands.add(("start", unit))
        expected_commands.add(("stop", unit))
    expected_commands.add(("restart", "ibkr-recorder.service"))

    assert set(commands) == expected_commands, f"Got {set(commands)}, expected {expected_commands}"

    # Verify file structure: should have one Cmnd_Alias definition and one grant line
    alias_count = sum(1 for line in lines if line.startswith("Cmnd_Alias FORECAST_UNITS"))
    grant_count = sum(1 for line in lines if "scura ALL=(root)" in line)
    assert alias_count == 1, f"Expected 1 Cmnd_Alias definition, got {alias_count}"
    assert grant_count == 1, f"Expected 1 grant line, got {grant_count}"

    # Every line in the alias definition must start with /usr/bin/systemctl
    in_alias = False
    for line in lines:
        if line.startswith("Cmnd_Alias FORECAST_UNITS"):
            in_alias = True
        elif "scura ALL=(root)" in line:
            in_alias = False
        elif in_alias and line:
            # Every non-empty line in alias definition must be a systemctl command
            assert line.startswith("/usr/bin/systemctl"), "Non-systemctl entry in alias: " + line

    assert "*" not in text and "ALL=(ALL)" not in text


def test_no_file_under_deploy_gives_a_secret_a_value():
    offenders = []
    for path in (REPO_ROOT / "deploy").rglob("*"):
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for key in SECRET_KEYS:
                match = re.search(rf"(?:^|[\s\-'\"']){key}\s*[=:]\s*(.*)$", line)
                if match and match.group(1).strip() and not match.group(1).strip().startswith("${"):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert offenders == []


def test_files_under_deploy_are_lf_in_every_checkout():
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "deploy/** text eol=lf" in attributes
    assert [p for p in (REPO_ROOT / "deploy").rglob("*") if p.is_file() and b"\r" in p.read_bytes()] == []
