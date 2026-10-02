"""Spark research-mirror parity tooling.

Design: docs/plans/2026-09-30-spark-research-split-design.md, section 5.

Research-only. Nothing here writes an operational file: no
``paper/gtm_signals.json``, no history line, no ledger. The CLIs under
``scripts/spark/`` are thin wrappers over these functions.

Layout assumed under a quant-data root (``~/quant_data`` on both machines,
``~/quant_data_snapshot`` for the Zenbook-side frozen copy)::

    <root>/ibkr_ticks/quotes/date=YYYY-MM-DD/quotes-HHMMSSZ.parquet
    <root>/ibkr_ticks/quotes_hourly/date=*/*.parquet          (optional)
    <root>/ibkr_ticks/contracts_cache.db                       (optional)
    <root>/ibkr_ticks/paper/ibkr_paper.db                      (optional)
    <root>/climate_data/forecasts/seas5_global_mean.parquet
    <root>/climate_data/reference/era5_global_mean.parquet
    <root>/climate_data/labels/labels_ledger.parquet
    <root>/climate_data/models/gtm_emos.json
    <root>/climate_data/paper/ibkr_paper_runs.jsonl            (optional)
    <root>/climate_data/reports/gtm_emos_diagnostics.json      (optional)

Entries without a marker are required: a missing one is a verify_mirror
failure, not a skip. That includes the two CDS products (SEAS5 and ERA5),
which build_training_frame reads, so a mirror without them cannot be scored.
"""

from __future__ import annotations

import glob
import hashlib
import math
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow.parquet as pq

from climate.emos import EmosModel
from climate.fit import MODEL_REL, REPORT_JSON_REL, model_fingerprint
from climate.ladder import list_sweep_files
from climate.reduce import ERA5_PARQUET, SEAS5_PARQUET
from climate.storage import ledger_path

REPO_ROOT = Path(__file__).resolve().parents[1]

TICKS_DIR = "ibkr_ticks"
CLIMATE_DIR = "climate_data"
CONTRACTS_DB = "contracts_cache.db"
PAPER_DB_REL = ("paper", "ibkr_paper.db")
RUNS_JSONL_REL = ("paper", "ibkr_paper_runs.jsonl")
SEAS5_REL = SEAS5_PARQUET
ERA5_REL = ERA5_PARQUET
DIAG_REL = REPORT_JSON_REL
LEDGER_REL = ("labels", "labels_ledger.parquet")
HEARTBEAT_REL = (TICKS_DIR, "state", "recorder_state.json")

# Everything the scoring probe reads, hashed into the compared identity block
# (relative to the quant-data root). Absent entries hash to None on both sides.
INPUT_FILES = (
    (CLIMATE_DIR, *SEAS5_REL),
    (CLIMATE_DIR, *ERA5_REL),
    (CLIMATE_DIR, *LEDGER_REL),
    (CLIMATE_DIR, *MODEL_REL),
    (CLIMATE_DIR, *DIAG_REL),
    (TICKS_DIR, CONTRACTS_DB),
)

# The operational files a push or a refit must never change on the LIVE root:
# manifested (sha256, size, mtime) before and after, and diffed.
MANIFEST_FILES = (
    (TICKS_DIR, *PAPER_DB_REL),
    (TICKS_DIR, "paper", "ibkr_sync_runs.jsonl"),
    (TICKS_DIR, "paper", "permid_map.json"),
    (CLIMATE_DIR, "paper", "gtm_signals.json"),
    (CLIMATE_DIR, "paper", "gtm_signals_history.jsonl"),
    (CLIMATE_DIR, *RUNS_JSONL_REL),
    (CLIMATE_DIR, *MODEL_REL),
    (CLIMATE_DIR, *DIAG_REL),
    (CLIMATE_DIR, *LEDGER_REL),
)

# --------------------------------------------------------------------------- #
# Mirror integrity
# --------------------------------------------------------------------------- #


def check_parquet(path: Path) -> str | None:
    """None when ``path`` is a complete parquet file, else the error text.

    Reads the whole table, not just the footer: a torn copy of a small
    sweep file can still carry a plausible footer offset.
    """
    try:
        pq.read_table(path)
    except Exception as exc:  # noqa: BLE001 -- any reader error IS the finding
        return f"{type(exc).__name__}: {exc}"
    return None


def check_sqlite(path: Path) -> str | None:
    """None when ``PRAGMA quick_check`` answers ``ok``, else the error text.

    Opens read-only and closes explicitly (Windows keeps an open handle
    alive until GC, which would block a later rewrite in tests). The URI is
    built with ``Path.as_uri`` so a ``#`` or ``?`` in the path cannot cut it
    short and drop ``mode=ro`` (SQLite would then create a stray file and
    report a false ok). A 0-byte file is refused up front: SQLite treats it
    as an empty, valid database.
    """
    target = Path(path)
    if target.is_file() and target.stat().st_size == 0:
        return "empty file (0 bytes)"
    conn = None
    try:
        conn = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
        rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
    answers = [str(r[0]) for r in rows]
    if answers != ["ok"]:
        return "quick_check: " + "; ".join(answers)
    return None


