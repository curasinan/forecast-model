"""docs/spark_runbook.md section 7 names files and units; each of them must exist."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "spark_runbook.md"
HEADING = "## 7. The trading runtime on the Spark"
TOP_LEVEL = ("scripts/", "deploy/", "monitoring/", "docs/", "tests/", "src/", "analysis/", "data/")


def section_7() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert HEADING in text, "section 7 is missing"
    return text.split(HEADING, 1)[1]


def test_every_repo_path_section_7_names_exists():
    tokens = set(re.findall(r"`([^`\s]+)`", section_7()))
    paths = {t for t in tokens if t.startswith(TOP_LEVEL) and "<" not in t and "*" not in t}
    assert len(paths) >= 15, sorted(paths)  # the section really is about these files
    if not any((REPO_ROOT / "docs").glob("spark_runtime_spike_*.md")):
        # the Gateway project is committed together with the spike report (Task 0)
        paths = {p for p in paths if not p.startswith("deploy/spark/ibgw/")}
    missing = sorted(p for p in paths if not (REPO_ROOT / p).exists())
    assert missing == []


def test_every_unit_section_7_names_is_in_the_repo():
    units = set(re.findall(r"`([a-z-]+\.(?:service|timer))`", section_7()))
    known = {p.name for p in (REPO_ROOT / "deploy" / "spark" / "systemd").iterdir()}
    assert {"ibkr-recorder.service", "ibkr-health.timer", "ibgw-owner-rule.service"} <= units
    assert units <= known, sorted(units - known)
