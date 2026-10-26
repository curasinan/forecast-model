"""Daily ERA5 global means for the nowcast: the CDS request, the reducer and the per-year task."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from climate.cds import DEFAULT_GRID, _two_digit
from climate.reduce import _ERA5_TIME_CANDIDATES, _pick, append_parquet, global_mean_series

ERA5_HOURLY_DATASET = "reanalysis-era5-single-levels"
DAILY_TIMES = ("00:00", "06:00", "12:00", "18:00")
DAILY_PARQUET = ("reference", "era5_daily_global_mean.parquet")
DAILY_KEYS = ["date"]


def build_era5_daily_request(year: int, grid: str = DEFAULT_GRID) -> dict:
    """Request dict for one year of 6-hourly ERA5 2m temperature."""
    return {
        "product_type": "reanalysis",
        "variable": "2m_temperature",
        "year": str(int(year)),
        "month": _two_digit(range(1, 13)),
        "day": _two_digit(range(1, 32)),
        "time": list(DAILY_TIMES),
        "data_format": "netcdf",
        "grid": grid,
    }


def era5_daily_frame(nc_path: str | Path) -> pd.DataFrame:
    """Tidy rows (date, t2m_global_K): the mean of the four steps of each complete day."""
    nc_path = Path(nc_path)
    reduced = global_mean_series(nc_path)
    names = set(reduced.dims) | set(reduced.coords)
    tname = _pick(names, _ERA5_TIME_CANDIDATES, "time coord", nc_path)

    df = reduced.to_dataframe().reset_index()
    steps = pd.DataFrame(
        {
            "date": pd.to_datetime(df[tname]).dt.normalize(),
            "t2m_global_K": df["t2m_global_K"].astype(float),
        }
    )
    days = steps.groupby("date")["t2m_global_K"].agg(["mean", "count"])
    days = days[days["count"] == len(DAILY_TIMES)]
    return pd.DataFrame(
        {"date": days.index.astype("datetime64[ns]"), "t2m_global_K": days["mean"].to_numpy()}
    )


def reduce_era5_daily_file(nc_path: str | Path, root: Path) -> tuple[int, int]:
    """Reduce one daily-request netCDF into the parquet; a file with no complete day raises."""
    frame = era5_daily_frame(nc_path)
    if frame.empty:
        raise ValueError(f"no complete day in {nc_path}")
    return append_parquet(frame, root / DAILY_PARQUET[0] / DAILY_PARQUET[1], DAILY_KEYS)


def daily_task(root: Path, year: int) -> tuple[str, dict, Path]:
    """The (dataset, request, raw file) task of one year, as run_tasks takes it."""
    out = root / "raw" / f"era5_{int(year)}_t2m_6hourly.nc"
    return ERA5_HOURLY_DATASET, build_era5_daily_request(year), out