def hourly_files(ticks_root: Path) -> list[Path]:
    """Backfilled hourly-bar parquets under quotes_hourly/date=*/, oldest first."""
    pattern = os.path.join(str(ticks_root), "quotes_hourly", "date=*", "*.parquet")
    return [Path(p) for p in sorted(glob.glob(pattern))]


def verify_mirror(root: Path) -> list[str]:
    """Every unreadable or missing canonical file under ``root``, as 'relpath: error'.

    Never skips silently: a missing REQUIRED file (the newest sweep, the
    SEAS5 and ERA5 products, the labels ledger, the model) is a failure; the
    optional trees (hourly bars, the two SQLite files) are checked only when
    present. Empty list = readable.
    """
    root = Path(root)
    ticks = root / TICKS_DIR
    climate = root / CLIMATE_DIR
    failures: list[str] = []

    def _rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    sweeps = list_sweep_files(ticks)
    if not sweeps:
        failures.append(f"{TICKS_DIR}/quotes: no sweep parquet found")
    else:
        err = check_parquet(sweeps[-1])
        if err:
            failures.append(f"{_rel(sweeps[-1])}: {err}")

    hourly = hourly_files(ticks)
    if hourly:
        err = check_parquet(hourly[-1])
        if err:
            failures.append(f"{_rel(hourly[-1])}: {err}")

    # The three inputs build_training_frame reads are REQUIRED: a mirror
    # without them cannot be scored, and a torn copy must be named here.
    for required in (climate.joinpath(*SEAS5_REL), climate.joinpath(*ERA5_REL), ledger_path(climate)):
        if not required.exists():
            failures.append(f"{_rel(required)}: missing")
        else:
            err = check_parquet(required)
            if err:
                failures.append(f"{_rel(required)}: {err}")

    model = climate.joinpath(*MODEL_REL)
    if not model.exists():
        failures.append(f"{_rel(model)}: missing")
    else:
        try:
            EmosModel.from_json(model)
        except Exception as exc:  # noqa: BLE001 -- any loader error IS the finding
            failures.append(f"{_rel(model)}: {type(exc).__name__}: {exc}")

    for db in (ticks / CONTRACTS_DB, ticks.joinpath(*PAPER_DB_REL)):
        if db.exists():
            err = check_sqlite(db)
            if err:
                failures.append(f"{_rel(db)}: {err}")
    return failures


# --------------------------------------------------------------------------- #
# Snapshot identity
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_identity(root: Path) -> dict[str, Any]:
    """What was scored, computed from the files under ``root`` on THIS machine.

    Both parity payloads carry the identity their own machine computed;
    ``compare_json`` then asserts the two blocks are equal, which is the
    independent witness that both runs read the same bytes (a check
    written by the process it certifies proves nothing).
    """
    root = Path(root)
    ticks = root / TICKS_DIR
    climate = root / CLIMATE_DIR
    sweeps = list_sweep_files(ticks)
    hourly = hourly_files(ticks)
    ledger = ledger_path(climate)
    ledger_df = pd.read_parquet(ledger) if ledger.exists() else None
    model_path = climate.joinpath(*MODEL_REL)
    runs = climate.joinpath(*RUNS_JSONL_REL)
    paper_db = ticks.joinpath(*PAPER_DB_REL)

    def _rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    labels_rows = int(len(ledger_df)) if ledger_df is not None else None
    has_labels = bool(labels_rows)
    paper_runs_lines: int | None = None
    if runs.exists():
        with runs.open("rb") as fh:
            paper_runs_lines = sum(1 for _ in fh)
    return {
        "newest_sweep": _rel(sweeps[-1]) if sweeps else None,
        "newest_sweep_sha256": _sha256(sweeps[-1]) if sweeps else None,
        "sweep_count": len(sweeps),
        "newest_hourly": _rel(hourly[-1]) if hourly else None,
        "hourly_count": len(hourly),
        "labels_rows": labels_rows,
        "labels_sha256": _sha256(ledger) if ledger.exists() else None,
        "labels_max_period": (
            str(ledger_df["period"].astype(str).max()) if has_labels else None
        ),
        "labels_max_first_seen_utc": (
            str(ledger_df["first_seen_utc"].astype(str).max()) if has_labels else None
        ),
        "model_fingerprint": (
            model_fingerprint(EmosModel.from_json(model_path)) if model_path.exists() else None
        ),
        "paper_runs_lines": paper_runs_lines,
        "paper_db_quick_check": (
            (check_sqlite(paper_db) or "ok") if paper_db.exists() else None
        ),
        "inputs_sha256": inputs_sha256(root),
        "code_sha": code_identity()["git_sha"],
    }


