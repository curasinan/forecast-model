"""Strict expanding windows: a bridge refit per test year and rows admitted by decision time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from climate.bridge import Bridge
from climate.emos import target_year
from climate.nn.availability import noaa_available_on
from climate.nn.features import seasonal_frame

FIRST_TEST_YEAR: int = 2001


@dataclass
class Window:
    """One test year: training rows, admitted test rows, the bridge refit for it, the cutoff."""

    test_year: int
    train: pd.DataFrame
    test: pd.DataFrame
    bridge: Bridge
    excluded: int
    cutoff_utc: datetime


def _is_labelled(frame: pd.DataFrame) -> np.ndarray:
    return np.isfinite(frame["y_C"].to_numpy(dtype=float))


def n_labelled_targets(frame: pd.DataFrame) -> int:
    """Distinct target months with a finite label."""
    return int(frame.loc[_is_labelled(frame), "target_month"].nunique())


def strict_windows(
    seas5: pd.DataFrame,
    era5_K: pd.Series,
    noaa_C: pd.Series,
    *,
    first_test_year: int = FIRST_TEST_YEAR,
    last_test_year: int,
    min_train_targets: int = 24,
    ledger: pd.DataFrame | None = None,
) -> list[Window]:
    """One Window per test year; a year with too few labelled training targets is skipped."""
    windows: list[Window] = []
    for year in range(first_test_year, last_test_year + 1):
        last_label = f"{year - 1}-12"
        bridge = Bridge().fit(era5_K, noaa_C, end=last_label)
        frame = seasonal_frame(seas5, era5_K, noaa_C, bridge=bridge)
        years = target_year(frame)
        train = frame[(years < year) & _is_labelled(frame)]
        if n_labelled_targets(train) < min_train_targets:
            continue
        cutoff = noaa_available_on(last_label, ledger)
        decided = (frame["decision_utc"] >= cutoff).to_numpy()
        in_year = years == year
        windows.append(
            Window(
                test_year=year,
                train=train.reset_index(drop=True),
                test=frame[in_year & decided].reset_index(drop=True),
                bridge=bridge,
                excluded=int(np.count_nonzero(in_year & ~decided)),
                cutoff_utc=cutoff,
            )
        )
    return windows
