"""Daily ERA5 global means for the nowcast: the CDS requests, the reducer and the fetch tasks."""

from __future__ import annotations

import calendar
import contextlib
import os
import re
import tempfile
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from climate.cds import DEFAULT_GRID, _two_digit
from climate.nn.availability import ERA5_LAG_DAYS
from climate.reduce import _ERA5_TIME_CANDIDATES, _pick, append_parquet, global_mean_series

ERA5_HOURLY_DATASET = "reanalysis-era5-single-levels"
DAILY_TIMES = ("00:00", "06:00", "12:00", "18:00")
DAILY_PARQUET = ("reference", "era5_daily_global_mean.parquet")
DAILY_KEYS = ["date"]
MONTH_TEXT = re.compile(r"([0-9]{4})-([0-9]{2})")


def _daily_request(year: int, months: Iterable[int], days: Iterable[int], grid: str) -> dict:
    """The request dict that the year and the month requests are both made from."""
    return {
        "product_type": "reanalysis",
        "variable": "2m_temperature",
        "year": str(int(year)),
        "month": _two_digit(months),
        "day": _two_digit(days),
        "time": list(DAILY_TIMES),
        "data_format": "netcdf",
        "grid": grid,
    }


def build_era5_daily_request(year: int, grid: str = DEFAULT_GRID) -> dict:
    """Request dict for one year of 6-hourly ERA5 2m temperature."""
    return _daily_request(year, range(1, 13), range(1, 32), grid)


def _parse_month(text: str) -> tuple[int, int]:
    """The (year, month) of a YYYY-MM text; anything else raises ValueError."""
    found = MONTH_TEXT.fullmatch(text)
    if found is None or not 1 <= int(found[2]) <= 12 or int(found[1]) == 0:
        raise ValueError(f"month must be YYYY-MM, got {text!r}")
    return int(found[1]), int(found[2])


def _cutoff(today: date) -> date:
    """The latest day that ERA5 has published by today."""
    return today - timedelta(days=ERA5_LAG_DAYS)


def has_era5_day(month: str, *, today: date) -> bool:
    """Whether ERA5 has published at least one day of the YYYY-MM month by today."""
    year, number = _parse_month(month)
    return date(year, number, 1) <= _cutoff(today)


def build_era5_daily_month_request(month: str, *, today: date, grid: str = DEFAULT_GRID) -> dict:
    """Request dict for the days of one YYYY-MM month that ERA5 has published by today."""
    year, number = _parse_month(month)
    if not has_era5_day(month, today=today):
        raise ValueError(f"no ERA5 day of {month} is available by {today}")
    cutoff = _cutoff(today)
    length = calendar.monthrange(year, number)[1]
    last = length if date(year, number, length) <= cutoff else cutoff.day
    request = _daily_request(year, [number], range(1, last + 1), grid)
    request["download_format"] = "unarchived"
    return request


def _month_index(text: str) -> int:
    year, number = _parse_month(text)
    return year * 12 + number - 1


def _month_text(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def parse_months(text: str) -> list[str]:
    """Months of '2026-01,2026-03' or the inclusive range '2026-01..2026-10', sorted, unique."""
    parts = [part.strip() for part in text.split(",")]
    if len(parts) == 1 and ".." in parts[0]:
        first, _, last = parts[0].partition("..")
        start, stop = _month_index(first), _month_index(last)
        if start > stop:
            raise ValueError(f"month range runs backwards: {text!r}")
        return [_month_text(index) for index in range(start, stop + 1)]
    return sorted({_month_text(_month_index(part)) for part in parts})


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


def reduce_era5_daily_file(
    nc_path: str | Path, root: Path, *, replace: bool = False
) -> tuple[int, int]:
    """Reduce one daily netCDF into the parquet; replace first drops the file's stored months."""
    frame = era5_daily_frame(nc_path)
    if frame.empty:
        raise ValueError(f"no complete day in {nc_path}")
    path = root / DAILY_PARQUET[0] / DAILY_PARQUET[1]
    if not (replace and path.exists()):
        return append_parquet(frame, path, DAILY_KEYS)
    stored = pd.read_parquet(path, engine="pyarrow")
    months = frame["date"].dt.to_period("M").unique()
    kept = stored[~stored["date"].dt.to_period("M").isin(months)]
    merged = (
        pd.concat([kept, frame], ignore_index=True)
        .drop_duplicates(subset=DAILY_KEYS, keep="first")
        .sort_values(DAILY_KEYS)
        .reset_index(drop=True)
    )
    _write_whole(merged, path)
    return len(merged) - len(kept), len(merged)


def _write_whole(frame: pd.DataFrame, path: Path) -> None:
    """Write the parquet through a temporary file beside it; the old one stays whole until then."""
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(handle)
    try:
        frame.to_parquet(name, engine="pyarrow", index=False)
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def daily_task(root: Path, year: int) -> tuple[str, dict, Path]:
    """The (dataset, request, raw file) task of one year, as run_tasks takes it."""
    out = root / "raw" / f"era5_{int(year)}_t2m_6hourly.nc"
    return ERA5_HOURLY_DATASET, build_era5_daily_request(year), out


def daily_month_task(root: Path, month: str, *, today: date) -> tuple[str, dict, Path]:
    """The (dataset, request, raw file) task of one YYYY-MM month, as run_tasks takes it."""
    request = build_era5_daily_month_request(month, today=today)
    return ERA5_HOURLY_DATASET, request, root / "raw" / f"era5_{month}_t2m_6hourly.nc"
