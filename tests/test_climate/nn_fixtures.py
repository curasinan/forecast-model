"""Synthetic SEAS5, ERA5 and NOAA inputs and nowcast rows shared by the GTM research tests."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from climate.dataset import month_of_year, target_month, target_year_frac

FIRST_INIT = "1996-01-01"
LAST_INIT = "2015-12-01"
# Observations start before the first init so a bridge ending 1996-12 can still be fitted.
FIRST_OBSERVED_MONTH = "1993-01"
LEADS = range(1, 7)
N_MEMBERS = 25
BIAS_K = 0.12
SPREAD_K = 0.2
LABEL_NOISE_C = 0.07


def _truth_C(months) -> np.ndarray:
    return 0.4 + 0.025 * (target_year_frac(months).to_numpy() - 2010.0)


def _level_K(months) -> np.ndarray:
    cycle = 2.0 * np.sin(2.0 * np.pi * month_of_year(months).to_numpy() / 12.0)
    return 287.0 + _truth_C(months) + cycle


def synthetic_inputs(seed: int = 0) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """SEAS5 members around a warming truth, with ERA5 and NOAA series for every target month."""
    rng = np.random.default_rng(seed)
    inits = pd.date_range(FIRST_INIT, LAST_INIT, freq="MS")
    index = pd.MultiIndex.from_product([inits, LEADS], names=["init_date", "lead_month"])
    cells = index.to_frame(index=False)
    targets = target_month(cells["init_date"], cells["lead_month"])
    centre = _level_K(targets) + BIAS_K
    draws = centre[:, None] + SPREAD_K * rng.standard_normal((len(cells), N_MEMBERS))
    seas5 = cells.loc[cells.index.repeat(N_MEMBERS)].reset_index(drop=True)
    seas5["member"] = np.tile(np.arange(N_MEMBERS), len(cells))
    seas5["t2m_global_K"] = draws.ravel()

    months = pd.period_range(FIRST_OBSERVED_MONTH, targets.max(), freq="M").astype(str)
    era5_K = pd.Series(_level_K(months), index=months, name="era5_K")
    labels = _truth_C(months) + rng.normal(0.0, LABEL_NOISE_C, len(months))
    return seas5, era5_K, pd.Series(labels, index=months, name="y_C")


def _column(values: Sequence[float] | None, n: int) -> np.ndarray:
    return np.full(n, np.nan) if values is None else np.asarray(values, dtype=float)


def nowcast_rows(
    checkpoint: str,
    month: int,
    *,
    deltas: Sequence[float] | None = None,
    mtd: Sequence[float] | None = None,
    y: Sequence[float] | None = None,
    months: Sequence[str] | None = None,
    prev_label: Sequence[float] | None = None,
) -> pd.DataFrame:
    """Rows of one checkpoint and one calendar month with the nowcast frame's column names.

    One row per entry of the longest list given (one row when none is). Defaults: target months
    run from 2000 in consecutive years, mtd_mean_K is 287.0, full_month_mean_K is mtd + delta
    (NaN without deltas), prev_label_C and y_C are NaN. days_in_month is 30 and days_used is the
    checkpoint's day number without the ERA5 lag, or 30 for a "+n" checkpoint after month end.
    """
    n = max((len(v) for v in (deltas, mtd, y, months, prev_label) if v is not None), default=1)
    labels = list(months) if months is not None else [f"{2000 + i}-{month:02d}" for i in range(n)]
    mtd_K = np.full(n, 287.0) if mtd is None else np.asarray(mtd, dtype=float)
    days_in_month = 30
    days_used = days_in_month if checkpoint.startswith("+") else int(checkpoint)
    return pd.DataFrame(
        {
            "target_month": labels,
            "checkpoint": checkpoint,
            "month_of_year": month,
            "days_in_month": days_in_month,
            "days_used": days_used,
            "mtd_mean_K": mtd_K,
            "full_month_mean_K": mtd_K + _column(deltas, n),
            "prev_label_C": _column(prev_label, n),
            "y_C": _column(y, n),
            "target_year_frac": target_year_frac(labels).to_numpy(),
        }
    )
