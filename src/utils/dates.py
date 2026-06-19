"""Shared date / time utilities."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso8601(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp into a tz-aware UTC datetime, or None.

    Accepts both ``...Z`` and ``...+00:00`` suffixes. Naive timestamps are
    assumed to be UTC. Returns ``None`` for empty / unparseable input rather
    than raising, so callers can use this in bulk operations on possibly-
    dirty data without per-row try/except.
    """
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    # datetime.fromisoformat() in <3.11 doesn't accept a trailing 'Z'.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
