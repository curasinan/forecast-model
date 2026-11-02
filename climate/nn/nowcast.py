"""Nowcast frame: one row per target month and checkpoint, every input cut at the ERA5 lag."""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
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
_SEASONAL_COLUMNS = ("target_month", "decision_utc", "fc_mean_C", "fc_sd_C", "init_month")
ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"


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
    if not index.normalize().equals(index):
        raise ValueError("daily_K must be indexed by dates without a time of day")
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


def _days_considered(first: date, last_day: date) -> int:
    """Days from the 1st through last_day; none when last_day is before the 1st."""
    return max(0, (last_day - first).days + 1)


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
    mean, used, gap, _ = _summarise(values[: _days_considered(first, last_day)])
    return mean, used, gap


_Candidate = tuple[pd.Timestamp, float, float, str]


def _by_target(seasonal: pd.DataFrame) -> dict[str, list[_Candidate]]:
    """Seasonal rows as (decision_utc, fc_mean_C, fc_sd_C, init_month) by target month."""
    missing = [column for column in _SEASONAL_COLUMNS if column not in seasonal.columns]
    if missing:
        raise ValueError(
            f"the seasonal frame lacks {', '.join(missing)}: build it with seasonal_frame"
        )
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


def _months_before(month: str) -> list[str]:
    """The months the previous label of a target may come from, the latest first."""
    period = pd.Period(month, freq="M")
    return [str(period - lag) for lag in range(1, _PREV_LABEL_LOOKBACK + 1)]


def _print_times(months: list[str], ledger: pd.DataFrame | None) -> dict[str, datetime]:
    """When the target months and the months before them become public."""
    needed = {period for month in months for period in (month, *_months_before(month))}
    return {period: noaa_available_on(period, ledger) for period in sorted(needed)}


def _month_encodings(months: list[str]) -> tuple[dict[str, int], dict[str, float]]:
    """Month of year and mid-month decimal year of each month, from the dataset helpers."""
    if not months:
        return {}, {}
    return (
        dict(zip(months, month_of_year(months).tolist(), strict=True)),
        dict(zip(months, target_year_frac(months).tolist(), strict=True)),
    )


def _printed_before(
    before: list[str], decision: datetime, printed: dict[str, datetime]
) -> str | None:
    """The latest month in before that is public at decision, if any."""
    return next((period for period in before if printed[period] <= decision), None)


def _previous_label(
    before: list[str], decision: datetime, labels: dict[str, float], printed: dict[str, datetime]
) -> tuple[float, str, datetime]:
    """Label, month and print time of the latest month in before public at decision."""
    period = _printed_before(before, decision, printed)
    value = float("nan") if period is None else labels.get(period, float("nan"))
    if not np.isfinite(value):
        return float("nan"), "", decision
    return value, period, printed[period]


@dataclass(frozen=True)
class _Lookups:
    """What every row reads besides the daily series: labels, print times, seasonal rows, codes."""

    labels: dict[str, float]
    printed: dict[str, datetime]
    candidates: dict[str, list[_Candidate]]
    month_of_year: dict[str, int]
    year_frac: dict[str, float]


def _lookups(
    labels: dict[str, float],
    months: list[str],
    seasonal: pd.DataFrame,
    ledger: pd.DataFrame | None,
) -> _Lookups:
    """The lookups of the given target months."""
    printed = _print_times(months, ledger)
    moy, year_frac = _month_encodings(months)
    return _Lookups(labels, printed, _by_target(seasonal), moy, year_frac)


def _month_rows(
    daily_K: pd.Series, month: str, look: _Lookups, decisions: list[tuple[str, datetime]]
) -> list[dict]:
    """The untyped rows of one target month at the given (checkpoint, decision) pairs."""
    first, end = _month_span(month)
    length = (end - first).days + 1
    values = _month_values(daily_K, first, length)
    full_mean = float(values.mean()) if np.isfinite(values).all() else float("nan")
    before = _months_before(month)
    seasonal_rows = look.candidates.get(month, [])
    rows = []
    for checkpoint, decision in decisions:
        considered = _days_considered(first, last_era5_day_for(decision))
        mean, used, gap, last = _summarise(values[:considered])
        prev, prev_month, prev_printed = _previous_label(
            before, decision, look.labels, look.printed
        )
        mu, sigma, init = _freshest(seasonal_rows, decision)
        rows.append(
            {
                "target_month": month,
                "checkpoint": checkpoint,
                "decision_utc": decision,
                "month_of_year": look.month_of_year[month],
                "target_year_frac": look.year_frac[month],
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
                "y_C": look.labels.get(month, float("nan")),
                "avail_label_utc": look.printed[month],
            }
        )
    return rows


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
    look = _lookups(labels, target_months, seasonal, ledger)
    rows = []
    for month in target_months:
        decisions = [(c, checkpoint_decision(month, c)) for c in CHECKPOINTS]
        rows += _month_rows(daily_K, month, look, decisions)
    return _typed(pd.DataFrame(rows, columns=NOWCAST_COLUMNS), bridge)


