"""docs/spark_runbook.md section 7 names files and units; each of them must exist."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "spark_runbook.md"
HEADING = "## 7. The trading runtime on the Spark"
TOP_LEVEL = ("scripts", "deploy", "monitoring", "docs", "tests", "src", "analysis", "data")
# A repo path anywhere in the text: in prose, in a command, in a code block. It starts a token
# (not in the middle of a word or of a file name) and runs to the first character a path does
# not hold; a placeholder (<date>) or a glob (*) right after it means it names no single file.
REPO_PATH = re.compile(r"(?<![\w.~-])((?:" + "|".join(TOP_LEVEL) + r")/[\w./-]+)")
# A name with an extension, wherever it stands: a unit is judged by its extension and by its stem.
NAME_WITH_EXTENSION = re.compile(r"(?<![\w.-])([\w-]+)\.([A-Za-z]+)\b")
UNIT_EXTENSIONS = ("service", "timer")


def section_7() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert HEADING in text, "section 7 is missing"
    return text.split(HEADING, 1)[1]


def named_paths(section: str) -> set[str]:
    paths = set()
    for match in REPO_PATH.finditer(section):
        if section[match.end():match.end() + 1] in ("<", "*", "$", "{"):
            continue
        paths.add(match.group(1).rstrip(".,;:/-"))
    return paths


def missing_paths(section: str) -> list[str]:
    paths = named_paths(section)
    if not any((REPO_ROOT / "docs").glob("spark_runtime_spike_*.md")):
        # the Gateway project is committed together with the spike report (Task 0)
        paths = {p for p in paths if not p.startswith("deploy/spark/ibgw/")}
    return sorted(p for p in paths if not (REPO_ROOT / p).exists())


def unit_files() -> set[str]:
    return {p.name for p in (REPO_ROOT / "deploy" / "spark" / "systemd").iterdir()}


def named_units(section: str) -> set[str]:
    return {f"{stem}.{ext}" for stem, ext in NAME_WITH_EXTENSION.findall(section)
            if ext in UNIT_EXTENSIONS}


def misnamed_units(section: str) -> list[str]:
    """Names meant as a unit that are not one: a unit extension on a name no unit file has, or
    any extension on the stem of a real unit (ibkr-health.timr)."""
    known = unit_files()
    stems = {name.rsplit(".", 1)[0] for name in known}
    return sorted({f"{stem}.{ext}" for stem, ext in NAME_WITH_EXTENSION.findall(section)
                   if (ext in UNIT_EXTENSIONS or stem in stems) and f"{stem}.{ext}" not in known})


def test_every_repo_path_section_7_names_exists():
    section = section_7()
    paths = named_paths(section)
    assert len(paths) >= 15, sorted(paths)  # the section really is about these files
    assert missing_paths(section) == []


def test_every_unit_section_7_names_is_in_the_repo():
    section = section_7()
    assert misnamed_units(section) == []  # first: it names the typo that a missing unit also shows
    required = {"ibkr-recorder.service", "ibkr-health.timer", "ibgw-owner-rule.service"}
    assert required <= named_units(section)


def test_the_scan_sees_a_typo_in_a_command_a_code_block_or_an_extension():
    text = ("run `.venv/bin/python scripts/run_gtm_papr.py --mode approve` or\n\n"
            "    bash scripts/spark/deplyo.sh\n"
            "    source scripts/spark/env.sh\n"
            "and `scripts/spark/deploy.shh`, but not `scripts/spark/<name>` or scripts/spark/*.sh\n")
    assert missing_paths(text) == sorted(["scripts/run_gtm_papr.py", "scripts/spark/deplyo.sh",
                                          "scripts/spark/deploy.shh"])


def test_the_scan_sees_a_misspelled_unit_name_or_unit_extension():
    text = ("`ibkr-health.timr`, ibkr-helth.timer, ibkr-paper-sync.servce, ibkr_health.timer and "
            "paper-book-backup.timers are wrong; `ibkr-recorder.service` and ibkr-health.timer "
            "are right")
    assert misnamed_units(text) == sorted(["ibkr-health.timr", "ibkr-helth.timer",
                                           "ibkr-paper-sync.servce", "ibkr_health.timer",
                                           "paper-book-backup.timers"])
