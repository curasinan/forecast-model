"""Restore a copy of the operational files into a scratch root and verify it.

    python scripts/spark/restore_check.py --from /srv/quant_data_backups/20261020 --scratch /tmp/restore
    python scripts/spark/restore_check.py --from ~/quant_data --scratch ~/restore_scratch   # a mirror

A copy is a day directory written by monitoring/backup_paper_book.py (it
carries manifest.json) or a whole quant-data root (it carries none; what the
source holds when the check starts is then the reference). The operational
files are copied into the scratch root, and the restored files are verified:

  * every file the reference lists is there, with the listed sha256 and size;
  * the paper book is present and passes SQLite's integrity check and has its tables;
  * every .parquet file reads, every .json file parses, every .jsonl line parses.

The source is only read. The scratch root must not exist or must be empty.
Exit 0 all good, 1 at least one failure (each printed), 2 copy invalid/unreadable
or scratch not empty or not a directory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import (  # noqa: E402
    MANIFEST_FILES,
    PAPER_DB_REL,
    TICKS_DIR,
    check_parquet,
)
from monitoring.backup_paper_book import MANIFEST_NAME, file_record  # noqa: E402

REQUIRED_TABLES = ("positions", "trade_log")
# The paper book by name, not "the first entry of MANIFEST_FILES": the requirement below must
# stay on the book if that tuple is ever reordered.
PAPER_DB_KEY = "/".join((TICKS_DIR, *PAPER_DB_REL))


def _reference(copy: Path) -> dict[str, dict | None]:
    manifest = copy / MANIFEST_NAME
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("the manifest is not a JSON object")
        # Validate each manifest record
        for key, record in data.items():
            if record is not None:
                if not isinstance(record, dict):
                    raise ValueError(f"manifest record for {key} is malformed")
                if "sha256" not in record or not isinstance(record.get("sha256"), str):
                    raise ValueError(f"manifest record for {key} is malformed")
                if "size" not in record or not isinstance(record.get("size"), int):
                    raise ValueError(f"manifest record for {key} is malformed")
        return data
    reference: dict[str, dict | None] = {}
    for parts in MANIFEST_FILES:
        src = copy.joinpath(*parts)
        reference["/".join(parts)] = file_record(src) if src.is_file() else None
    return reference


def _check_book(path: Path) -> str | None:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()
            tables = {row[0] for row in
                      conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error as exc:
        return f"not a readable database ({type(exc).__name__}: {exc})"
    if verdict != ("ok",):
        return f"integrity check says {verdict!r}"
    missing = [table for table in REQUIRED_TABLES if table not in tables]
    if missing:
        return f"tables missing: {', '.join(missing)}"
    return None


def _check_content(key: str, path: Path) -> str | None:
    if path.suffix == ".db":
        return _check_book(path)
    if path.suffix == ".parquet":
        return check_parquet(path)
    if path.suffix == ".json":
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, RecursionError) as exc:
            return f"not JSON ({exc})"
    if path.suffix == ".jsonl":
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            return f"not UTF-8 ({exc})"
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except (ValueError, RecursionError):
                return f"line {number} is not JSON"
    return None


def restore_and_verify(copy: Path, scratch: Path, failures: list[str] | None = None) -> list[str]:
    """Restore ``copy`` into ``scratch``; return the failures (empty when it is good)."""
    if failures is None:
        failures = []
    reference = _reference(copy)

    # Check that the paper book is present in the reference
    if PAPER_DB_KEY not in reference or reference[PAPER_DB_KEY] is None:
        failures.append(f"FAIL {PAPER_DB_KEY}: the copy has no paper book")

    for key, expected in sorted(reference.items()):
        if expected is None:
            continue

        # Check for path escaping BEFORE checking if file exists
        restored = scratch.joinpath(*key.split("/"))
        try:
            resolved_restored = restored.resolve()
            resolved_scratch = scratch.resolve()
            if not resolved_restored.is_relative_to(resolved_scratch):
                failures.append(f"FAIL {key}: path escapes the scratch root")
                continue
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(f"FAIL {key}: cannot resolve paths ({exc})")
            continue

        src = copy.joinpath(*key.split("/"))
        if not src.is_file():
            failures.append(f"FAIL {key}: missing from the copy")
            continue

        restored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, restored)
        actual = file_record(restored)
        if actual != expected:
            failures.append(f"FAIL {key}: sha256 or size differs from the reference "
                            f"({actual['size']} bytes restored, {expected['size']} expected)")
            continue
        problem = _check_content(key, restored)
        if problem:
            failures.append(f"FAIL {key}: {problem}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="copy", required=True,
                        help="a backup day directory, or a quant-data root")
    parser.add_argument("--scratch", required=True,
                        help="where to restore to; must not exist or must be empty")
    args = parser.parse_args(argv)
    copy = Path(args.copy).expanduser()
    scratch = Path(args.scratch).expanduser()

    # Check if copy exists and is a directory
    if not copy.is_dir():
        print(f"cannot read the copy: {copy} is not a directory")
        return 2

    # Check if scratch exists and is not empty, or if it's not a directory
    if scratch.exists():
        if scratch.is_file():
            print(f"the scratch root {scratch} is not a directory")
            return 2
        if not scratch.is_dir():
            print(f"the scratch root {scratch} is not a directory")
            return 2
        if any(scratch.iterdir()):
            print(f"refusing: {scratch} is not empty; name a fresh scratch directory")
            return 2

    try:
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"cannot prepare the scratch root: {type(exc).__name__}: {exc}")
        return 2

    try:
        reference = _reference(copy)
    except (ValueError, OSError, RecursionError) as exc:
        print(f"cannot read the copy: {type(exc).__name__}: {exc}")
        return 2

    failures: list[str] = []
    try:
        failures = restore_and_verify(copy, scratch, failures)
    except OSError as exc:
        for failure in failures:
            print(failure)
        print(f"cannot read the copy: {type(exc).__name__}: {exc}")
        return 2

    for failure in failures:
        print(failure)

    total_files = sum(1 for record in reference.values() if record is not None)
    if failures:
        print(f"RESTORE FAILED: {len(failures)} check(s) failed, {total_files} files listed ({copy} -> {scratch})")
        return 1
    print(f"RESTORE OK: {total_files} files restored and verified ({copy} -> {scratch})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
