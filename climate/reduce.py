"""Reduce CDS netCDF files to tidy global-mean parquet series."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

log = logging.getLogger("climate.reduce")

SEAS5_PARQUET = ("forecasts", "seas5_global_mean.parquet")
ERA5_PARQUET = ("reference", "era5_global_mean.parquet")

SEAS5_KEYS = ["init_date", "lead_month", "member"]
ERA5_KEYS = ["month"]

_VAR_CANDIDATES = ("t2m", "2m_temperature", "tas", "2t")
_LAT_CANDIDATES = ("latitude", "lat")
_LON_CANDIDATES = ("longitude", "lon")
_INIT_CANDIDATES = ("forecast_reference_time", "indexing_time", "time")
_LEAD_CANDIDATES = ("forecastMonth", "leadtime_month", "lead_month")
_MEMBER_CANDIDATES = ("number", "member", "realization")
_ERA5_TIME_CANDIDATES = ("valid_time", "time", "date", "month")


def _pick(names, candidates, what: str, path: Path) -> str:
    for cand in candidates:
        if cand in names:
            return cand
    raise KeyError(f"could not find {what} among {sorted(names)} in {path}")


def global_mean_series(nc_path: str | Path) -> xr.DataArray:
    """Cosine-latitude-weighted global mean of 2m temperature."""
    nc_path = Path(nc_path)
    with xr.open_dataset(nc_path) as ds:
        var_name = _pick(ds.data_vars, _VAR_CANDIDATES, "2m temperature variable", nc_path)
        da = ds[var_name]
        lat = _pick(da.dims, _LAT_CANDIDATES, "latitude dim", nc_path)
        lon = _pick(da.dims, _LON_CANDIDATES, "longitude dim", nc_path)
        if "expver" in da.dims:  # ERA5 final + ERA5T split: one is NaN per time
            da = da.mean("expver", skipna=True)
        weights = np.cos(np.deg2rad(ds[lat]))
        reduced = da.weighted(weights).mean(dim=[lat, lon])
        reduced = reduced.rename("t2m_global_K")
        return reduced.load()


def seas5_frame(nc_path: str | Path) -> pd.DataFrame:
    """Tidy rows (init_date, lead_month, member, t2m_global_K) for a SEAS5 file."""
    nc_path = Path(nc_path)
    reduced = global_mean_series(nc_path)
    names = set(reduced.dims) | set(reduced.coords)
    init = _pick(names, _INIT_CANDIDATES, "init time coord", nc_path)
    lead = _pick(names, _LEAD_CANDIDATES, "lead month coord", nc_path)
    member = _pick(names, _MEMBER_CANDIDATES, "ensemble member coord", nc_path)

    df = reduced.to_dataframe().reset_index()
    out = pd.DataFrame(
        {
            "init_date": pd.to_datetime(df[init]).dt.normalize(),
            "lead_month": df[lead].astype(int),
            "member": df[member].astype(int),
            "t2m_global_K": df["t2m_global_K"].astype(float),
        }
    )
    return out.sort_values(SEAS5_KEYS).reset_index(drop=True)


def era5_frame(nc_path: str | Path) -> pd.DataFrame:
    """Tidy rows (month, t2m_global_K) for an ERA5 monthly-means file."""
    nc_path = Path(nc_path)
    reduced = global_mean_series(nc_path)
    names = set(reduced.dims) | set(reduced.coords)
    tname = _pick(names, _ERA5_TIME_CANDIDATES, "time coord", nc_path)

    df = reduced.to_dataframe().reset_index()
    months = pd.to_datetime(df[tname]).dt.to_period("M").dt.to_timestamp()
    out = pd.DataFrame(
        {
            "month": months,
            "t2m_global_K": df["t2m_global_K"].astype(float),
        }
    )
    return out.sort_values(ERA5_KEYS).reset_index(drop=True)


def append_parquet(df: pd.DataFrame, path: str | Path, key_cols: list[str]) -> tuple[int, int]:
    """Append ``df`` to parquet at ``path``, deduping on ``key_cols``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = pd.read_parquet(path, engine="pyarrow")
        before = len(existing)
        merged = pd.concat([existing, df], ignore_index=True)
    else:
        before = 0
        merged = df.copy()
    merged = (
        merged.drop_duplicates(subset=key_cols, keep="first")
        .sort_values(key_cols)
        .reset_index(drop=True)
    )
    merged.to_parquet(path, engine="pyarrow", index=False)
    added = len(merged) - before
    log.info("parquet %s: +%d rows -> %d total", path.name, added, len(merged))
    return added, len(merged)


def reduce_seas5_file(nc_path: str | Path, root: Path) -> tuple[int, int]:
    """Reduce one SEAS5 netCDF and append to the SEAS5 global-mean parquet."""
    df = seas5_frame(nc_path)
    return append_parquet(df, root / SEAS5_PARQUET[0] / SEAS5_PARQUET[1], SEAS5_KEYS)


def reduce_era5_file(nc_path: str | Path, root: Path) -> tuple[int, int]:
    """Reduce one ERA5 netCDF and append to the ERA5 global-mean parquet."""
    df = era5_frame(nc_path)
    return append_parquet(df, root / ERA5_PARQUET[0] / ERA5_PARQUET[1], ERA5_KEYS)
