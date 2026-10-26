"""Seasonal feature frame: the GTM frame plus member quantiles, encodings and availability."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import numpy as np
import pandas as pd

from climate.bridge import Bridge
from climate.dataset import build_frame_from_inputs
from climate.nn.availability import noaa_available_on, seas5_available_on

QUANTILES: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)
MIN_MEMBERS: int = 10

SEASONAL_FEATURES: list[str] = [
    "fc_mean_C",
    "fc_sd_C",
    "q10_C",
    "q25_C",
    "q50_C",
    "q75_C",
    "q90_C",
    "target_year_frac",
    "moy_sin",
    "moy_cos",
    "lead_1",
    "lead_2",
    "lead_3",
    "lead_4",
    "lead_5",
    "lead_6",
]

_LEADS = (1, 2, 3, 4, 5, 6)
_NAMES = [f"q{round(p * 100)}" for p in QUANTILES]
_K_COLUMNS = [f"{name}_K" for name in _NAMES]
_C_COLUMNS = [f"{name}_C" for name in _NAMES]


def _summarise_members(values: pd.Series) -> pd.Series:
    finite = values.to_numpy(dtype=float)
    finite = finite[np.isfinite(finite)]
    enough = len(finite) >= MIN_MEMBERS
    quantiles = np.quantile(finite, QUANTILES) if enough else np.full(len(QUANTILES), np.nan)
    return pd.Series([*quantiles, len(finite)], index=[*_K_COLUMNS, "n_finite"])


def member_quantiles(seas5: pd.DataFrame) -> pd.DataFrame:
    """Per (init_date, lead_month) member quantiles in K, NaN below MIN_MEMBERS finite members."""
    grouped = seas5.groupby(["init_date", "lead_month"], sort=True)["t2m_global_K"]
    stats = grouped.apply(_summarise_members).unstack()[[*_K_COLUMNS, "n_finite"]]
    stats["n_finite"] = stats["n_finite"].astype(int)
    return stats.rename_axis(columns=None).reset_index()


def _utc_times(months: pd.Series, rule: Callable[[str], datetime]) -> pd.Series:
    by_month = {month: rule(month) for month in months.unique()}
    return pd.to_datetime(months.map(by_month), utc=True)


def seasonal_frame(
    seas5: pd.DataFrame, era5_K: pd.Series, noaa_C: pd.Series, *, bridge: Bridge
) -> pd.DataFrame:
    """The GTM frame plus bridged member quantiles, month and lead encodings, availability times."""
    frame = build_frame_from_inputs(seas5, era5_K, noaa_C, bridge=bridge)
    keys = ["init_date", "lead_month"]
    quantiles = frame[keys].merge(member_quantiles(seas5), on=keys, how="left")
    month = frame["month_of_year"].to_numpy()
    for k_col, c_col in zip(_K_COLUMNS, _C_COLUMNS, strict=True):
        frame[c_col] = bridge.predict_mean(quantiles[k_col].to_numpy(), month)
    angle = 2.0 * np.pi * month / 12.0
    frame["moy_sin"] = np.sin(angle)
    frame["moy_cos"] = np.cos(angle)
    for lead in _LEADS:
        frame[f"lead_{lead}"] = (frame["lead_month"].to_numpy() == lead).astype(float)
    frame["init_month"] = frame["init_date"].dt.strftime("%Y-%m")
    frame["decision_utc"] = _utc_times(frame["init_month"], seas5_available_on)
    frame["avail_seas5_utc"] = frame["decision_utc"]
    frame["avail_label_utc"] = _utc_times(frame["target_month"], noaa_available_on)
    return frame
