"""Daily backup of ``data/polymarket.db`` with 7-copy rotation.

Copies the live database to ``data/backups/polymarket_YYYYMMDD_HHMMSS.db``
using the **SQLite online backup API** (``sqlite3.Connection.backup``) rather
than a plain file copy. The DB runs in ``journal_mode=delete`` and the
snapshot collector writes every 15 minutes, so a naive ``shutil.copy`` at
3 AM can land mid-transaction and produce a torn ~500 MB file that won't
open. The backup API yields a transactionally-consistent snapshot even
under concurrent writers.

Disk budget: 7 retained copies x ~500 MB ≈ 3.5 GB steady-state in
``data/backups/`` (git-ignored).

Runnable for the scheduler (daily at 3 AM):
    python -m monitoring.backup_db

Exit code 0 on success, 1 on any failure (so the scheduler's LastTaskResult
reflects backup health).
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "polymarket.db"
DEFAULT_BACKUP_DIR = REPO_ROOT / "data" / "backups"
LOG_PATH = REPO_ROOT / "logs" / "backup_db.log"

KEEP_BACKUPS = 7
BACKUP_GLOB = "polymarket_*.db"

log = logging.getLogger("monitoring.backup_db")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    log.addHandler(console)


# --------------------------------------------------------------------------- #
# Backup + rotation
# --------------------------------------------------------------------------- #


def backup_database(
    db_path: Path | str = DEFAULT_DB_PATH,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    keep: int = KEEP_BACKUPS,
    now: datetime | None = None,
) -> Path:
    """Create a consistent backup copy and prune old ones.

    Returns the path of the new backup file. Raises on failure (missing
    source DB, disk errors) — the CLI wrapper converts that to exit 1.
    """
    src_path = Path(db_path)
    if not src_path.exists():
        raise FileNotFoundError(f"source database not found at {src_path}")

    dest_dir = Path(backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    dest_path = dest_dir / f"polymarket_{stamp}.db"

    log.info("Backing up %s -> %s (SQLite online backup API)", src_path, dest_path)
    # Read-only source connection: the backup can never mutate the live DB.
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    except BaseException:
        # Don't leave a partial file behind on failure.
        src.close()
        dest_path.unlink(missing_ok=True)
        raise
    else:
        src.close()

    size_mb = dest_path.stat().st_size / 1e6
    log.info("Backup complete: %s (%.1f MB)", dest_path.name, size_mb)

    _prune_old_backups(dest_dir, keep=keep)
    return dest_path


def _prune_old_backups(
    backup_dir: Path | str,
    keep: int = KEEP_BACKUPS,
) -> list[Path]:
    """Delete all but the newest ``keep`` backups. Returns the deleted paths.

    Sorted by filename — the YYYYMMDD_HHMMSS stamp makes lexicographic order
    equal chronological order, so no reliance on filesystem mtimes.
    """
    dest_dir = Path(backup_dir)
    backups = sorted(dest_dir.glob(BACKUP_GLOB))
    to_delete = backups[:-keep] if keep > 0 else backups
    deleted: list[Path] = []
    for path in to_delete:
        try:
            path.unlink()
            deleted.append(path)
            log.info("Pruned old backup: %s", path.name)
        except OSError as exc:
            log.error("Failed to delete old backup %s: %s", path, exc)
    retained = len(backups) - len(deleted)
    log.info("Rotation: retained %d backup(s), deleted %d", retained, len(deleted))
    return deleted


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    setup_logging()
    log.info("Starting database backup run")
    try:
        dest = backup_database()
    except Exception as exc:  # noqa: BLE001
        log.critical("Backup FAILED: %s", exc, exc_info=True)
        return 1
    log.info("Backup run finished OK: %s", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