def missing_label(
    noaa_C: pd.Series, ledger: pd.DataFrame | None, *, target_month: str, decision: datetime
) -> tuple[str, datetime] | None:
    """(month, print time) of the previous label public at decision that noaa_C lacks, or None."""
    _require_aware(decision)
    _month_span(target_month)
    printed = _print_times([target_month], ledger)
    period = _printed_before(_months_before(target_month), decision, printed)
    if period is None or np.isfinite(_label_lookup(noaa_C).get(period, float("nan"))):
        return None
    return period, printed[period]


def expected_seas_init(target_month: str, decision: datetime) -> str:
    """The SEAS5 init a decision should see for the target: the latest released, at most it."""
    _require_aware(decision)
    month = pd.Period(decision.astimezone(timezone.utc).strftime("%Y-%m"), freq="M")
    released = month if decision >= seas5_available_on(str(month)) else month - 1
    return str(min(released, pd.Period(target_month, freq="M")))


def _checkpoint_at(target_month: str, decision: datetime) -> str:
    """The latest checkpoint decided at or before the decision; the first one when none is."""
    reached = [c for c in CHECKPOINTS if checkpoint_decision(target_month, c) <= decision]
    return reached[-1] if reached else CHECKPOINTS[0]


def nowcast_row(
    daily_K: pd.Series,
    noaa_C: pd.Series,
    seasonal: pd.DataFrame,
    *,
    bridge: Bridge,
    ledger: pd.DataFrame | None = None,
    target_month: str,
    decision: datetime,
) -> pd.Series:
    """The frame's row of one target month at an exact decision time, not at a checkpoint."""
    _require_daily(daily_K)
    _require_aware(decision)
    _month_span(target_month)
    look = _lookups(_label_lookup(noaa_C), [target_month], seasonal, ledger)
    checkpoint = _checkpoint_at(target_month, decision)
    (row,) = _month_rows(daily_K, target_month, look, [(checkpoint, decision)])
    return _typed(pd.DataFrame([row], columns=NOWCAST_COLUMNS), bridge).iloc[0]


def nowcast_payload(row: pd.Series, mu: float, sigma: float, *, model_fingerprint: str) -> dict:
    """A row's JSON-ready nowcast; usable needs every public day, checkpoint 10, the right init."""
    decision = pd.Timestamp(row["decision_utc"])
    if decision.tzinfo is None:
        raise ValueError("decision_utc must be a timezone-aware time")
    decision = decision.tz_convert("UTC")
    target_month, checkpoint = str(row["target_month"]), str(row["checkpoint"])
    first, end = _month_span(target_month)
    visible = last_era5_day_for(decision.to_pydatetime())
    gap_days = int(row["gap_days"])
    current = str(row["seas_init"]) == expected_seas_init(target_month, decision.to_pydatetime())
    return {
        "as_of": decision.strftime(ISO_UTC),
        "target_month": target_month,
        "checkpoint": checkpoint,
        "mu": float(mu) if math.isfinite(mu) else None,
        "sigma": float(sigma) if math.isfinite(sigma) else None,
        "days_used": int(row["days_used"]),
        "gap_days": gap_days,
        "last_era5_day": None if visible < first else min(visible, end).isoformat(),
        "seas_init": str(row["seas_init"]),
        "prev_label_month": str(row["prev_label_month"]),
        "model_fingerprint": str(model_fingerprint),
        "usable": gap_days == 0 and math.isfinite(mu) and checkpoint != CHECKPOINTS[0] and current,
    }
