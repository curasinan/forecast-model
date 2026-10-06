"""Restore a copy of the operational files into a scratch root and verify it.

    python scripts/spark/restore_check.py --from /srv/quant_data_backups/20261020 --scratch /tmp/restore
    python scripts/spark/restore_check.py --from ~/quant_data --scratch ~/restore_scratch   # a mirror

A copy is a day directory written by monitoring/backup_paper_book.py (it
carries manifest.json) or a whole quant-data root (it carries none; what the
source holds when the check starts is then the reference). The operational
files are copied into the scratch root, and the restored files are verified:

  * every file the reference lists is there, with the listed sha256 and size;
  * the paper book passes SQLite's integrity check and has its tables;
  * every .parquet file reads, every .json file parses, every .jsonl line parses.

The source is only read. The scratch root must not exist or must be empty.
Exit 0 all good, 1 at least one failure (each printed), 2 scratch not empty.
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

from analysis.spark_parity import MANIFEST_FILES, check_parquet  # noqa: E402
from monitoring.backup_paper_book import MANIFEST_NAME, file_record  # noqa: E402

REQUIRED_TABLES = ("positions", "trade_log")


def _reference(copy: Path) -> dict[str, dict | None]:
    manifest = copy / MANIFEST_NAME
    if manifest.is_file():
        return json.loads(manifest.read_text(encoding="utf-8"))
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
        except ValueError as exc:
            return f"not JSON ({exc})"
    if path.suffix == ".jsonl":
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError:
                return f"line {number} is not JSON"
    return None


def restore_and_verify(copy: Path, scratch: Path) -> list[str]:
    """Restore ``copy`` into ``scratch``; return the failures (empty when it is good)."""
    reference = _reference(copy)
    failures: list[str] = []
    for key, expected in sorted(reference.items()):
        if expected is None:
            continue
        src = copy.joinpath(*key.split("/"))
        if not src.is_file():
            failures.append(f"FAIL {key}: missing from the copy")
            continue
        restored = scratch.joinpath(*key.split("/"))
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
    if scratch.exists() and any(scratch.iterdir()):
        print(f"refusing: {scratch} is not empty; name a fresh scratch directory")
        return 2
    scratch.mkdir(parents=True, exist_ok=True)
    reference = _reference(copy)
    listed = sum(1 for record in reference.values() if record is not None)
    failures = restore_and_verify(copy, scratch)
    for failure in failures:
        print(failure)
    if failures:
        print(f"RESTORE FAILED: {len(failures)} of {listed} files ({copy} -> {scratch})")
        return 1
    print(f"RESTORE OK: {listed} files restored and verified ({copy} -> {scratch})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
