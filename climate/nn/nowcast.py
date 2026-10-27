"""Nowcast frame: one row per target month and checkpoint, every input cut at the ERA5 lag."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from climate.bridge import Bridge, to_year_month_index
from climate.dataset import month_of_year, target_year_frac
from climate.nn.availability import (
    era5_available_on,
    last_era5_day_for,
    noaa_available_on,
    seas5_available_on,
)
from climate.nn.daily import DAILY_PARQUET

CHECKPOINTS: tuple[str, ...] = ("5", "10", "15", "20", "25", "+1", "+3", "+6", "+10")
WIN_CHECKPOINTS: tuple[str, ...] = ("10", "15", "20", "25", "+1", "+3")

NOWCAST_FEATURES: list[str] = [
    "mtd_mean_C",
    "frac_days_used",
    "prev_label_C",
    "seas_mu",
    "seas_sigma",
    "moy_sin",
    "moy_cos",
    "target_year_frac",
]

NOWCAST_COLUMNS: list[str] = [
    "target_month",
    "checkpoint",
    "decision_utc",
    "month_of_year",
    "moy_sin",
    "moy_cos",
    "target_year_frac",
    "days_in_month",
    "days_used",
    "gap_days",
    "mtd_mean_K",
    "mtd_mean_C",
    "full_month_mean_K",
    "frac_days_used",
    "prev_label_C",
    "prev_label_month",
    "avail_prev_label_utc",
    "seas_mu",
    "seas_sigma",
    "seas_init",
    "avail_seas_utc",
    "avail_era5_utc",
    "y_C",
    "avail_label_utc",
]

_INT_COLUMNS = ["month_of_year", "days_in_month", "days_used", "gap_days"]
_TEXT_COLUMNS = ["target_month", "checkpoint", "prev_label_month", "seas_init"]
_TIME_COLUMNS = [
    "decision_utc",
    "avail_prev_label_utc",
    "avail_seas_utc",
    "avail_era5_utc",
    "avail_label_utc",
]
_FLOAT_COLUMNS = [
    c for c in NOWCAST_COLUMNS if c not in _INT_COLUMNS + _TEXT_COLUMNS + _TIME_COLUMNS
]

# the previous label is read from the latest of this many months before the target
_PREV_LABEL_LOOKBACK = 2

_CHECKPOINT = re.compile(r"(\+?)([1-9][0-9]*)")
_MONTH = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")


def _month_span(target_month: str) -> tuple[date, date]:
    """First and last day of a YYYY-MM month; anything else raises ValueError."""
    found = _MONTH.fullmatch(target_month)
    if found is None:
        raise ValueError(f"target_month must be YYYY-MM, got {target_month!r}")
    year, number = int(found[1]), int(found[2])
    return date(year, number, 1), date(year, number, calendar.monthrange(year, number)[1])


def _require_aware(moment: datetime) -> None:
    if moment.utcoffset() is None:
        raise ValueError("decision must be a timezone-aware datetime")


def _require_daily(daily_K: pd.Series) -> None:
    index = daily_K.index
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError("daily_K must be indexed by dates")
    if index.tz is not None:
        raise ValueError("daily_K must be indexed by timezone-naive dates")
    if not index.is_unique:
        raise ValueError("daily_K repeats a date")


def checkpoint_decision(target_month: str, checkpoint: str) -> datetime:
    """00:00 UTC of day n of the month, or n days after its last day for a "+n" checkpoint."""
    found = _CHECKPOINT.fullmatch(checkpoint)
    if found is None:
        raise ValueError(f"checkpoint must be 'n' or '+n' with n >= 1, got {checkpoint!r}")
    first, last = _month_span(target_month)
    count = int(found[2])
    day = last + timedelta(days=count) if found[1] else first.replace(day=count)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def load_daily(root: Path) -> pd.Series:
    """Daily ERA5 global means (K) by naive UTC date, sorted, the first value of a repeat kept."""
    stored = pd.read_parquet(Path(root) / DAILY_PARQUET[0] / DAILY_PARQUET[1], engine="pyarrow")
    index = pd.DatetimeIndex(stored["date"], name="date")
    daily = pd.Series(
        stored["t2m_global_K"].to_numpy(dtype=float), index=index, name="t2m_global_K"
    )
    return daily[~daily.index.duplicated(keep="first")].sort_index()


def _month_values(daily_K: pd.Series, first: date, length: int) -> np.ndarray:
    """The month's daily values in day order, NaN where the series has none."""
    days = pd.date_range(first, periods=length, freq="D")
    return daily_K.reindex(days).to_numpy(dtype=float)


