"""Shared label-finalization guards for weather training data.

Single source of truth for the "finalized + 72h before usable_for_training"
rule previously duplicated in collect_labels_wu / collect_labels_cli.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Minimum age after valid local date before we mark usable_for_training.
MIN_FINALIZATION_HOURS = 72


def usable_for_training(
    valid_local_date: str, finalized: bool, now: datetime | None = None
) -> bool:
    """True once a finalized label is >= 72h old (age from UTC midnight of D)."""
    if not finalized:
        return False
    now = now or datetime.now(timezone.utc)
    d = date.fromisoformat(valid_local_date)
    age = now - datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return age >= timedelta(hours=MIN_FINALIZATION_HOURS)


def is_finalized_conservative_utc(valid_local_date: str, now: datetime | None = None) -> bool:
    """Timezone-agnostic proxy: day D counts as finalized after D+1 06:00 UTC."""
    now = now or datetime.now(timezone.utc)
    d = date.fromisoformat(valid_local_date)
    earliest = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1, hours=6)
    return now >= earliest


def is_local_day_complete(
    valid_local_date: str, timezone_name: str, now: datetime | None = None
) -> bool:
    """True once local calendar day D has fully elapsed (DST-safe)."""
    now = now or datetime.now(timezone.utc)
    d = date.fromisoformat(valid_local_date)
    tz = ZoneInfo(timezone_name)
    next_day_start = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1)
    return now >= next_day_start.astimezone(timezone.utc)
