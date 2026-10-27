"""Run directories of the GTM research benchmarks: write, read, render and compare."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from climate.nn.benchmark import COVER50_BAND, COVER90_BAND, LEADS, WIN_MIN_LEADS
from climate.nn.scoring import PIT_BINS

KIND_SEASONAL = "seasonal"
COMPARE_MODELS: tuple[str, ...] = ("nn", "emos")
LABEL_NOTE = (
    "Labels are NOAA's current vintage for history (genuine first prints exist only since "
    "2026-08), so a score carries a revision of a few hundredths of a degree."
)
PROTOCOL_NOTES = {
    "strict": (
        "Expanding windows by target year: the bridge is refit on the years each window trains "
        "on, and a test row counts only when it was decided on or after the publication of the "
        "last training label."
    ),
    "frozen": (
        "The bridge is fitted once on every year and rows are assigned by target year, as in "
        "docs/gtm_model_report.md. This run shows that the baselines are the same code and is "
        "never the headline. Coverage is tabulated for emos only, as in that report."
    ),
}


# run directories


def write_run(
    run_dir: Path,
    *,
    config: dict,
    metrics: dict,
    rows: pd.DataFrame,
    report_md: str,
    model_dir: Path | None = None,
) -> None:
    """Write a new run directory in one step; FileExistsError when it is there already."""
    run_dir = Path(run_dir)
    if run_dir.exists():
        raise FileExistsError(f"the run directory exists already: {run_dir}")
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    scratch = run_dir.with_name(f".{run_dir.name}.{uuid.uuid4().hex[:8]}.partial")
    scratch.mkdir()
    try:
        _write_json(scratch / "config.json", config)
        _write_json(scratch / "metrics.json", metrics)
        rows.to_parquet(scratch / "rows.parquet", engine="pyarrow", index=False)
        (scratch / "report.md").write_text(report_md, encoding="utf-8", newline="\n")
        if model_dir is not None:
            shutil.copytree(model_dir, scratch / "weights")
        os.rename(scratch, run_dir)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def read_run(run_dir: Path) -> dict:
    """The run's config.json and metrics.json under the keys 'config' and 'metrics'."""
    run_dir = Path(run_dir)
    return {
        "config": _read_json(run_dir / "config.json"),
        "metrics": _read_json(run_dir / "metrics.json"),
    }


def git_commit() -> str:
    """Short hash of HEAD in the checkout this code runs from; 'unknown' when git cannot say."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() or "unknown"


def _plain(value):
    """A JSON-ready copy: numpy values as Python ones, NaN and infinity as None."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: dict) -> None:
    text = json.dumps(_plain(payload), indent=2, allow_nan=False, default=str)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# markdown


