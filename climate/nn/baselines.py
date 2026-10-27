"""Numpy baselines the seasonal and nowcast benchmarks compare the networks against."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from climate.bridge import Bridge
from climate.emos import SIGMA_FLOOR, EmosModel, trend_climatology_baseline

SEASONAL_BASELINES: tuple[str, ...] = ("raw", "emos", "trend")
NOWCAST_BASELINES: tuple[str, ...] = ("bridge", "persistence")
MIN_KEY_ROWS: int = 3


@dataclass
class RemainderStats:
    """Mean and sd (K) of the rest of the month, by (month_of_year, checkpoint) and checkpoint."""

    mean: dict[tuple[int, str], float]
    sd: dict[tuple[int, str], float]
    pooled_mean: dict[str, float]
    pooled_sd: dict[str, float]


def _sample_sd(values: np.ndarray) -> float:
    """Sample sd (ddof=1) floored at SIGMA_FLOOR; NaN below two values."""
    if len(values) < 2:
        return float("nan")
    return max(float(np.std(values, ddof=1)), SIGMA_FLOOR)


def raw_ensemble(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """The bridged ensemble mean and its spread, the spread floored at SIGMA_FLOOR."""
    mu = np.array(frame["fc_mean_C"], dtype=float)
    return mu, np.maximum(np.array(frame["fc_sd_C"], dtype=float), SIGMA_FLOOR)


def emos_baseline(
    train: pd.DataFrame, test: pd.DataFrame, *, trend: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """The operational EMOS fit on train, predicted for the rows of test."""
    prediction = EmosModel(trend=trend).fit(train).predict(test)
    return np.array(prediction["mu"], dtype=float), np.array(prediction["sigma"], dtype=float)


def trend_climatology(train: pd.DataFrame, test: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Per-month trend climatology fit on train, predicted for the rows of test."""
    return trend_climatology_baseline(train, test)


def fit_remainder(train_nowcast: pd.DataFrame) -> RemainderStats:
    """Mean and sd of full_month_mean_K - mtd_mean_K by (month, checkpoint); sparse keys pool."""
    remainder = np.array(train_nowcast["full_month_mean_K"], dtype=float) - np.array(
        train_nowcast["mtd_mean_K"], dtype=float
    )
    known = np.isfinite(remainder)
    remainder = remainder[known]
    month = np.array(train_nowcast["month_of_year"], dtype=int)[known]
    checkpoint = np.array(train_nowcast["checkpoint"], dtype=str)[known]

    pooled_mean: dict[str, float] = {}
    pooled_sd: dict[str, float] = {}
    for name in sorted(set(checkpoint.tolist())):
        values = remainder[checkpoint == name]
        pooled_mean[name] = float(values.mean())
        pooled_sd[name] = _sample_sd(values)

    mean: dict[tuple[int, str], float] = {}
    sd: dict[tuple[int, str], float] = {}
    for key in sorted(set(zip(month.tolist(), checkpoint.tolist(), strict=True))):
        values = remainder[(month == key[0]) & (checkpoint == key[1])]
        if len(values) < MIN_KEY_ROWS:
            mean[key], sd[key] = pooled_mean[key[1]], pooled_sd[key[1]]
        else:
            mean[key], sd[key] = float(values.mean()), _sample_sd(values)
    return RemainderStats(mean=mean, sd=sd, pooled_mean=pooled_mean, pooled_sd=pooled_sd)


def _remainder_for(stats: RemainderStats, month: int, checkpoint: str) -> tuple[float, float]:
    """(mean, sd) for a key; the pooled stats of an unseen key; NaN for an unseen checkpoint."""
    key = (month, checkpoint)
    if key in stats.mean:
        return stats.mean[key], stats.sd[key]
    nan = float("nan")
    return stats.pooled_mean.get(checkpoint, nan), stats.pooled_sd.get(checkpoint, nan)


def bridge_nowcast(
    train_nowcast: pd.DataFrame, test_nowcast: pd.DataFrame, *, bridge: Bridge
) -> tuple[np.ndarray, np.ndarray]:
    """The bridge of the month-to-date mean plus the climatological remainder of the month."""
    if len(test_nowcast) == 0:
        return np.empty(0), np.empty(0)
    stats = fit_remainder(train_nowcast)
    month = np.array(test_nowcast["month_of_year"], dtype=int)
    checkpoint = np.array(test_nowcast["checkpoint"], dtype=str)
    pairs = [
        _remainder_for(stats, m, c)
        for m, c in zip(month.tolist(), checkpoint.tolist(), strict=True)
    ]
    mean, sd = np.array(pairs, dtype=float).T
    complete = np.array(test_nowcast["days_used"]) == np.array(test_nowcast["days_in_month"])
    # a complete month has no remainder, whatever the training stats say
    mean = np.where(complete, 0.0, mean)
    sd = np.where(complete, 0.0, sd)
    mu_K = np.array(test_nowcast["mtd_mean_K"], dtype=float) + mean
    mu = np.asarray(bridge.predict_mean(mu_K, month), dtype=float)
    sigma = np.sqrt((bridge.predict_sd_scale() * sd) ** 2 + bridge.resid_sd**2)
    return mu, sigma


def persistence(
    train_nowcast: pd.DataFrame, test_nowcast: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray]:
    """The previous month's label, with the sd of the month-to-month change in training."""
    labelled = train_nowcast[np.isfinite(np.array(train_nowcast["y_C"], dtype=float))]
    months = labelled.drop_duplicates("target_month").sort_values("target_month")
    sigma = _sample_sd(np.diff(np.array(months["y_C"], dtype=float)))
    mu = np.array(test_nowcast["prev_label_C"], dtype=float)
    return mu, np.full(len(mu), sigma)
