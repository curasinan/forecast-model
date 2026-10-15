"""deploy/spark/ibgw: the Gateway's compose project, pinned and closed."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = REPO_ROOT / "deploy" / "spark" / "ibgw"


def _service() -> dict:
    compose = yaml.safe_load((PROJECT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert list(compose["services"]) == ["ibgw"]
    return compose["services"]["ibgw"]


def test_the_image_is_pinned_by_a_digest_and_never_by_a_tag():
    image = _service()["image"]
    assert re.fullmatch(r"ghcr\.io/gnzsnz/ib-gateway@sha256:[0-9a-f]{64}", image), image


def test_the_pinned_digest_is_the_one_the_spike_report_records():
    digest = _service()["image"].split("@", 1)[1]
    reports = sorted((REPO_ROOT / "docs").glob("spark_runtime_spike_*.md"))
    assert reports, "the spike report is missing"
    assert digest in reports[-1].read_text(encoding="utf-8")


def test_only_the_api_and_the_screen_are_published_and_only_on_loopback():
    assert sorted(_service()["ports"]) == ["127.0.0.1:4002:4004", "127.0.0.1:5900:5900"]


def test_the_container_is_protected_and_bounded():
    service = _service()
    assert service["restart"] == "unless-stopped"  # a deliberate stop survives a reboot
    assert service["oom_score_adj"] == -900
    assert service["mem_limit"] == "4g"
    assert service["container_name"] == "ibgw"
    assert "healthcheck" in service


def test_the_session_is_paper_manual_and_closed_to_other_hosts():
    env = _service()["environment"]
    assert env["TRADING_MODE"] == "paper"
    assert env["TWS_ACCEPT_INCOMING"] == "reject"  # "localhost only"
    assert env["EXISTING_SESSION_DETECTED_ACTION"] == "manual"
    assert env["READ_ONLY_API"] == "no"
    assert env["TIME_ZONE"] == "Europe/Istanbul" and env["TZ"] == "Europe/Istanbul"


def test_no_ibkr_credential_is_written_in_the_project():
    env = _service()["environment"]
    assert env["TWS_USERID"] == "${TWS_USERID:-}"
    assert env["TWS_PASSWORD"] == "${TWS_PASSWORD:-}"
    example = (PROJECT / "env.example").read_text(encoding="utf-8")
    assert re.search(r"^TWS_USERID=$", example, flags=re.MULTILINE)
    assert re.search(r"^TWS_PASSWORD=$", example, flags=re.MULTILINE)
    assert re.search(r"^VNC_SERVER_PASSWORD=$", example, flags=re.MULTILINE)


def test_the_settings_survive_a_restart():
    service = _service()
    assert service["environment"]["TWS_SETTINGS_PATH"] == "/home/ibgateway/tws_settings"
    assert service["volumes"] == ["./jts:/home/ibgateway/tws_settings"]
