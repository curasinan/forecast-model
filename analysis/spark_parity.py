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
    <root>/climate_data/labels/labels_ledger.parquet
    <root>/climate_data/models/gtm_emos.json
    <root>/climate_data/paper/ibkr_paper_runs.jsonl            (optional)
    <root>/climate_data/reports/gtm_emos_diagnostics.json      (optional)
"""

from __future__ import annotations

import glob
import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from climate.emos import EmosModel
from climate.fit import MODEL_REL, model_fingerprint
from climate.ladder import list_sweep_files
from climate.storage import ledger_path

REPO_ROOT = Path(__file__).resolve().parents[1]

TICKS_DIR = "ibkr_ticks"
CLIMATE_DIR = "climate_data"
CONTRACTS_DB = "contracts_cache.db"
PAPER_DB_REL = ("paper", "ibkr_paper.db")
RUNS_JSONL_REL = ("paper", "ibkr_paper_runs.jsonl")
SEAS5_REL = ("forecasts", "seas5_global_mean.parquet")
ERA5_REL = ("reference", "era5_global_mean.parquet")
DIAG_REL = ("reports", "gtm_emos_diagnostics.json")
HEARTBEAT_REL = (TICKS_DIR, "state", "recorder_state.json")

# Everything the scoring probe reads, hashed into the compared identity block
# (relative to the quant-data root). Absent entries hash to None on both sides.
INPUT_FILES = (
    (CLIMATE_DIR, *SEAS5_REL),
    (CLIMATE_DIR, *ERA5_REL),
    (CLIMATE_DIR, "labels", "labels_ledger.parquet"),
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
    (CLIMATE_DIR, "labels", "labels_ledger.parquet"),
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
    alive until GC, which would block a later rewrite in tests).
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
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

    Never skips silently: a missing REQUIRED file (newest sweep, labels
    ledger, model) is a failure; the optional trees (hourly bars, the two
    SQLite files) are checked only when present. Empty list = readable.
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
        except (OSError, ValueError, KeyError, TypeError) as exc:
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
        "paper_runs_lines": (
            sum(1 for _ in runs.open("rb")) if runs.exists() else None
        ),
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
            done = subprocess.run(
                ["git", "-C", str(repo_root), *args],
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