def _days_considered(first: date, end: date, last_day: date) -> int:
    """Days from the 1st through min(last_day, month end); none when last_day is before the 1st."""
    return max(0, (min(last_day, end) - first).days + 1)


def _summarise(values: np.ndarray) -> tuple[float, int, int, int]:
    """Mean of the finite values (NaN if none), their count, the rest, the last finite index."""
    present = np.flatnonzero(np.isfinite(values))
    used = int(present.size)
    mean = float(values[present].mean()) if used else float("nan")
    return mean, used, len(values) - used, int(present[-1]) if used else -1


def month_to_date(daily_K: pd.Series, target_month: str, last_day: date) -> tuple[float, int, int]:
    """(mean K, days used, gap days) over the days from the 1st through min(last_day, month end)."""
    _require_daily(daily_K)
    first, end = _month_span(target_month)
    values = _month_values(daily_K, first, (end - first).days + 1)
    mean, used, gap, _ = _summarise(values[: _days_considered(first, end, last_day)])
    return mean, used, gap


_Candidate = tuple[pd.Timestamp, float, float, str]


def _by_target(seasonal: pd.DataFrame) -> dict[str, list[_Candidate]]:
    """Seasonal rows as (decision_utc, fc_mean_C, fc_sd_C, init_month) by target month."""
    columns = (
        seasonal["target_month"],
        seasonal["decision_utc"],
        seasonal["fc_mean_C"],
        seasonal["fc_sd_C"],
        seasonal["init_month"],
    )
    found: dict[str, list[_Candidate]] = {}
    for target, stamp, mu, sd, init in zip(*columns, strict=True):
        found.setdefault(str(target), []).append((stamp, float(mu), float(sd), str(init)))
    return found


def _freshest(candidates: list[_Candidate], decision: datetime) -> tuple[float, float, str]:
    """The candidate with the latest decision_utc <= decision (the first of a tie), else blanks."""
    public = [c for c in candidates if c[0] <= decision]
    if not public:
        return float("nan"), float("nan"), ""
    _, mu, sd, init = max(public, key=lambda c: c[0])
    return mu, sd, init


def freshest_seasonal(
    seasonal: pd.DataFrame, target_month: str, decision: datetime
) -> tuple[float, float, str]:
    """(fc_mean_C, fc_sd_C, init_month) of the target's latest row public by decision, or blank."""
    _require_aware(decision)
    return _freshest(_by_target(seasonal).get(target_month, []), decision)


def _label_lookup(noaa_C: pd.Series) -> dict[str, float]:
    """Labels by YYYY-MM month; the first of a repeated month wins."""
    lookup: dict[str, float] = {}
    months = to_year_month_index(noaa_C.index)
    for month, value in zip(months, np.asarray(noaa_C, dtype=float), strict=True):
        lookup.setdefault(month, float(value))
    return lookup


def _target_months(daily_K: pd.Series, months: list[str] | None) -> list[str]:
    """The given months validated, sorted and unique; else every month from first day to last."""
    if months is not None:
        for month in months:
            _month_span(month)
        return sorted(set(months))
    if daily_K.empty:
        return []
    span = pd.period_range(daily_K.index.min(), daily_K.index.max(), freq="M")
    return [str(period) for period in span]


def _print_times(months: list[str], ledger: pd.DataFrame | None) -> dict[str, datetime]:
    """When the target months and the months before them become public."""
    needed = {
        str(pd.Period(month, freq="M") - lag)
        for month in months
        for lag in range(_PREV_LABEL_LOOKBACK + 1)
    }
    return {period: noaa_available_on(period, ledger) for period in sorted(needed)}


