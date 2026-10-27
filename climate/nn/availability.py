"""When ERA5 days, SEAS5 inits and NOAA monthly values become public."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from climate.storage import VINTAGE_LIVE

ERA5_LAG_DAYS: int = 5
SEAS5_RELEASE_DAY: int = 13
SEAS5_RELEASE_HOUR_UTC: int = 12
NOAA_PRINT_DAY: int = 12
# printed late: December 2018 after the 2018-19 shutdown, September 2024 after Hurricane Helene
LATE_PRINTS: dict[str, datetime] = {
    "2018-12": datetime(2019, 2, 6, 16, tzinfo=timezone.utc),
    "2024-09": datetime(2024, 10, 24, 16, tzinfo=timezone.utc),
}


def era5_available_on(day: date) -> datetime:
    """An ERA5 day is public at 00:00 UTC, ERA5_LAG_DAYS after the day."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return midnight + timedelta(days=ERA5_LAG_DAYS)


def seas5_available_on(init_month: str) -> datetime:
    """A SEAS5 init month (YYYY-MM) is released on its 13th at 12:00 UTC."""
    month = pd.Period(init_month, freq="M")
    return datetime(
        month.year, month.month, SEAS5_RELEASE_DAY, SEAS5_RELEASE_HOUR_UTC, tzinfo=timezone.utc
    )


def noaa_available_on(period: str | pd.Period, ledger: pd.DataFrame | None = None) -> datetime:
    """The ledger's genuine first print, else LATE_PRINTS, else the 12th of the next month."""
    period = str(pd.Period(period, freq="M"))
    if ledger is not None:
        prints = _genuine_first_prints(ledger, "gtm")
        seen = prints.loc[prints["period"] == period, "first_seen_utc"]
        if not seen.empty:
            return min(_as_utc(value, period) for value in seen).to_pydatetime()
    if period in LATE_PRINTS:
        return LATE_PRINTS[period]
    next_month = pd.Period(period, freq="M") + 1
    return datetime(next_month.year, next_month.month, NOAA_PRINT_DAY, tzinfo=timezone.utc)


def genuine_first_print_months(ledger: pd.DataFrame, series: str = "gtm") -> set[str]:
    """Periods whose first print is a live row that is not a revision, even if revised later."""
    return set(_genuine_first_prints(ledger, series)["period"])


def last_era5_day_for(decision: datetime) -> date:
    """The latest ERA5 day already public at decision (its UTC date minus ERA5_LAG_DAYS)."""
    if decision.utcoffset() is None:
        raise ValueError("decision must be timezone-aware")
    return decision.astimezone(timezone.utc).date() - timedelta(days=ERA5_LAG_DAYS)


def _genuine_first_prints(ledger: pd.DataFrame, series: str) -> pd.DataFrame:
    is_live = (ledger["series"] == series) & (ledger["vintage"] == VINTAGE_LIVE)
    return ledger[is_live & ~ledger["is_revision"]]


def _as_utc(value: object, period: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError(
            f"first_seen_utc {value!r} of {period} has no UTC offset: "
            "the labels ledger must store aware times"
        )
    return stamp.tz_convert("UTC")
