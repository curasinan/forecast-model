"""Daily backup of a quant-data root's operational files, with a manifest and rotation.

    python -m monitoring.backup_paper_book --root /srv/quant_data --dest /srv/quant_data_backups

Copies the files analysis.spark_parity lists as MANIFEST_FILES -- the paper
book, the sync and run ledgers, the signals, the operational model, its
diagnostics and the label ledger -- into ``<dest>/YYYYMMDD/`` under the same
relative paths, writes ``manifest.json`` (sha256 and size of every copy,
``null`` for a file the root does not hold) beside them, and keeps the newest
KEEP_DAYS day directories.

The paper book is copied with SQLite's online backup API, not as a file: the
paper sync writes it, and a plain copy taken mid-transaction can be a
database that does not open. Every other file is small and written
atomically or append-only by its owner.

The day directory is built under a ``.partial`` name and renamed when it is
complete, so a reader never finds a half-written day. Exit 0 when the day
directory is in place, 1 on any failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from analysis.spark_parity import MANIFEST_FILES

KEEP_DAYS = 30
MANIFEST_NAME = "manifest.json"
_DAY_DIR = re.compile(r"^\d{8}$")


def file_record(path: Path) -> dict:
    """sha256 and size of one file, as the manifest stores them."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size": path.stat().st_size}


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix == ".db":
        uri = f"file:{src.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as source, \
                closing(sqlite3.connect(dst)) as target:
            source.backup(target)
    else:
        shutil.copy2(src, dst)


def _prune(dest: Path, keep: int) -> list[Path]:
    """Remove all but the ``keep`` newest day directories; return what was removed."""
    days = sorted(p for p in dest.iterdir() if p.is_dir() and _DAY_DIR.match(p.name))
    removed = days[:-keep] if keep > 0 else []
    for old in removed:
        shutil.rmtree(old)
    return removed


def run_backup(root: Path, dest: Path, today: date, keep: int = KEEP_DAYS) -> Path:
    """Copy the operational files of ``root`` into ``dest/YYYYMMDD`` and prune old days."""
    day_dir = dest / today.strftime("%Y%m%d")
    partial = dest / (day_dir.name + ".partial")
    dest.mkdir(parents=True, exist_ok=True)
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    manifest: dict[str, dict | None] = {}
    for parts in MANIFEST_FILES:
        src = Path(root).joinpath(*parts)
        key = "/".join(parts)
        if src.exists():
            copy = partial.joinpath(*parts)
            _copy(src, copy)
            manifest[key] = file_record(copy)
        else:
            manifest[key] = None
    (partial / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True),
                                         encoding="utf-8")
    if day_dir.exists():
        shutil.rmtree(day_dir)
    partial.rename(day_dir)
    _prune(dest, keep)
    return day_dir


def main(argv: list[str] | None = None, today: date | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path.home() / "quant_data"),
                        help="quant-data root to back up (default ~/quant_data)")
    parser.add_argument("--dest", default=str(Path.home() / "quant_data_backups"),
                        help="where the day directories go (default ~/quant_data_backups)")
    parser.add_argument("--keep", type=int, default=KEEP_DAYS,
                        help=f"day directories to keep (default {KEEP_DAYS})")
    args = parser.parse_args(argv)
    # One clock for the directory name and for the rotation, and it can be injected.
    day = today or datetime.now(timezone.utc).date()
    try:
        day_dir = run_backup(Path(args.root).expanduser(), Path(args.dest).expanduser(),
                             day, keep=args.keep)
    except Exception as exc:  # noqa: BLE001 -- any failure is the outcome to report
        print(f"backup failed: {type(exc).__name__}: {exc}")
        return 1
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    present = sum(1 for record in manifest.values() if record is not None)
    print(f"backup written: {day_dir} ({present}/{len(manifest)} files present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