def _month_encodings(months: list[str]) -> tuple[dict[str, int], dict[str, float]]:
    """Month of year and mid-month decimal year of each month, from the dataset helpers."""
    if not months:
        return {}, {}
    return (
        dict(zip(months, month_of_year(months).tolist(), strict=True)),
        dict(zip(months, target_year_frac(months).tolist(), strict=True)),
    )


def _previous_label(
    before: list[str], decision: datetime, labels: dict[str, float], printed: dict[str, datetime]
) -> tuple[float, str, datetime]:
    """Label, month and print time of the latest month in before public at decision."""
    for period in before:
        if printed[period] <= decision:
            value = labels.get(period, float("nan"))
            if np.isfinite(value):
                return value, period, printed[period]
            break
    return float("nan"), "", decision


def _typed(frame: pd.DataFrame, bridge: Bridge) -> pd.DataFrame:
    """Set the column types, then add the columns that follow from the others."""
    for column in _TIME_COLUMNS:
        frame[column] = pd.to_datetime(frame[column], utc=True)
    for column in _TEXT_COLUMNS:
        frame[column] = frame[column].astype(str)
    for column in _INT_COLUMNS:
        frame[column] = frame[column].astype("int64")
    for column in _FLOAT_COLUMNS:
        frame[column] = frame[column].astype(float)
    month = frame["month_of_year"].to_numpy()
    angle = 2.0 * np.pi * month / 12.0
    frame["moy_sin"] = np.sin(angle)
    frame["moy_cos"] = np.cos(angle)
    frame["frac_days_used"] = frame["days_used"] / frame["days_in_month"]
    if len(frame):
        frame["mtd_mean_C"] = bridge.predict_mean(frame["mtd_mean_K"].to_numpy(), month)
    return frame


def nowcast_frame(
    daily_K: pd.Series,
    noaa_C: pd.Series,
    seasonal: pd.DataFrame,
    *,
    bridge: Bridge,
    ledger: pd.DataFrame | None = None,
    months: list[str] | None = None,
) -> pd.DataFrame:
    """One row per (target month, checkpoint) with each input and when it became public."""
    _require_daily(daily_K)
    labels = _label_lookup(noaa_C)
    target_months = _target_months(daily_K, months)
    printed = _print_times(target_months, ledger)
    moy, year_frac = _month_encodings(target_months)
    candidates = _by_target(seasonal)
    rows = []
    for month in target_months:
        first, end = _month_span(month)
        length = (end - first).days + 1
        values = _month_values(daily_K, first, length)
        full_mean = float(values.mean()) if np.isfinite(values).all() else float("nan")
        before = [
            str(pd.Period(month, freq="M") - lag) for lag in range(1, _PREV_LABEL_LOOKBACK + 1)
        ]
        seasonal_rows = candidates.get(month, [])
        for checkpoint in CHECKPOINTS:
            decision = checkpoint_decision(month, checkpoint)
            considered = _days_considered(first, end, last_era5_day_for(decision))
            mean, used, gap, last = _summarise(values[:considered])
            prev, prev_month, prev_printed = _previous_label(before, decision, labels, printed)
            mu, sigma, init = _freshest(seasonal_rows, decision)
            rows.append(
                {
                    "target_month": month,
                    "checkpoint": checkpoint,
                    "decision_utc": decision,
                    "month_of_year": moy[month],
                    "target_year_frac": year_frac[month],
                    "days_in_month": length,
                    "days_used": used,
                    "gap_days": gap,
                    "mtd_mean_K": mean,
                    "full_month_mean_K": full_mean,
                    "prev_label_C": prev,
                    "prev_label_month": prev_month,
                    "avail_prev_label_utc": prev_printed,
                    "seas_mu": mu,
                    "seas_sigma": sigma,
                    "seas_init": init,
                    "avail_seas_utc": seas5_available_on(init) if init else decision,
                    "avail_era5_utc": era5_available_on(first + timedelta(days=last))
                    if used
                    else decision,
                    "y_C": labels.get(month, float("nan")),
                    "avail_label_utc": printed[month],
                }
            )
    return _typed(pd.DataFrame(rows, columns=NOWCAST_COLUMNS), bridge)
