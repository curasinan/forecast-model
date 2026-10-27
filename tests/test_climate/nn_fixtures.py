"""Synthetic SEAS5, ERA5, NOAA and daily ERA5 inputs and nowcast rows for the GTM research tests."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
import pytest

from climate.dataset import month_of_year, target_month, target_year_frac
from climate.nn import seasonal_model
from climate.nn.windows import strict_windows
from climate.storage import LEDGER_COLUMNS, VINTAGE_BACKFILL, VINTAGE_LIVE

FIRST_INIT = "1996-01-01"
LAST_INIT = "2015-12-01"
# Observations start before the first init so a bridge ending 1996-12 can still be fitted.
FIRST_OBSERVED_MONTH = "1993-01"
LEADS = range(1, 7)
N_MEMBERS = 25
BIAS_K = 0.12
SPREAD_K = 0.2
LABEL_NOISE_C = 0.07

NOWCAST_FIRST_DAY = "2000-01-01"
NOWCAST_LAST_DAY = "2011-12-31"
NOWCAST_LAST_LABEL = "2011-11"
NOWCAST_MEMBERS = 12
DAY_NOISE_K = 0.3
ANOMALY_SD_C = 0.1
ANOMALY_PERSISTENCE = 0.6
BACKFILL_SEEN = "2012-01-01T00:00:00+00:00"
# the live first prints came a day after NOAA's usual date
LIVE_FIRST_PRINTS = {"2011-09": "2011-10-13T00:00:00+00:00", "2011-10": "2011-11-13T00:00:00+00:00"}


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
    """Rows of one checkpoint and calendar month as the nowcast frame has them; NaN if not given."""
    given = {"deltas": deltas, "mtd": mtd, "y": y, "months": months, "prev_label": prev_label}
    given = {name: values for name, values in given.items() if values is not None}
    n = max((len(values) for values in given.values()), default=1)
    for name, values in given.items():
        if len(values) != n:
            raise ValueError(f"{name} has {len(values)} values for {n} rows: give one per row")
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


def synthetic_nowcast(
    seed: int = 0,
) -> tuple[pd.Series, pd.Series, pd.DataFrame, pd.DataFrame]:
    """(daily_K, noaa_C, seas5, ledger) for 2000 to 2011, the last month unlabelled."""
    rng = np.random.default_rng(seed)
    days = pd.date_range(NOWCAST_FIRST_DAY, "2012-06-30", freq="D")  # past the daily series
    periods = days.to_period("M")
    months = pd.period_range(NOWCAST_FIRST_DAY, "2012-06", freq="M")
    anomaly = np.zeros(len(months))
    shocks = rng.normal(0.0, ANOMALY_SD_C, len(months))
    for i in range(1, len(months)):
        anomaly[i] = ANOMALY_PERSISTENCE * anomaly[i - 1] + shocks[i]
    anomaly = pd.Series(anomaly, index=months)
    year = days.year + (days.day_of_year - 0.5) / 365.25
    warming = 0.4 + 0.025 * (year - 2010.0)
    cycle = 2.0 * np.sin(2.0 * np.pi * (days.day_of_year - 80) / 365.25)
    smooth = pd.Series(287.0 + warming + cycle, index=days)
    expected_K = smooth.groupby(periods).mean()
    warming_C = pd.Series(warming, index=days).groupby(periods).mean()

    noise = rng.normal(0.0, DAY_NOISE_K, len(days))
    values = smooth.to_numpy() + anomaly.reindex(periods).to_numpy() + noise
    seen = days <= NOWCAST_LAST_DAY
    daily_K = pd.Series(
        values[seen], index=pd.DatetimeIndex(days[seen], name="date"), name="t2m_global_K"
    )

    labelled = pd.period_range(NOWCAST_FIRST_DAY, NOWCAST_LAST_LABEL, freq="M")
    noise = rng.normal(0.0, LABEL_NOISE_C, len(labelled))
    labels = warming_C[labelled].to_numpy() + anomaly[labelled].to_numpy() + noise
    noaa_C = pd.Series(labels, index=labelled.astype(str), name="y_C")

    inits = pd.date_range(NOWCAST_FIRST_DAY, "2011-12-01", freq="MS")
    cells = pd.MultiIndex.from_product([inits, LEADS], names=["init_date", "lead_month"])
    cells = cells.to_frame(index=False)
    targets = pd.PeriodIndex(target_month(cells["init_date"], cells["lead_month"]), freq="M")
    centre = expected_K.reindex(targets).to_numpy() + BIAS_K
    draws = centre[:, None] + SPREAD_K * rng.standard_normal((len(cells), NOWCAST_MEMBERS))
    seas5 = cells.loc[cells.index.repeat(NOWCAST_MEMBERS)].reset_index(drop=True)
    seas5["member"] = np.tile(np.arange(NOWCAST_MEMBERS), len(cells))
    seas5["t2m_global_K"] = draws.ravel()

    ledger = pd.DataFrame(
        [
            {
                "series": "gtm",
                "period": month,
                "value": float(value),
                "first_seen_utc": LIVE_FIRST_PRINTS.get(month, BACKFILL_SEEN),
                "vintage": VINTAGE_LIVE if month in LIVE_FIRST_PRINTS else VINTAGE_BACKFILL,
                "is_revision": False,
                "source_url": "",
            }
            for month, value in noaa_C.items()
        ],
        columns=LEDGER_COLUMNS,
    )
    return daily_K, noaa_C, seas5, ledger


class _Seen(Exception):
    """Ends a fit once the rows it hands to the torch core are known."""


def rows_a_fit_uses(model, rows: pd.DataFrame) -> int:
    """How many rows the model's fit hands to the torch core, training and validation together."""
    seen = []

    def stop(X_train, y_train, X_val, y_val, cfg):
        seen.append(len(X_train) + len(X_val))
        raise _Seen

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(seasonal_model, "fit_gaussian_net", stop)
        with pytest.raises(_Seen):
            model.fit(rows)
    return seen[0]


def windows_with_a_late_input(*args, **kwargs):
    """strict_windows with the last window's first test row public a day after its decision."""
    windows = strict_windows(*args, **kwargs)
    test = windows[-1].test
    test.loc[0, "avail_seas5_utc"] = test.loc[0, "decision_utc"] + pd.Timedelta(days=1)
    return windows
