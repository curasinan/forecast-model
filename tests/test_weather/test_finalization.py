"""72h rule boundaries + back-compat re-exports. Synthetic only."""

from __future__ import annotations

from datetime import datetime, timezone

from weather import finalization
from weather.finalization import (
    MIN_FINALIZATION_HOURS,
    is_local_day_complete,
    usable_for_training,
)


def test_72h_boundary_exact():
    # Age measured from UTC midnight of the valid date; >= semantics.
    at_72h = datetime(2024, 6, 18, 0, 0, tzinfo=timezone.utc)
    assert usable_for_training("2024-06-15", True, now=at_72h) is True


def test_just_under_72h():
    just_under = datetime(2024, 6, 17, 23, 59, tzinfo=timezone.utc)
    assert usable_for_training("2024-06-15", True, now=just_under) is False


def test_not_finalized_never_usable():
    far_later = datetime(2024, 7, 1, tzinfo=timezone.utc)
    assert usable_for_training("2024-06-15", False, now=far_later) is False


def test_min_finalization_hours_value():
    assert MIN_FINALIZATION_HOURS == 72


def test_is_local_day_complete_us():
    # 2024-06-15 America/New_York (EDT) ends 2024-06-16T04:00Z.
    before = datetime(2024, 6, 16, 3, 59, tzinfo=timezone.utc)
    after = datetime(2024, 6, 16, 4, 0, tzinfo=timezone.utc)
    assert is_local_day_complete("2024-06-15", "America/New_York", now=before) is False
    assert is_local_day_complete("2024-06-15", "America/New_York", now=after) is True


def test_is_local_day_complete_tokyo():
    # 2024-06-15 Asia/Tokyo (UTC+9) ends 2024-06-15T15:00Z.
    assert (
        is_local_day_complete(
            "2024-06-15", "Asia/Tokyo", now=datetime(2024, 6, 15, 14, 59, tzinfo=timezone.utc)
        )
        is False
    )
    assert (
        is_local_day_complete(
            "2024-06-15", "Asia/Tokyo", now=datetime(2024, 6, 15, 15, 0, tzinfo=timezone.utc)
        )
        is True
    )


# ---- back-compat: collectors must keep re-exporting the old names ---- #


def test_collect_labels_wu_backcompat():
    from collectors.weather import collect_labels_wu as wu

    assert wu.usable_for_training is finalization.usable_for_training
    assert wu.is_finalized is finalization.is_finalized_conservative_utc
    assert wu.MIN_FINALIZATION_HOURS == MIN_FINALIZATION_HOURS
    # Behavior preserved: day D finalized after D+1 06:00 UTC.
    assert wu.is_finalized("2024-06-15", now=datetime(2024, 6, 16, 6, 0, tzinfo=timezone.utc))
    assert not wu.is_finalized(
        "2024-06-15", now=datetime(2024, 6, 16, 5, 59, tzinfo=timezone.utc)
    )


def test_collect_labels_cli_backcompat():
    from collectors.weather import collect_labels_cli as cli

    assert cli.usable_for_training is finalization.usable_for_training
    assert cli.MIN_FINALIZATION_HOURS == MIN_FINALIZATION_HOURS
