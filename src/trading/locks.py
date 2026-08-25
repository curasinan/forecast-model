"""Sidecar lockfile excluding concurrent writers of one SQLite paper book.

`scripts/run_ibkr_paper.py` (placement) and `scripts/sync_ibkr_paper.py`
(reconciliation, Task 1.6) can both, in principle, run against the same
`ibkr_paper.db` from two different OS processes -- an operator's manual run
racing the Task Scheduler job, or two overlapping scheduler runs if one
overruns its interval. SQLite's own locking serializes a single statement,
but this repo's DB writers run multi-statement transactions inside
independent Python processes with no shared state; a plain file lock, held
for the whole run, is what actually keeps two of them from interleaving
writes to the same book.

The lock is a JSON sidecar file `<db_path>.lock` created with `O_EXCL` -- an
atomic "create only if it does not already exist" at the OS level, so there
is no separate existence-check-then-create step that a second process could
land inside of. Contents are `{"pid": <int>, "ts_utc": <iso8601>}`,
informational only: they let an operator, or this module on its next run,
judge whether the holder is realistically still alive.

A lock older than `stale_after_min` is assumed to belong to a process that
crashed or was killed without ever reaching its `finally` -- Task Scheduler
makes this a real scenario, since a job killed at its time limit gets no
chance to clean up. Breaking a stale lock is logged at WARNING so it is
visible in whatever captures this process's logs; it is never silent.

Release compares the sidecar's contents against what THIS call wrote before
unlinking it. If another process broke this lock as stale and took it over
while this run was still working (clock skew, or a run that legitimately
took longer than `stale_after_min`), the file on disk belongs to THEM by the
time this call exits -- deleting it would let a third run start while the
second one is still in progress, exactly the failure mode the lock exists to
prevent. Tolerating a missing file on release covers the neighbouring race:
someone else already cleaned this lock up.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_OPEN_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY
if hasattr(os, "O_BINARY"):  # Windows: raw bytes, no CRLF translation
    _OPEN_FLAGS |= os.O_BINARY


class LockHeldError(RuntimeError):
    """`<db_path>.lock` exists and is not old enough to be considered stale."""


def _lock_path(db_path) -> Path:
    return Path(str(db_path) + ".lock")


def _read_lock(lock_path: Path) -> dict | None:
    """Best-effort read: a missing or unparseable file is `None`, never raised."""
    try:
        raw = lock_path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _age_minutes(payload: dict, now: datetime) -> float | None:
    ts = payload.get("ts_utc")
    if not ts:
        return None
    try:
        stamp = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds() / 60.0


def _describe(payload: dict | None) -> str:
    if not payload:
        return "an unreadable lock file"
    return f"pid={payload.get('pid', 'unknown')} since ts_utc={payload.get('ts_utc', 'unknown')}"


def _create(lock_path: Path, content: bytes) -> None:
    """Atomic create-or-fail; raises `FileExistsError` if the sidecar is there."""
    fd = os.open(str(lock_path), _OPEN_FLAGS)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


@contextmanager
def acquire_db_lock(db_path, stale_after_min: float = 10.0):
    """Hold the sidecar lock on `db_path` for the duration of the `with` block.

    Raises `LockHeldError` if `<db_path>.lock` exists and its recorded
    `ts_utc` is not older than `stale_after_min` minutes (an unreadable or
    un-parseable existing lock is treated the same as a live one -- never
    broken on a guess). A stale lock is broken (deleted and recreated) with
    a logged WARNING.

    Always releases on the way out of the `with` block, including when the
    block raises -- but only deletes the sidecar if its contents still match
    what this call wrote; see the module docstring for why.
    """
    lock_path = _lock_path(db_path)
    now = datetime.now(timezone.utc)
    payload = {"pid": os.getpid(), "ts_utc": now.isoformat()}
    content = json.dumps(payload).encode("utf-8")

    try:
        _create(lock_path, content)
    except FileExistsError:
        existing = _read_lock(lock_path)
        age = _age_minutes(existing, now) if existing is not None else None
        if age is not None and age > stale_after_min:
            logger.warning(
                "breaking stale lock %s held by %s (%.1f min old > stale_after_min=%.1f min)",
                lock_path, _describe(existing), age, stale_after_min,
            )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass  # already gone -- fine, we're about to recreate it
            try:
                _create(lock_path, content)
            except FileExistsError as exc:
                raced = _read_lock(lock_path)
                raise LockHeldError(
                    f"{lock_path} was re-acquired by {_describe(raced)} while this "
                    "process was breaking the stale lock; refusing"
                ) from exc
        else:
            raise LockHeldError(
                f"{lock_path} is held by {_describe(existing)}; refusing concurrent "
                f"access (stale_after_min={stale_after_min})"
            )

    try:
        yield
    finally:
        current = _read_lock(lock_path)
        if current == payload:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass  # someone else already removed it -- tolerated
        elif current is not None:
            logger.warning(
                "not releasing %s: held by %s, not this process (pid=%s) -- it was "
                "likely broken as stale and re-acquired while this run was still "
                "working",
                lock_path, _describe(current), payload["pid"],
            )