def _fmt(value, digits: int) -> str:
    """The value with that many decimals; '-' when it is missing or not finite."""
    if value is None or not math.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def _table(header: list[str], body: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return lines + ["| " + " | ".join(cells) + " |" for cells in body]


def _rule_line() -> str:
    (low50, high50), (low90, high90) = COVER50_BAND, COVER90_BAND
    return (
        f"Win rule: nn CRPS at or below emos at {WIN_MIN_LEADS} or more of {len(LEADS)} leads, "
        f"with cover50 in [{low50}, {high50}] and cover90 in [{low90}, {high90}] at every lead."
    )


def _verdict_line(win: tuple[bool, str] | None) -> str:
    if win is None:
        return "Verdict: none (the win rule is judged on a strict run that includes the network)"
    ok, why = win
    return "Verdict: win" if ok else f"Verdict: no win ({why})"


def render_seasonal_report(
    summary: pd.DataFrame, extras: dict, win: tuple[bool, str] | None
) -> str:
    """The markdown report of a seasonal run; win None says that no verdict was asked for."""
    protocol = extras["protocol"]
    lines = [f"# GTM seasonal benchmark: {protocol} protocol", ""]
    if protocol in PROTOCOL_NOTES:
        lines += [PROTOCOL_NOTES[protocol], ""]
    lines += [_rule_line(), "", _verdict_line(win), ""]
    lines += ["## CRPS and calibration by model and lead", "", LABEL_NOTE, ""]
    header = ["model", "lead", "n", "CRPS", "skill vs trend", "skill vs emos", "cover50", "cover90"]
    body = [
        [
            str(r["model"]),
            str(int(r["lead_month"])),
            str(int(r["n"])),
            _fmt(r["crps"], 4),
            _fmt(r["skill_vs_trend"], 3),
            _fmt(r["skill_vs_emos"], 3),
            _fmt(r["cover50"], 3),
            _fmt(r["cover90"], 3),
        ]
        for r in summary.to_dict("records")
    ]
    lines += [*_table(header, body), ""]
    if protocol == "strict" and "excluded_rows" in extras:
        lines += [f"Rows excluded by the strict rule: {extras['excluded_rows']}", ""]
    skipped = extras.get("skipped_windows") or []
    lines += ["Test years skipped: " + (", ".join(str(year) for year in skipped) or "none"), ""]
    pit = extras.get("pit_hist") or {}
    if pit:
        edges = [f"{k / PIT_BINS:.1f}-{(k + 1) / PIT_BINS:.1f}" for k in range(PIT_BINS)]
        body = [[name, *(_fmt(freq, 3) for freq in freqs)] for name, freqs in pit.items()]
        lines += [f"## PIT histogram (all leads, {PIT_BINS} bins)", "", LABEL_NOTE, ""]
        lines += [*_table(["model", *edges], body), ""]
    return "\n".join(lines)


# compare


def _win_cell(win) -> str:
    if win is None:
        return "-"
    ok, why = (win, "") if isinstance(win, bool) else win
    if ok:
        return "yes"
    return f"no: {why}" if why else "no"


def _run_name(run_dir: Path) -> str:
    return run_dir.name or run_dir.resolve().name


def _seasonal_cells(run_dir: Path, run: dict) -> list[str]:
    summary = run["metrics"].get("summary", [])
    crps = {(r["model"], r["lead_month"]): r.get("crps") for r in summary}
    cells = [_run_name(run_dir), str(run["config"].get("protocol", "-"))]
    cells += [_fmt(crps.get((model, lead)), 4) for model in COMPARE_MODELS for lead in LEADS]
    return [*cells, _win_cell(run["metrics"].get("win"))]


def _seasonal_header() -> list[str]:
    header = ["run", "protocol"]
    header += [f"lead{lead}_{model}" for model in COMPARE_MODELS for lead in LEADS]
    return [*header, "win"]


# run kind -> (table header, cells of one run)
COMPARE_LAYOUTS = {KIND_SEASONAL: (_seasonal_header, _seasonal_cells)}


def compare_runs(run_dirs: list[Path]) -> str:
    """One markdown table, a row per run, laid out by the runs' kind; ValueError for a mixed set."""
    runs = [(Path(run_dir), read_run(run_dir)) for run_dir in run_dirs]
    kinds = {run["config"].get("kind", KIND_SEASONAL) for _, run in runs}
    if len(kinds) > 1:
        listed = ", ".join(
            f"{_run_name(run_dir)} ({run['config'].get('kind', KIND_SEASONAL)})"
            for run_dir, run in runs
        )
        raise ValueError(f"cannot compare runs of different kinds: {listed}")
    kind = kinds.pop() if kinds else KIND_SEASONAL
    if kind not in COMPARE_LAYOUTS:
        raise ValueError(f"unknown run kind {kind!r}; choose from {', '.join(COMPARE_LAYOUTS)}")
    header, cells = COMPARE_LAYOUTS[kind]
    body = [cells(run_dir, run) for run_dir, run in runs]
    return "\n".join(_table(header(), body)) + "\n"
