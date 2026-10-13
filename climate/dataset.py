"""Training frame for the GTM climate lane: one row per (init_date, lead_month)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from climate.bridge import Bridge, to_year_month_index
from climate.paths import data_root
from climate.reduce import ERA5_PARQUET, SEAS5_PARQUET
from climate.storage import latest_values

GTM_SERIES = "gtm"

LEAD1_OFFSET_MONTHS = 0

FRAME_COLUMNS = [
    "init_date",
    "lead_month",
    "target_month",
    "target_year_frac",
    "month_of_year",
    "ens_mean_K",
    "ens_sd_K",
    "n_members",
    "era5_K",
    "y_C",
    "fc_mean_C",
    "fc_sd_C",
]


def target_month(init_date, lead_month) -> pd.Series | str:
    """'YYYY-MM' of the month a (init_date, lead_month) forecast verifies on."""
    scalar = np.ndim(init_date) == 0 and np.ndim(lead_month) == 0
    init = pd.DatetimeIndex(pd.to_datetime(np.atleast_1d(init_date)))
    lead = np.atleast_1d(np.asarray(lead_month, dtype=int))
    if len(lead) == 1 and len(init) > 1:
        lead = np.repeat(lead, len(init))
    offset = lead - 1 + LEAD1_OFFSET_MONTHS
    periods = init.to_period("M") + offset
    out = pd.Series(pd.PeriodIndex(periods).astype(str))
    if scalar:
        return str(out.iloc[0])
    return out


def target_year_frac(target_month_ym) -> pd.Series | float:
    """Mid-month decimal year: year + (month - 0.5) / 12 (Jan 2010 -> 2010.0417)."""
    scalar = np.ndim(target_month_ym) == 0
    ym = to_year_month_index(np.atleast_1d(target_month_ym))
    year = ym.str.slice(0, 4).astype(int).to_numpy()
    month = ym.str.slice(5, 7).astype(int).to_numpy()
    frac = year + (month - 0.5) / 12.0
    if scalar:
        return float(frac[0])
    return pd.Series(frac, dtype=float)


def month_of_year(target_month_ym) -> pd.Series | int:
    """Calendar month (1-12) of a 'YYYY-MM' key."""
    scalar = np.ndim(target_month_ym) == 0
    ym = to_year_month_index(np.atleast_1d(target_month_ym))
    m = ym.str.slice(5, 7).astype(int).to_numpy()
    if scalar:
        return int(m[0])
    return pd.Series(m, dtype=int)


def load_seas5(root: Path) -> pd.DataFrame:
    path = root / SEAS5_PARQUET[0] / SEAS5_PARQUET[1]
    df = pd.read_parquet(path, engine="pyarrow")
    df["init_date"] = pd.to_datetime(df["init_date"]).dt.normalize()
    return df


def load_era5(root: Path) -> pd.Series:
    """ERA5 global-mean K as a Series indexed by 'YYYY-MM'."""
    path = root / ERA5_PARQUET[0] / ERA5_PARQUET[1]
    df = pd.read_parquet(path, engine="pyarrow")
    s = pd.Series(
        df["t2m_global_K"].astype(float).to_numpy(),
        index=to_year_month_index(pd.to_datetime(df["month"])),
        name="era5_K",
    )
    return s[~s.index.duplicated(keep="first")].sort_index()


def load_noaa_gtm(root: Path) -> pd.Series:
    """NOAA gtm first-print anomalies (degC) as a Series indexed by 'YYYY-MM'."""
    values = latest_values(GTM_SERIES, root)
    s = pd.Series(values, dtype=float, name="y_C")
    s.index = to_year_month_index(s.index)
    return s.sort_index()


def aggregate_members(seas5: pd.DataFrame) -> pd.DataFrame:
    """(init_date, lead_month) -> ens_mean_K, ens_sd_K (ddof=1), n_members."""
    g = seas5.groupby(["init_date", "lead_month"], sort=True)["t2m_global_K"]
    agg = pd.DataFrame(
        {
            "ens_mean_K": g.mean(),
            "ens_sd_K": g.std(ddof=1),  # sample sd; NaN when a single member
            "n_members": g.count(),
        }
    ).reset_index()
    agg["lead_month"] = agg["lead_month"].astype(int)
    agg["n_members"] = agg["n_members"].astype(int)
    return agg


def fit_bridge(era5_K: pd.Series, noaa_C: pd.Series) -> Bridge:
    """Fit the ERA5 -> NOAA bridge on the overlap of the two series."""
    return Bridge().fit(era5_K, noaa_C)


def build_frame_from_inputs(
    seas5: pd.DataFrame,
    era5_K: pd.Series,
    noaa_C: pd.Series,
    bridge: Bridge | None = None,
) -> pd.DataFrame:
    """Assemble the training frame from in-memory inputs (no disk access)."""
    era5_K = pd.Series(np.asarray(era5_K, dtype=float), index=to_year_month_index(era5_K.index))
    noaa_C = pd.Series(np.asarray(noaa_C, dtype=float), index=to_year_month_index(noaa_C.index))
    era5_K = era5_K[~era5_K.index.duplicated(keep="first")]
    noaa_C = noaa_C[~noaa_C.index.duplicated(keep="first")]
    if bridge is None:
        bridge = fit_bridge(era5_K, noaa_C)

    frame = aggregate_members(seas5)
    frame["target_month"] = target_month(frame["init_date"], frame["lead_month"]).to_numpy()
    frame["target_year_frac"] = target_year_frac(frame["target_month"]).to_numpy()
    frame["month_of_year"] = month_of_year(frame["target_month"]).to_numpy()
    frame["era5_K"] = era5_K.reindex(frame["target_month"]).to_numpy()
    frame["y_C"] = noaa_C.reindex(frame["target_month"]).to_numpy()
    frame["fc_mean_C"] = bridge.predict_mean(
        frame["ens_mean_K"].to_numpy(), frame["month_of_year"].to_numpy()
    )
    frame["fc_sd_C"] = bridge.predict_sd_scale() * frame["ens_sd_K"].to_numpy()

    frame = frame[FRAME_COLUMNS].sort_values(["init_date", "lead_month"]).reset_index(drop=True)
    frame["init_date"] = pd.to_datetime(frame["init_date"])
    for col in ("target_year_frac", "ens_mean_K", "ens_sd_K", "era5_K", "y_C", "fc_mean_C", "fc_sd_C"):
        frame[col] = frame[col].astype(float)
    frame["lead_month"] = frame["lead_month"].astype(int)
    frame["month_of_year"] = frame["month_of_year"].astype(int)
    frame["n_members"] = frame["n_members"].astype(int)
    frame.attrs["bridge"] = bridge
    frame.attrs["bridge_json"] = bridge.to_json()
    return frame


def build_training_frame(root: str | Path | None = None, bridge: Bridge | None = None) -> pd.DataFrame:
    """Load SEAS5 / ERA5 / NOAA gtm from ``root`` and build the training frame."""
    root = data_root(root)
    seas5 = load_seas5(root)
    era5_K = load_era5(root)
    noaa_C = load_noaa_gtm(root)
    return build_frame_from_inputs(seas5, era5_K, noaa_C, bridge=bridge)


def frame_bridge(frame: pd.DataFrame) -> Bridge:
    """Return the Bridge attached to a frame by :func:`build_training_frame`."""
    b = frame.attrs.get("bridge")
    if isinstance(b, Bridge):
        return b
    js = frame.attrs.get("bridge_json")
    if js:
        return Bridge.from_json(js)
    raise KeyError("frame carries no bridge (was it built by build_training_frame?)")
