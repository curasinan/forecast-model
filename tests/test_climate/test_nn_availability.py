"""Availability rules: when ERA5 days, SEAS5 inits and NOAA monthly values become public."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from climate.nn.availability import (
    LATE_PRINTS,
    era5_available_on,
    genuine_first_print_months,
    last_era5_day_for,
    noaa_available_on,
    seas5_available_on,
)
from climate.storage import LEDGER_COLUMNS, VINTAGE_BACKFILL, VINTAGE_LIVE

UTC = timezone.utc
_BACKFILL_SEEN = "2026-01-01T00:00:00+00:00"


def _ledger(backfill=(), live=None, revised=()) -> pd.DataFrame:
    live = live or {}
    rows = [
        {
            "series": "gtm",
            "period": period,
            "value": 1.0,
            "first_seen_utc": _BACKFILL_SEEN,
            "vintage": VINTAGE_BACKFILL,
            "is_revision": False,
            "source_url": "",
        }
        for period in backfill
    ]
    rows += [
        {
            "series": "gtm",
            "period": period,
            "value": 1.0,
            "first_seen_utc": seen,
            "vintage": VINTAGE_LIVE,
            "is_revision": period in revised,
            "source_url": "",
        }
        for period, seen in live.items()
    ]
    return pd.DataFrame(rows, columns=LEDGER_COLUMNS)


def _revision(period: str, seen: str) -> pd.DataFrame:
    """One live revision row of a gtm period."""
    row = {
        "series": "gtm",
        "period": period,
        "value": 2.0,
        "first_seen_utc": seen,
        "vintage": VINTAGE_LIVE,
        "is_revision": True,
        "source_url": "",
    }
    return pd.DataFrame([row], columns=LEDGER_COLUMNS)


def test_era5_day_is_available_five_days_later():
    assert era5_available_on(date(2026, 10, 1)) == datetime(2026, 10, 6, tzinfo=timezone.utc)


def test_seas5_init_is_available_on_the_13th_at_noon_utc():
    assert seas5_available_on("2026-10") == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def test_noaa_value_is_available_on_the_12th_of_the_next_month_without_a_real_first_print():
    assert noaa_available_on("2025-12") == datetime(2026, 1, 12, tzinfo=timezone.utc)
    ledger = _ledger(backfill={"2025-12"})
    assert noaa_available_on("2025-12", ledger=ledger) == datetime(2026, 1, 12, tzinfo=timezone.utc)


def test_a_genuine_first_print_sets_the_availability_to_its_first_seen():
    ledger = _ledger(live={"2026-09": "2026-10-10T13:30:09+00:00"})
    assert noaa_available_on("2026-09", ledger) == datetime(
        2026, 10, 10, 13, 30, 9, tzinfo=timezone.utc
    )
    assert genuine_first_print_months(ledger) == {"2026-09"}


def test_a_revision_row_is_not_a_first_print():
    ledger = _ledger(live={"2026-08": "2026-10-10T13:30:09+00:00"}, revised={"2026-08"})
    assert genuine_first_print_months(ledger) == set()


def test_last_era5_day_for_a_decision_time():
    assert last_era5_day_for(datetime(2026, 10, 15, tzinfo=timezone.utc)) == date(2026, 10, 10)


def test_last_era5_day_uses_the_utc_date_of_an_aware_decision():
    # 2026-10-14 23:00 UTC: the 2026-10-10 day is not public until 2026-10-15 00:00 UTC.
    decision = datetime(2026, 10, 15, 1, tzinfo=timezone(timedelta(hours=2)))
    assert last_era5_day_for(decision) == date(2026, 10, 9)


# the two late prints


def test_the_two_late_prints_are_used_without_a_ledger_and_there_is_no_third():
    assert LATE_PRINTS == {
        "2018-12": datetime(2019, 2, 6, 16, tzinfo=UTC),
        "2024-09": datetime(2024, 10, 24, 16, tzinfo=UTC),
    }
    assert noaa_available_on("2018-12") == datetime(2019, 2, 6, 16, tzinfo=UTC)
    assert noaa_available_on("2024-09") == datetime(2024, 10, 24, 16, tzinfo=UTC)
    backfilled = _ledger(backfill={"2018-12", "2024-09"})
    assert noaa_available_on("2018-12", backfilled) == datetime(2019, 2, 6, 16, tzinfo=UTC)
    assert noaa_available_on("2024-09", backfilled) == datetime(2024, 10, 24, 16, tzinfo=UTC)


def test_the_months_beside_the_late_prints_keep_the_12th_rule():
    assert noaa_available_on("2018-11") == datetime(2018, 12, 12, tzinfo=UTC)
    assert noaa_available_on("2019-01") == datetime(2019, 2, 12, tzinfo=UTC)
    assert noaa_available_on("2024-08") == datetime(2024, 9, 12, tzinfo=UTC)
    assert noaa_available_on("2024-10") == datetime(2024, 11, 12, tzinfo=UTC)


def test_a_genuine_first_print_in_the_ledger_wins_over_a_late_print():
    ledger = _ledger(live={"2018-12": "2019-01-20T10:00:00+00:00"})
    assert noaa_available_on("2018-12", ledger) == datetime(2019, 1, 20, 10, tzinfo=UTC)


def test_the_late_prints_make_no_month_a_genuine_first_print():
    assert genuine_first_print_months(_ledger(backfill={"2018-12", "2024-09"})) == set()
    live = _ledger(live={"2018-12": "2019-02-06T16:00:00+00:00"})
    assert genuine_first_print_months(live) == {"2018-12"}


# the edge rules


@pytest.mark.parametrize("key", ["2026-9", pd.Period("2026-09", freq="M")], ids=["short", "period"])
def test_a_month_written_another_way_finds_the_same_genuine_first_print(key):
    ledger = _ledger(live={"2026-09": "2026-10-10T13:30:09+00:00"})
    assert noaa_available_on(key, ledger) == datetime(2026, 10, 10, 13, 30, 9, tzinfo=UTC)
    assert noaa_available_on(key) == noaa_available_on("2026-09")


def test_a_first_print_that_was_revised_later_keeps_its_first_seen_time():
    first = _ledger(live={"2026-08": "2026-09-10T13:30:00+00:00"})
    revised = _revision("2026-08", "2026-10-10T13:30:00+00:00")
    ledger = pd.concat([first, revised], ignore_index=True)
    assert noaa_available_on("2026-08", ledger) == datetime(2026, 9, 10, 13, 30, tzinfo=UTC)
    assert genuine_first_print_months(ledger) == {"2026-08"}


def test_a_month_seen_only_as_a_revision_falls_back_to_the_rule():
    ledger = _ledger(live={"2026-08": "2026-10-10T13:30:09+00:00"}, revised={"2026-08"})
    assert noaa_available_on("2026-08", ledger) == datetime(2026, 9, 12, tzinfo=UTC)


def test_a_first_print_without_a_utc_offset_is_refused():
    ledger = _ledger(live={"2026-09": "2026-10-10T13:30:09"})
    with pytest.raises(ValueError, match="2026-09.*no UTC offset"):
        noaa_available_on("2026-09", ledger)


def test_a_print_of_another_series_is_not_a_gtm_print():
    gtm = _ledger(live={"2026-09": "2026-10-10T13:30:09+00:00"})
    assert genuine_first_print_months(gtm, series="nh") == set()
    other = gtm.assign(series="nh")
    assert genuine_first_print_months(other) == set()
    assert genuine_first_print_months(other, series="nh") == {"2026-09"}
    assert noaa_available_on("2026-09", other) == datetime(2026, 10, 12, tzinfo=UTC)


def test_a_naive_decision_time_is_refused_not_read_as_utc():
    with pytest.raises(ValueError, match="decision must be timezone-aware"):
        last_era5_day_for(datetime(2026, 10, 15, 12))


def test_a_decision_at_utc_minus_5_gives_the_era5_day_of_its_utc_time():
    # 2026-10-14 20:00 at UTC-5 is 2026-10-15 01:00 UTC; its local date would give the 9th
    local = datetime(2026, 10, 14, 20, tzinfo=timezone(timedelta(hours=-5)))
    assert last_era5_day_for(local) == last_era5_day_for(datetime(2026, 10, 15, 1, tzinfo=UTC))
    assert last_era5_day_for(local) == date(2026, 10, 10)


@pytest.mark.parametrize("hours", [-12, -5, 0, 2, 14])
def test_the_last_era5_day_is_public_at_the_decision_and_the_next_one_is_not(hours):
    start = datetime(2026, 2, 25, tzinfo=timezone(timedelta(hours=hours)))
    for step in range(0, 24 * 7, 5):  # every five hours for a week, across the month end
        decision = start + timedelta(hours=step)
        day = last_era5_day_for(decision)
        assert era5_available_on(day) <= decision < era5_available_on(day + timedelta(days=1))
