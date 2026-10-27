"""Strict nowcast windows: the bridge, the seasonal input and the frame rebuilt per test year."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from climate.bridge import Bridge
from climate.emos import target_year
from climate.nn.availability import noaa_available_on
from climate.nn.features import seasonal_frame
from climate.nn.nowcast import nowcast_frame

MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


class WindowError(ValueError):
    """A test year whose bridge cannot be fitted."""


@dataclass(eq=False)
class NowcastWindow:
    """One test year: the training rows, the admitted gapless test rows, the bridge, the counts."""

    test_year: int
    train: pd.DataFrame
    test: pd.DataFrame
    bridge: Bridge
    dropped_gap_rows: int
    excluded: int
    cutoff_utc: datetime


def monthly_means(daily_K: pd.Series) -> pd.Series:
    """Calendar-month means of the daily series by YYYY-MM; NaN unless every day has a value."""
    clean = daily_K.where(np.isfinite(daily_K))
    grouped = clean.groupby(clean.index.to_period("M"))
    counts = grouped.count()
    means = grouped.mean().where(counts.to_numpy() == counts.index.days_in_month.to_numpy())
    means.index = means.index.astype(str)
    return means


def _series_months(daily_K: pd.Series) -> list[str]:
    """Every month from the first day of the series to its last."""
    if daily_K.empty:
        return []
    return [str(p) for p in pd.period_range(daily_K.index.min(), daily_K.index.max(), freq="M")]


def nowcast_windows(
    daily_K: pd.Series,
    noaa_C: pd.Series,
    seas5: pd.DataFrame,
    ledger: pd.DataFrame | None = None,
    *,
    first_test_year: int,
    last_test_year: int,
    bridge_through: str | None = None,
) -> list[NowcastWindow]:
    """One NowcastWindow per test year, the frame rebuilt with the window's own bridge."""
    if bridge_through is not None and not MONTH.fullmatch(bridge_through):
        raise ValueError(f"bridge_through must be YYYY-MM, got {bridge_through!r}")
    monthly = monthly_means(daily_K)
    series_months = _series_months(daily_K)
    windows = []
    for year in range(first_test_year, last_test_year + 1):
        end = f"{year - 1}-12" if bridge_through is None else bridge_through
        try:
            bridge = Bridge().fit(monthly, noaa_C, end=end)
        except ValueError as exc:
            raise WindowError(f"test year {year}: {exc}") from exc
        seasonal = seasonal_frame(seas5, monthly, noaa_C, bridge=bridge)
        months = [month for month in series_months if month <= f"{year}-12"]
        frame = nowcast_frame(
            daily_K, noaa_C, seasonal, bridge=bridge, ledger=ledger, months=months
        )
        years = target_year(frame)
        gapless = (frame["gap_days"] == 0).to_numpy()
        labelled = np.isfinite(frame["y_C"].to_numpy(dtype=float))
        cutoff = noaa_available_on(f"{year - 1}-12", ledger)
        decided = (frame["decision_utc"] >= cutoff).to_numpy()
        in_year = years == year
        windows.append(
            NowcastWindow(
                test_year=year,
                train=frame[(years < year) & labelled & gapless].reset_index(drop=True),
                test=frame[in_year & decided & gapless].reset_index(drop=True),
                bridge=bridge,
                dropped_gap_rows=int(np.count_nonzero(in_year & decided & ~gapless)),
                excluded=int(np.count_nonzero(in_year & ~decided)),
                cutoff_utc=cutoff,
            )
        )
    return windows
