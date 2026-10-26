"""Availability rules: when ERA5 days, SEAS5 inits and NOAA monthly values become public."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from climate.nn.availability import (
    era5_available_on,
    genuine_first_print_months,
    last_era5_day_for,
    noaa_available_on,
    seas5_available_on,
)
from climate.storage import LEDGER_COLUMNS, VINTAGE_BACKFILL, VINTAGE_LIVE

_BACKFILL_SEEN = "2026-01-01T00:00:00+00:00"


def _ledger(backfill=(), live=None, revised=()) -> pd.DataFrame:
    live = live or {}
    rows = [
        {
            "series": "gtm", "period": period, "value": 1.0,
            "first_seen_utc": _BACKFILL_SEEN, "vintage": VINTAGE_BACKFILL,
            "is_revision": False, "source_url": "",
        }
        for period in backfill
    ]
    rows += [
        {
            "series": "gtm", "period": period, "value": 1.0,
            "first_seen_utc": seen, "vintage": VINTAGE_LIVE,
            "is_revision": period in revised, "source_url": "",
        }
        for period, seen in live.items()
    ]
    return pd.DataFrame(rows, columns=LEDGER_COLUMNS)


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