def inputs_sha256(root: Path) -> dict[str, str | None]:
    """sha256 of every scoring input under ``root`` (None when absent), keyed by relpath."""
    root = Path(root)
    out: dict[str, str | None] = {}
    for parts in INPUT_FILES:
        path = root.joinpath(*parts)
        out["/".join(parts)] = _sha256(path) if path.exists() else None
    return out


def code_identity(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Commit the scoring code ran at, and whether the tree was dirty.

    ``git_sha`` goes into the COMPARED identity block, so two payloads only
    count as comparable when both machines ran the same commit. ``dirty``
    is informational (the report is edited on the Zenbook while a run
    happens) and belongs in the ignored machine block.
    """

    def _git(*args: str) -> str | None:
        try:
            # --no-optional-locks: `git status` would otherwise refresh and
            # rewrite .git/index (taking its lock); this probe stays read-only.
            done = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(repo_root), *args],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    sha = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {"git_sha": sha or "unknown", "dirty": bool(status) if status is not None else None}


# --------------------------------------------------------------------------- #
# Operational manifest (proof the LIVE root was not touched)
# --------------------------------------------------------------------------- #


def operational_manifest(root: Path) -> dict[str, dict[str, Any] | None]:
    """(sha256, size, mtime_ns) of every operational file under ``root``; None when absent.

    Taken before and after anything that reads the LIVE root (a push, a
    refit) and diffed byte-for-byte: an equal manifest is the proof that
    the paper book, the ledgers and the operational model were not touched.
    """
    root = Path(root)
    out: dict[str, dict[str, Any] | None] = {}
    for parts in MANIFEST_FILES:
        path = root.joinpath(*parts)
        if path.exists():
            stat = path.stat()
            out["/".join(parts)] = {
                "sha256": _sha256(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            }
        else:
            out["/".join(parts)] = None
    return out


# --------------------------------------------------------------------------- #
# JSON compare
# --------------------------------------------------------------------------- #


@dataclass
class CompareResult:
    mismatches: list[str] = field(default_factory=list)
    n_numeric: int = 0
    max_abs: float = 0.0
    max_rel: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.mismatches


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare_json(
    left: Any,
    right: Any,
    *,
    rtol: float = 1e-9,
    atol: float = 1e-12,
    ignore: Iterable[str] = (),
    _path: str = "",
    _result: CompareResult | None = None,
) -> CompareResult:
    """Recursive structural compare of two JSON documents.

    Numbers agree when ``|a-b| <= atol + rtol*max(|a|,|b|)``; NaN equals
    NaN; None never equals NaN; bools, strings and None are exact; dict key
    sets and list lengths must match. ``ignore`` holds dotted paths
    (``identity.machine``) whose subtrees are skipped. Every mismatch is
    reported with its path; the result also carries the largest numeric
    differences seen, so a passing run still shows how close it was.
    """
    result = _result if _result is not None else CompareResult()
    ignored = set(ignore)
    if _path in ignored:
        return result
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right), key=str):
            sub = f"{_path}.{key}" if _path else str(key)
            if sub in ignored:
                continue
            if key not in left:
                result.mismatches.append(f"{sub}: missing on left")
            elif key not in right:
                result.mismatches.append(f"{sub}: missing on right")
            else:
                compare_json(left[key], right[key], rtol=rtol, atol=atol,
                             ignore=ignored, _path=sub, _result=result)
        return result
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            result.mismatches.append(f"{_path}: length {len(left)} != {len(right)}")
            return result
        for i, (a, b) in enumerate(zip(left, right, strict=True)):
            compare_json(a, b, rtol=rtol, atol=atol, ignore=ignored,
                         _path=f"{_path}[{i}]", _result=result)
        return result
    if _is_number(left) and _is_number(right):
        result.n_numeric += 1
        fa, fb = float(left), float(right)
        if math.isnan(fa) and math.isnan(fb):
            return result
        if math.isnan(fa) or math.isnan(fb) or math.isinf(fa) or math.isinf(fb):
            if fa != fb:
                result.mismatches.append(f"{_path}: {left!r} != {right!r}")
            return result
        diff = abs(fa - fb)
        scale = max(abs(fa), abs(fb))
        rel = diff / scale if scale > 0 else 0.0
        result.max_abs = max(result.max_abs, diff)
        result.max_rel = max(result.max_rel, rel)
        if diff > atol + rtol * scale:
            result.mismatches.append(
                f"{_path}: {left!r} != {right!r} (abs {diff:.3e}, rel {rel:.3e})"
            )
        return result
    if type(left) is not type(right) or left != right:
        result.mismatches.append(f"{_path}: {left!r} != {right!r}")
    return result
