"""Daily ERA5 global means for the nowcast: the requests, the reducer and the fetch script."""

from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from climate import cds
from climate.nn import daily
from climate.nn.daily import (
    DAILY_KEYS,
    DAILY_PARQUET,
    DAILY_TIMES,
    ERA5_HOURLY_DATASET,
    build_era5_daily_month_request,
    build_era5_daily_request,
    daily_month_task,
    daily_task,
    era5_daily_frame,
    parse_months,
    reduce_era5_daily_file,
)
from scripts.research import gtm_nn_fetch
from scripts.research.gtm_nn_fetch import main
from src.utils.live_root import MIRROR_MARKER

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "research" / "gtm_nn_fetch.py"
LATS = np.array([-45.0, 0.0, 45.0])
LONS = np.array([0.0, 90.0, 180.0, 270.0])


# the synthetic files


def _field(day: int, step: int) -> np.ndarray:
    """One 3 x 4 step: a lopsided latitude profile and a curved step profile keep it honest."""
    lat_profile = np.array([-12.0, 15.0, -3.0])[:, None]
    lon_profile = np.array([0.0, 1.0, 2.0, 3.0])[None, :]
    return 250.0 + 3.0 * day + 0.25 * step**2 + lat_profile + lon_profile


def _expected_mean(day: int) -> float:
    """The day's four steps, cosine-latitude weighted, summed by a route of their own."""
    weights = np.cos(np.deg2rad(LATS))
    per_step = [np.average(_field(day, step).mean(axis=1), weights=weights) for step in range(4)]
    return float(np.mean(per_step))


def _synthetic_hourly_file(
    tmp_path,
    days,
    steps_on_last_day,
    *,
    name="hourly.nc",
    start="2001-03-01",
    time_name="valid_time",
    expver=False,
    offset=0.0,
) -> Path:
    """A 6-hourly float32 t2m file: four steps a day, the last day with steps_on_last_day."""
    stamps, fields = [], []
    for day in range(days):
        for step in range(steps_on_last_day if day == days - 1 else 4):
            stamps.append(pd.Timestamp(start) + pd.Timedelta(days=day, hours=6 * step))
            fields.append(_field(day, step))
    data = (np.stack(fields) + offset).astype("float32")
    dims = (time_name, "latitude", "longitude")
    coords = {time_name: pd.DatetimeIndex(stamps), "latitude": LATS, "longitude": LONS}
    if expver:  # ERA5 and ERA5T alternate: one of the two slices is NaN at every step
        split = np.full((2, *data.shape), np.nan, dtype="float32")
        split[0, ::2] = data[::2]
        split[1, 1::2] = data[1::2]
        variable = (("expver", *dims), split)
        coords["expver"] = ["0001", "0005"]
    else:
        variable = (dims, data)
    path = tmp_path / name
    xr.Dataset({"t2m": variable}, coords=coords).to_netcdf(path)
    return path


def _stored(root):
    """The daily parquet under root, as the nowcast loads it."""
    return pd.read_parquet(root.joinpath(*DAILY_PARQUET))


def test_the_synthetic_field_makes_the_weights_and_the_days_matter():
    plain = np.mean([_field(0, step).mean() for step in range(4)])
    assert abs(_expected_mean(0) - plain) > 1.0
    assert _expected_mean(1) - _expected_mean(0) == pytest.approx(3.0, abs=1e-9)


# the request


def test_daily_request_names_four_steps_every_day_of_the_year_on_the_one_degree_grid():
    r = build_era5_daily_request(1993)
    assert r["year"] == "1993" and r["time"] == ["00:00", "06:00", "12:00", "18:00"]
    assert len(r["day"]) == 31 and len(r["month"]) == 12
    assert r["grid"] == "1.0/1.0" and r["product_type"] == "reanalysis"


def test_daily_request_snapshot():
    assert build_era5_daily_request(2001) == {
        "product_type": "reanalysis",
        "variable": "2m_temperature",
        "year": "2001",
        "month": [f"{m:02d}" for m in range(1, 13)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "time": ["00:00", "06:00", "12:00", "18:00"],
        "data_format": "netcdf",
        "grid": "1.0/1.0",
    }


def test_daily_request_takes_another_grid():
    assert build_era5_daily_request(2001, grid="2.5/2.5")["grid"] == "2.5/2.5"


def test_the_names_the_nowcast_reads_are_pinned():
    assert ERA5_HOURLY_DATASET == "reanalysis-era5-single-levels"
    assert DAILY_TIMES == ("00:00", "06:00", "12:00", "18:00")
    assert DAILY_PARQUET == ("reference", "era5_daily_global_mean.parquet")
    assert DAILY_KEYS == ["date"]


def test_daily_task_is_one_year_and_its_raw_file(tmp_path):
    dataset, request, out = daily_task(tmp_path, 1993)
    assert dataset == ERA5_HOURLY_DATASET and request == build_era5_daily_request(1993)
    assert out == tmp_path / "raw" / "era5_1993_t2m_6hourly.nc"


# the month requests


def test_month_request_lists_only_the_days_available_by_today():
    r = build_era5_daily_month_request("2026-10", today=date(2026, 10, 27))
    assert r["year"] == "2026" and r["month"] == ["10"]
    assert r["day"] == [f"{d:02d}" for d in range(1, 23)]
    assert r["download_format"] == "unarchived" and r["grid"] == "1.0/1.0"
    assert r["time"] == list(DAILY_TIMES)


def test_month_request_for_a_past_month_lists_every_day():
    today = date(2026, 10, 27)
    assert len(build_era5_daily_month_request("2026-02", today=today)["day"]) == 28
    assert len(build_era5_daily_month_request("2024-02", today=today)["day"]) == 29


def test_month_request_refuses_a_month_with_no_available_day():
    with pytest.raises(ValueError, match="2026-11"):
        build_era5_daily_month_request("2026-11", today=date(2026, 11, 3))
    assert build_era5_daily_month_request("2026-11", today=date(2026, 11, 6))["day"] == ["01"]


@pytest.mark.parametrize(
    ("month", "today", "last"),
    [
        ("2026-09", date(2026, 10, 4), 29),
        ("2026-09", date(2026, 10, 5), 30),
        ("2026-09", date(2026, 10, 6), 30),
        ("2025-12", date(2026, 1, 3), 29),
        ("2025-12", date(2026, 1, 6), 31),
    ],
)
def test_month_request_stops_at_the_cutoff_or_the_end_of_the_month(month, today, last):
    days = build_era5_daily_month_request(month, today=today)["day"]
    assert days == [f"{day:02d}" for day in range(1, last + 1)]


def test_month_request_snapshot():
    request = build_era5_daily_month_request("2026-02", today=date(2026, 10, 27))
    assert request == {
        "product_type": "reanalysis",
        "variable": "2m_temperature",
        "year": "2026",
        "month": ["02"],
        "day": [f"{d:02d}" for d in range(1, 29)],
        "time": ["00:00", "06:00", "12:00", "18:00"],
        "data_format": "netcdf",
        "grid": "1.0/1.0",
        "download_format": "unarchived",
    }


def test_month_request_takes_another_grid():
    request = build_era5_daily_month_request("2026-02", today=date(2026, 10, 27), grid="2.5/2.5")
    assert request["grid"] == "2.5/2.5"


def test_month_request_follows_the_availability_lag(monkeypatch):
    monkeypatch.setattr(daily, "ERA5_LAG_DAYS", 2)
    days = build_era5_daily_month_request("2026-10", today=date(2026, 10, 27))["day"]
    assert days[-1] == "25"


FULL_WIDTH_MONTH = "".join(chr(ord(c) + 0xFEE0) if c.isdigit() else c for c in "2026-10")


@pytest.mark.parametrize(
    "month",
    [
        "",
        " 2026-10",
        "2026-10\n",
        "2026-1",
        "2026-13",
        "2026-00",
        "0000-05",
        "2026/10",
        "26-10",
        "2026-10-01",
        "abcd-ef",
        FULL_WIDTH_MONTH,
    ],
)
def test_month_request_refuses_anything_but_yyyy_mm(month):
    with pytest.raises(ValueError, match="YYYY-MM"):
        build_era5_daily_month_request(month, today=date(2026, 10, 27))


def test_the_month_request_and_task_take_today_by_keyword_and_have_no_default(tmp_path):
    with pytest.raises(TypeError):
        build_era5_daily_month_request("2026-10")
    with pytest.raises(TypeError):
        daily_month_task(tmp_path, "2026-10")


def test_daily_month_task_names_the_raw_file_by_month(tmp_path):
    today = date(2026, 10, 27)
    dataset, request, out = daily_month_task(tmp_path, "2026-10", today=today)
    assert dataset == ERA5_HOURLY_DATASET
    assert out == tmp_path / "raw" / "era5_2026-10_t2m_6hourly.nc"
    assert request == build_era5_daily_month_request("2026-10", today=today)


def test_daily_month_task_refuses_a_month_with_no_available_day(tmp_path):
    message = "no ERA5 day of 2026-11 is available by 2026-11-03"
    with pytest.raises(ValueError, match=f"^{message}$"):
        daily_month_task(tmp_path, "2026-11", today=date(2026, 11, 3))


def test_parse_months_accepts_lists_and_ranges():
    assert parse_months("2026-03,2026-01") == ["2026-01", "2026-03"]
    assert parse_months("2025-11..2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    with pytest.raises(ValueError):
        parse_months("2026-1")


def test_parse_months_sorts_removes_duplicates_and_strips_blanks():
    assert parse_months("2026-05") == ["2026-05"]
    assert parse_months("2026-03, 2026-01 ,2026-03") == ["2026-01", "2026-03"]
    assert parse_months("2026-03..2026-03") == ["2026-03"]
    assert parse_months(" 2026-03..2026-04 ") == ["2026-03", "2026-04"]
    assert len(parse_months("2024-01..2026-12")) == 36


@pytest.mark.parametrize(
    "text",
    [
        "",
        ",",
        "abc",
        "2026-1",
        "2026-13",
        "2026-00",
        "2026-01,",
        "2026-01,abc",
        "2026-01-15",
        "2026/01",
        "2026-01 2026-02",
        "2026-03..2026-01",
        "2026-01..",
        "..2026-03",
        "2026-01..2026-02..2026-03",
        "2026-01..2026-03,2026-05",
    ],
)
def test_parse_months_refuses_anything_but_months(text):
    with pytest.raises(ValueError):
        parse_months(text)


# the reducer


def test_daily_frame_averages_the_four_steps_and_drops_partial_days(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, days=3, steps_on_last_day=3)
    frame = era5_daily_frame(nc)
    assert len(frame) == 2 and (frame["date"].dt.hour == 0).all()
    assert frame["t2m_global_K"].iloc[0] == pytest.approx(_expected_mean(day=0))


def test_daily_frame_has_the_columns_dates_and_values_the_nowcast_reads(tmp_path):
    frame = era5_daily_frame(_synthetic_hourly_file(tmp_path, days=3, steps_on_last_day=4))
    assert list(frame.columns) == ["date", "t2m_global_K"]
    assert frame["date"].dtype == "datetime64[ns]" and frame["date"].dt.tz is None
    assert list(frame["date"]) == [pd.Timestamp(f"2001-03-0{day}") for day in (1, 2, 3)]
    assert frame["t2m_global_K"].dtype == np.float64
    expected = [_expected_mean(day) for day in range(3)]
    np.testing.assert_allclose(frame["t2m_global_K"], expected, rtol=1e-9)


def test_daily_frame_is_sorted_by_date_whatever_the_order_of_the_file(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, days=4, steps_on_last_day=4)
    with xr.open_dataset(nc) as ds:
        reversed_ds = ds.isel(valid_time=slice(None, None, -1)).load()
    backwards = tmp_path / "backwards.nc"
    reversed_ds.to_netcdf(backwards)
    frame = era5_daily_frame(backwards)
    assert frame["date"].is_monotonic_increasing and len(frame) == 4
    assert frame["t2m_global_K"].iloc[0] == pytest.approx(_expected_mean(0))


def test_daily_frame_reads_a_time_coordinate_named_time(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, days=2, steps_on_last_day=4, time_name="time")
    frame = era5_daily_frame(nc)
    assert list(frame["date"]) == [pd.Timestamp("2001-03-01"), pd.Timestamp("2001-03-02")]
    assert frame["t2m_global_K"].iloc[1] == pytest.approx(_expected_mean(1))


def test_daily_frame_averages_the_expver_dimension_away(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, days=2, steps_on_last_day=4, expver=True)
    frame = era5_daily_frame(nc)
    assert len(frame) == 2
    np.testing.assert_allclose(frame["t2m_global_K"], [_expected_mean(0), _expected_mean(1)])


def test_daily_frame_of_a_file_without_a_complete_day_is_empty_but_typed(tmp_path):
    frame = era5_daily_frame(_synthetic_hourly_file(tmp_path, days=1, steps_on_last_day=3))
    assert frame.empty and list(frame.columns) == ["date", "t2m_global_K"]
    assert frame["date"].dtype == "datetime64[ns]" and frame["t2m_global_K"].dtype == np.float64


def test_daily_frame_drops_a_day_with_a_fifth_duplicated_stamp(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, days=3, steps_on_last_day=4)
    with xr.open_dataset(nc) as ds:
        doubled = xr.concat([ds, ds.isel(valid_time=[1])], dim="valid_time").load()
    doubled.to_netcdf(tmp_path / "doubled.nc")
    frame = era5_daily_frame(tmp_path / "doubled.nc")
    assert list(frame["date"]) == [pd.Timestamp("2001-03-02"), pd.Timestamp("2001-03-03")]
    np.testing.assert_allclose(frame["t2m_global_K"], [_expected_mean(1), _expected_mean(2)])


def test_daily_frame_drops_a_day_with_an_all_nan_step(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, days=3, steps_on_last_day=4)
    with xr.open_dataset(nc) as ds:
        holed = ds.load()
    holed["t2m"][{"valid_time": 5}] = np.nan  # day 1, second step
    holed.to_netcdf(tmp_path / "holed.nc")
    frame = era5_daily_frame(tmp_path / "holed.nc")
    assert list(frame["date"]) == [pd.Timestamp("2001-03-01"), pd.Timestamp("2001-03-03")]
    np.testing.assert_allclose(frame["t2m_global_K"], [_expected_mean(0), _expected_mean(2)])


def test_reduce_daily_is_idempotent(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, days=3, steps_on_last_day=4)
    assert reduce_era5_daily_file(nc, tmp_path) == (3, 3)
    assert reduce_era5_daily_file(nc, tmp_path) == (0, 3)


def test_reduce_daily_writes_the_parquet_the_nowcast_loads(tmp_path):
    reduce_era5_daily_file(_synthetic_hourly_file(tmp_path, 3, 4), tmp_path)
    stored = pd.read_parquet(tmp_path.joinpath(*DAILY_PARQUET))
    assert list(stored.columns) == ["date", "t2m_global_K"] and len(stored) == 3
    assert stored["date"].dtype == "datetime64[ns]" and stored["date"].dt.tz is None
    assert stored["date"].iloc[0] == pd.Timestamp("2001-03-01")
    assert stored["t2m_global_K"].iloc[2] == pytest.approx(_expected_mean(2))


def test_reduce_daily_appends_a_second_file_and_keeps_the_dates_sorted(tmp_path):
    later = _synthetic_hourly_file(tmp_path, 2, 4, name="later.nc", start="2001-03-10")
    earlier = _synthetic_hourly_file(tmp_path, 3, 4, name="earlier.nc", start="2001-03-01")
    assert reduce_era5_daily_file(later, tmp_path) == (2, 2)
    assert reduce_era5_daily_file(earlier, tmp_path) == (3, 5)
    stored = pd.read_parquet(tmp_path.joinpath(*DAILY_PARQUET))
    assert stored["date"].is_monotonic_increasing and stored["date"].is_unique


def test_reduce_daily_of_a_file_without_a_complete_day_raises_and_writes_nothing(tmp_path):
    partial = _synthetic_hourly_file(tmp_path, 1, 3, name="partial.nc", start="2001-04-01")
    with pytest.raises(ValueError, match="no complete day"):
        reduce_era5_daily_file(partial, tmp_path)
    assert not tmp_path.joinpath(*DAILY_PARQUET).exists()
    reduce_era5_daily_file(_synthetic_hourly_file(tmp_path, 3, 4), tmp_path)
    with pytest.raises(ValueError, match="no complete day"):
        reduce_era5_daily_file(partial, tmp_path)
    assert len(pd.read_parquet(tmp_path.joinpath(*DAILY_PARQUET))) == 3


def _month_rows(frame, month):
    return frame[frame["date"].dt.month == month].reset_index(drop=True)


def test_reduce_with_replace_overwrites_the_stored_days_of_the_file_months(tmp_path):
    first = _synthetic_hourly_file(tmp_path, 3, 4, name="a.nc", start="2026-02-01")
    assert reduce_era5_daily_file(first, tmp_path) == (3, 3)
    second = _synthetic_hourly_file(tmp_path, 3, 4, name="b.nc", start="2026-02-01", offset=1.0)
    assert reduce_era5_daily_file(second, tmp_path) == (0, 3)  # keep-first
    kept = _stored(tmp_path)["t2m_global_K"].to_numpy()
    np.testing.assert_allclose(kept, era5_daily_frame(first)["t2m_global_K"])
    assert reduce_era5_daily_file(second, tmp_path, replace=True) == (3, 3)
    stored = _stored(tmp_path)
    np.testing.assert_allclose(stored["t2m_global_K"], era5_daily_frame(second)["t2m_global_K"])
    np.testing.assert_allclose(stored["t2m_global_K"], kept + 1.0, rtol=1e-6)
    assert stored["date"].dtype == "datetime64[ns]" and stored["date"].dt.tz is None


def test_replace_touches_only_the_months_in_the_file(tmp_path):
    january = _synthetic_hourly_file(tmp_path, 3, 4, name="jan.nc", start="2026-01-10")
    february = _synthetic_hourly_file(tmp_path, 3, 4, name="feb.nc", start="2026-02-10")
    reduce_era5_daily_file(january, tmp_path)
    reduce_era5_daily_file(february, tmp_path)
    before = _stored(tmp_path)
    again = _synthetic_hourly_file(tmp_path, 3, 4, name="feb2.nc", start="2026-02-10", offset=2.0)
    assert reduce_era5_daily_file(again, tmp_path, replace=True) == (3, 6)
    after = _stored(tmp_path)
    pd.testing.assert_frame_equal(_month_rows(after, 1), _month_rows(before, 1))
    new_february = _month_rows(after, 2)["t2m_global_K"]
    np.testing.assert_allclose(
        new_february, _month_rows(before, 2)["t2m_global_K"] + 2.0, rtol=1e-6
    )
    assert after["date"].is_monotonic_increasing and after["date"].is_unique


def test_replace_drops_the_whole_calendar_month_of_the_file_not_only_its_dates(tmp_path):
    long_february = _synthetic_hourly_file(tmp_path, 5, 4, name="a.nc", start="2026-02-01")
    reduce_era5_daily_file(long_february, tmp_path)
    later = _synthetic_hourly_file(tmp_path, 2, 4, name="b.nc", start="2026-02-10")
    assert reduce_era5_daily_file(later, tmp_path, replace=True) == (2, 2)
    stored = _stored(tmp_path)
    assert list(stored["date"]) == [pd.Timestamp("2026-02-10"), pd.Timestamp("2026-02-11")]


def test_replace_covers_every_month_a_file_spans_and_no_other(tmp_path):
    for name, start, days in [("a.nc", "2026-01-25", 7), ("b.nc", "2026-02-01", 3)]:
        reduce_era5_daily_file(
            _synthetic_hourly_file(tmp_path, days, 4, name=name, start=start), tmp_path
        )
    reduce_era5_daily_file(
        _synthetic_hourly_file(tmp_path, 1, 4, name="c.nc", start="2026-03-01"), tmp_path
    )
    assert len(_stored(tmp_path)) == 11
    spanning = _synthetic_hourly_file(tmp_path, 4, 4, name="s.nc", start="2026-01-30", offset=1.0)
    assert reduce_era5_daily_file(spanning, tmp_path, replace=True) == (4, 5)
    dates = list(_stored(tmp_path)["date"])
    assert dates == [
        pd.Timestamp(day)
        for day in ("2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02", "2026-03-01")
    ]


def test_replace_without_a_parquet_just_writes_it(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, 3, 4, start="2026-02-01")
    assert reduce_era5_daily_file(nc, tmp_path, replace=True) == (3, 3)
    assert len(_stored(tmp_path)) == 3


def test_replace_with_a_file_without_a_complete_day_raises_and_keeps_the_parquet(tmp_path):
    reduce_era5_daily_file(_synthetic_hourly_file(tmp_path, 3, 4, start="2026-02-01"), tmp_path)
    parquet = tmp_path.joinpath(*DAILY_PARQUET)
    before = parquet.read_bytes()
    partial = _synthetic_hourly_file(tmp_path, 1, 3, name="partial.nc", start="2026-02-20")
    with pytest.raises(ValueError, match="no complete day"):
        reduce_era5_daily_file(partial, tmp_path, replace=True)
    assert parquet.read_bytes() == before


# the fetch script


def _label(request):
    """What a request is served under: its year, or YYYY-MM for a one-month request."""
    if len(request["month"]) == 1:
        return f"{request['year']}-{request['month'][0]}"
    return request["year"]


class FakeCds:
    """A CDS client: serve[label] is the bytes to deliver, an exception to raise or None to hang."""

    def __init__(self):
        self.requests = []
        self.serve = {}
        self.release = threading.Event()

    def retrieve(self, dataset, request, target):
        self.requests.append(request)
        item = self.serve[_label(request)]
        if item is None:
            self.release.wait(10.0)
        elif isinstance(item, BaseException):
            raise item
        else:
            Path(target).write_bytes(item)


@pytest.fixture
def cds_client(monkeypatch):
    """The CDS client factory replaced by a FakeCds that the test fills; no request leaves."""
    client = FakeCds()
    monkeypatch.setattr(cds, "_make_client", lambda: client)
    yield client
    client.release.set()


def _year_bytes(tmp_path, year):
    """The bytes of a small daily-request file whose three days fall in year."""
    nc = _synthetic_hourly_file(tmp_path, 3, 4, name=f"served_{year}.nc", start=f"{year}-01-01")
    return nc.read_bytes()


def _month_bytes(tmp_path, month, days, offset=0.0):
    """The bytes of a month file: days complete days from the 1st, every value up by offset."""
    name = f"served_{month}_{offset}.nc"
    nc = _synthetic_hourly_file(tmp_path, days, 4, name=name, start=f"{month}-01", offset=offset)
    return nc.read_bytes()


def _zipped(payload):
    """What the CDS sends when it archives its answer: a zip with the netCDF inside."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data.nc", payload)
    return buffer.getvalue()


def _raw(root, label):
    return root / "raw" / f"era5_{label}_t2m_6hourly.nc"


ITEM_LABEL = re.compile(r"\d{4}(-\d{2})?")


def _year_lines(captured):
    """The per-item lines of the output: the text before the first colon is a year or a month."""
    lines = captured.out.splitlines()
    return [line for line in lines if ITEM_LABEL.fullmatch(line.partition(":")[0])]


def _done_line(captured):
    (line,) = [line for line in captured.out.splitlines() if line.startswith("done:")]
    return line


def test_the_item_lines_are_told_from_log_lines_and_the_header():
    captured = SimpleNamespace(
        out="2026-10-27 12:00:00,123 [INFO] climate.cds: CDS request: dataset=x\n"
        "daily ERA5: 1 years 1993..1993, 0 raw files already present\n"
        "1993: era5_1993_t2m_6hourly.nc downloaded (1 bytes), +3 rows, 3 total\n"
        "2026-01: era5_2026-01_t2m_6hourly.nc cached (1 bytes), +0 rows, 3 total\n"
        "2026-02: FAILED to reduce era5_2026-02_t2m_6hourly.nc: empty download\n"
        "done: 2/3 years and months reduced\n"
    )
    labels = [line.partition(":")[0] for line in _year_lines(captured)]
    assert labels == ["1993", "2026-01", "2026-02"]


def test_fetch_cli_refuses_a_mirror_root(tmp_path, cds_client, capsys):
    (tmp_path / MIRROR_MARKER).write_text("x")
    assert main(["--root", str(tmp_path), "--years", "1993"]) == 2
    assert "mirror" in capsys.readouterr().err
    assert cds_client.requests == [] and not (tmp_path / "raw").exists()


def test_a_mirror_root_loses_no_raw_file_to_a_refresh(tmp_path, cds_client, capsys):
    (tmp_path / MIRROR_MARKER).write_text("x")
    raw = _raw(tmp_path, "2026-02")
    raw.parent.mkdir()
    raw.write_bytes(_month_bytes(tmp_path, "2026-02", 3))
    argv = ["--root", str(tmp_path), "--months", "2026-02", "--refresh", "--today", "2026-10-27"]
    assert main(argv) == 2
    assert "mirror" in capsys.readouterr().err
    assert cds_client.requests == [] and raw.exists() and raw.stat().st_size > 0


def test_fetch_cli_downloads_the_requested_years_and_reduces_them(tmp_path, cds_client, capsys):
    root = tmp_path / "root"
    cds_client.serve = {str(year): _year_bytes(tmp_path, year) for year in (1993, 1994)}
    assert main(["--root", str(root), "--years", "1993-1994"]) == 0
    assert sorted(request["year"] for request in cds_client.requests) == ["1993", "1994"]
    assert all(request["time"] == list(DAILY_TIMES) for request in cds_client.requests)
    assert all(_raw(root, year).stat().st_size > 0 for year in (1993, 1994))
    assert _stored(root)["date"].dt.year.value_counts().to_dict() == {1993: 3, 1994: 3}
    captured = capsys.readouterr()
    lines = _year_lines(captured)
    assert [line.split(":")[0] for line in lines] == ["1993", "1994"]
    assert all("downloaded" in line and "cached" not in line for line in lines)
    done = [line for line in captured.out.splitlines() if line.startswith("done:")]
    assert len(done) == 1 and done[0].startswith("done: 2/2") and "6 daily rows" in done[0]


def test_fetch_cli_takes_a_list_of_years_and_handles_each_once(tmp_path, cds_client, capsys):
    cds_client.serve = {str(year): _year_bytes(tmp_path, year) for year in (1995, 1997)}
    assert main(["--root", str(tmp_path / "root"), "--years", "1997,1995,1995"]) == 0
    assert sorted(request["year"] for request in cds_client.requests) == ["1995", "1997"]
    assert [line[:4] for line in _year_lines(capsys.readouterr())] == ["1995", "1997"]


def test_a_second_run_is_a_cache_hit_that_adds_nothing(tmp_path, cds_client, capsys):
    argv = ["--root", str(tmp_path / "root"), "--years", "1993"]
    cds_client.serve = {"1993": _year_bytes(tmp_path, 1993)}
    assert main(argv) == 0 and len(cds_client.requests) == 1
    capsys.readouterr()
    assert main(argv) == 0 and len(cds_client.requests) == 1
    (line,) = _year_lines(capsys.readouterr())
    assert "cached" in line and "downloaded" not in line and "+0 rows, 3 total" in line


def test_an_empty_raw_file_is_not_a_cache_hit(tmp_path, cds_client, capsys):
    root = tmp_path / "root"
    _raw(root, 1993).parent.mkdir(parents=True)
    _raw(root, 1993).write_bytes(b"")
    cds_client.serve = {"1993": _year_bytes(tmp_path, 1993)}
    assert main(["--root", str(root), "--years", "1993"]) == 0
    (line,) = _year_lines(capsys.readouterr())
    assert len(cds_client.requests) == 1 and "downloaded" in line and "cached" not in line


def test_a_rerun_after_a_failed_year_fetches_only_that_year(tmp_path, cds_client, capsys):
    root = tmp_path / "root"
    argv = ["--root", str(root), "--years", "1993-1994"]
    good = {str(year): _year_bytes(tmp_path, year) for year in (1993, 1994)}
    cds_client.serve = {"1993": good["1993"], "1994": RuntimeError("queue full")}
    assert main(argv) == 1
    assert "FAILED: era5_1994_t2m_6hourly.nc" in capsys.readouterr().out
    assert len(_stored(root)) == 3 and not _raw(root, 1994).exists()
    cds_client.requests.clear()
    cds_client.serve["1994"] = good["1994"]
    assert main(argv) == 0
    assert [request["year"] for request in cds_client.requests] == ["1994"]
    assert len(_stored(root)) == 6


def test_fetch_cli_tells_the_user_to_accept_the_licence_and_exits_1(tmp_path, cds_client, capsys):
    cds_client.serve = {"1993": RuntimeError("required licences not accepted")}
    assert main(["--root", str(tmp_path / "root"), "--years", "1993"]) == 1
    assert "USER ACTION required for era5_1993_t2m_6hourly.nc" in capsys.readouterr().out


def test_fetch_cli_exits_1_on_a_soft_timeout_and_reduces_nothing(tmp_path, cds_client, capsys):
    root = tmp_path / "root"
    cds_client.serve = {"1993": None}
    assert main(["--root", str(root), "--years", "1993", "--soft-timeout", "0.05"]) == 1
    out = capsys.readouterr().out
    assert "submitted/queued, not complete: era5_1993_t2m_6hourly.nc" in out
    assert "done: 0/1 years reduced" in out and "daily rows" not in out
    assert not _raw(root, 1993).exists() and not root.joinpath(*DAILY_PARQUET).exists()


def _wrong_variable_bytes(tmp_path):
    """A netCDF in the right layout whose temperature variable has a name the reducer lacks."""
    with xr.open_dataset(_synthetic_hourly_file(tmp_path, 3, 4, name="named.nc")) as ds:
        ds.rename({"t2m": "foo"}).load().to_netcdf(tmp_path / "wrong.nc")
    return (tmp_path / "wrong.nc").read_bytes()


@pytest.mark.parametrize("kind", ["not a netcdf", "no complete day", "no temperature variable"])
def test_an_unusable_download_fails_its_year_and_the_others_are_still_reduced(
    tmp_path, cds_client, capsys, kind
):
    root = tmp_path / "root"
    if kind == "not a netcdf":
        bad = b"this is not a netcdf file"
    elif kind == "no complete day":
        bad = _synthetic_hourly_file(tmp_path, 1, 3, name="bad.nc", start="1994-01-01").read_bytes()
    else:
        bad = _wrong_variable_bytes(tmp_path)
    cds_client.serve = {"1993": _year_bytes(tmp_path, 1993), "1994": bad}
    assert main(["--root", str(root), "--years", "1993-1994"]) == 1
    out = capsys.readouterr().out
    assert "1994: FAILED to reduce era5_1994_t2m_6hourly.nc" in out and "delete it" not in out
    assert "1993: era5_1993_t2m_6hourly.nc" in out and "done: 1/2" in out
    assert len(_stored(root)) == 3 and _raw(root, 1994).exists()


@pytest.mark.parametrize(
    ("kind", "named"),
    [("empty", "empty download"), ("zip", "the CDS returned a zip, not a netCDF")],
)
def test_a_known_bad_delivery_is_named_and_the_other_months_are_still_reduced(
    tmp_path, cds_client, capsys, kind, named
):
    root = tmp_path / "root"
    bad = b"" if kind == "empty" else _zipped(_month_bytes(tmp_path, "2026-02", 28))
    cds_client.serve = {"2026-01": _month_bytes(tmp_path, "2026-01", 31), "2026-02": bad}
    argv = ["--root", str(root), "--months", "2026-01,2026-02", "--today", "2026-10-27"]
    assert main(argv) == 1
    out = capsys.readouterr().out
    failed = "2026-02: FAILED to reduce era5_2026-02_t2m_6hourly.nc"
    assert f"{failed}: {named}; delete it to fetch it again" in out
    assert "2026-01: era5_2026-01_t2m_6hourly.nc downloaded" in out and "done: 1/2" in out
    assert len(_stored(root)) == 31 and _raw(root, "2026-02").exists()


def _fail_reducing_1994(monkeypatch, error):
    """The script's reducer made to raise error for the 1994 file and to work as usual otherwise."""
    real = gtm_nn_fetch.reduce_era5_daily_file

    def reducer(path, root, **kwargs):
        if "1994" in path.name:
            raise error
        return real(path, root, **kwargs)

    monkeypatch.setattr(gtm_nn_fetch, "reduce_era5_daily_file", reducer)


@pytest.mark.parametrize(
    "error", [ValueError("odd"), KeyError("odd"), OSError("odd")], ids=lambda e: type(e).__name__
)
def test_the_errors_of_a_bad_file_fail_its_item_and_the_others_are_still_reduced(
    tmp_path, cds_client, monkeypatch, capsys, error
):
    cds_client.serve = {str(year): _year_bytes(tmp_path, year) for year in (1993, 1994)}
    _fail_reducing_1994(monkeypatch, error)
    assert main(["--root", str(tmp_path / "root"), "--years", "1993-1994"]) == 1
    out = capsys.readouterr().out
    assert f"1994: FAILED to reduce era5_1994_t2m_6hourly.nc: {error!r}" in out
    assert "1993: era5_1993_t2m_6hourly.nc" in out and "done: 1/2" in out


def test_an_error_that_is_not_about_the_file_is_not_swallowed(tmp_path, cds_client, monkeypatch):
    cds_client.serve = {"1994": _year_bytes(tmp_path, 1994)}
    _fail_reducing_1994(monkeypatch, RuntimeError("a bug, not a bad file"))
    with pytest.raises(RuntimeError, match="a bug"):
        main(["--root", str(tmp_path / "root"), "--years", "1994"])


def _spy_on_run_tasks(monkeypatch):
    """Replace run_tasks by a recorder that completes nothing."""
    seen = {}

    def spy(tasks, soft_timeout, jobs):
        seen.update(tasks=tasks, soft_timeout=soft_timeout, jobs=jobs)
        return [], []

    monkeypatch.setattr(gtm_nn_fetch, "run_tasks", spy)
    return seen


def test_fetch_cli_defaults_are_the_planned_years_two_jobs_and_two_hours(tmp_path, monkeypatch):
    seen = _spy_on_run_tasks(monkeypatch)
    main(["--root", str(tmp_path)])
    assert [task[1]["year"] for task in seen["tasks"]] == [str(y) for y in range(1993, 2026)]
    assert (seen["soft_timeout"], seen["jobs"]) == (7200, 2)


def test_fetch_cli_hands_its_options_to_run_tasks(tmp_path, monkeypatch):
    seen = _spy_on_run_tasks(monkeypatch)
    logged = []
    monkeypatch.setattr(gtm_nn_fetch, "setup_logging", lambda: logged.append(True))
    argv = [
        "--root",
        str(tmp_path),
        "--years",
        "2001-2003",
        "--jobs",
        "3",
        "--soft-timeout",
        "12.5",
    ]
    main(argv)
    assert [task[2].name for task in seen["tasks"]] == [
        f"era5_{year}_t2m_6hourly.nc" for year in (2001, 2002, 2003)
    ]
    assert {task[2].parent for task in seen["tasks"]} == {tmp_path.resolve() / "raw"}
    assert (seen["soft_timeout"], seen["jobs"]) == (12.5, 3) and logged == [True]


def test_the_years_are_reduced_and_listed_in_order_whatever_order_they_finished_in(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "root"
    paths = []
    for year in (1993, 1994):
        raw = _raw(root, year)
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(_year_bytes(tmp_path, year))
        paths.append(raw)
    monkeypatch.setattr(gtm_nn_fetch, "run_tasks", lambda tasks, timeout, jobs: (paths[::-1], []))
    assert main(["--root", str(root), "--years", "1993-1994"]) == 0
    lines = _year_lines(capsys.readouterr())
    assert [line[:4] for line in lines] == ["1993", "1994"]
    assert (
        "cached" in lines[0] and "+3 rows, 3 total" in lines[0] and "+3 rows, 6 total" in lines[1]
    )


# the months and the refresh


def _before_february(frame):
    return frame[frame["date"] < pd.Timestamp("2026-02-01")].reset_index(drop=True)


def _header(captured):
    (line,) = [line for line in captured.out.splitlines() if line.startswith("daily ERA5:")]
    return line


def test_fetch_cli_months_fetch_only_the_months_and_skip_the_years(tmp_path, cds_client, capsys):
    root = tmp_path / "root"
    cds_client.serve = {
        "2026-01": _month_bytes(tmp_path, "2026-01", 31),
        "2026-02": _month_bytes(tmp_path, "2026-02", 28),
    }
    argv = ["--root", str(root), "--months", "2026-01..2026-02", "--today", "2026-10-27"]
    assert main(argv) == 0
    requests = sorted(cds_client.requests, key=lambda request: request["month"])
    shape = [(request["year"], request["month"], len(request["day"])) for request in requests]
    assert shape == [("2026", ["01"], 31), ("2026", ["02"], 28)]
    assert all(request["download_format"] == "unarchived" for request in requests)
    assert sorted(path.name for path in (root / "raw").iterdir()) == [
        "era5_2026-01_t2m_6hourly.nc",
        "era5_2026-02_t2m_6hourly.nc",
    ]
    assert _stored(root)["date"].dt.month.value_counts().to_dict() == {1: 31, 2: 28}
    captured = capsys.readouterr()
    lines = _year_lines(captured)
    assert [line.partition(":")[0] for line in lines] == ["2026-01", "2026-02"]
    assert all("downloaded" in line and "cached" not in line for line in lines)
    assert "+31 rows, 31 total" in lines[0] and "+28 rows, 59 total" in lines[1]
    assert _header(captured) == "daily ERA5: 2 months 2026-01..2026-02, 0 raw files already present"
    done = _done_line(captured)
    assert done.startswith("done: 2/2 months reduced") and done.endswith("59 daily rows")


def test_fetch_cli_years_and_months_together(tmp_path, cds_client, capsys):
    cds_client.serve = {
        "1993": _year_bytes(tmp_path, 1993),
        "2026-01": _month_bytes(tmp_path, "2026-01", 31),
    }
    argv = ["--root", str(tmp_path / "root"), "--years", "1993", "--months", "2026-01"]
    assert main([*argv, "--today", "2026-10-27"]) == 0
    assert sorted(_label(request) for request in cds_client.requests) == ["1993", "2026-01"]
    captured = capsys.readouterr()
    lines = _year_lines(captured)
    assert [line.partition(":")[0] for line in lines] == ["1993", "2026-01"]
    assert "+3 rows, 3 total" in lines[0] and "+31 rows, 34 total" in lines[1]
    assert _header(captured) == (
        "daily ERA5: 1 years 1993..1993, 1 months 2026-01..2026-01, 0 raw files already present"
    )
    done = _done_line(captured)
    assert done.startswith("done: 2/2 years and months reduced") and done.endswith("34 daily rows")


def test_fetch_cli_runs_the_years_first_and_then_the_months_in_one_call(tmp_path, monkeypatch):
    seen = _spy_on_run_tasks(monkeypatch)
    argv = ["--root", str(tmp_path), "--years", "1994,1993", "--months", "2026-03,2026-02"]
    main([*argv, "--today", "2026-10-27"])
    assert [task[2].name for task in seen["tasks"]] == [
        "era5_1993_t2m_6hourly.nc",
        "era5_1994_t2m_6hourly.nc",
        "era5_2026-02_t2m_6hourly.nc",
        "era5_2026-03_t2m_6hourly.nc",
    ]
    assert [task[1]["day"][-1] for task in seen["tasks"][2:]] == ["28", "31"]


def test_fetch_cli_months_alone_request_no_year(tmp_path, monkeypatch):
    seen = _spy_on_run_tasks(monkeypatch)
    main(["--root", str(tmp_path), "--months", "2026-02", "--today", "2026-10-27"])
    assert [task[2].name for task in seen["tasks"]] == ["era5_2026-02_t2m_6hourly.nc"]


def test_fetch_cli_refresh_deletes_and_refetches_a_cached_month(tmp_path, cds_client, capsys):
    root = tmp_path / "root"
    argv = ["--root", str(root), "--months", "2026-02", "--today", "2026-10-27"]
    cds_client.serve = {"2026-02": _month_bytes(tmp_path, "2026-02", 28)}
    assert main(argv) == 0
    old = _stored(root)["t2m_global_K"].to_numpy()
    new = _month_bytes(tmp_path, "2026-02", 28, offset=1.0)
    cds_client.serve["2026-02"] = new
    cds_client.requests.clear()
    capsys.readouterr()

    assert main(argv) == 0 and cds_client.requests == []
    (line,) = _year_lines(capsys.readouterr())
    assert "cached" in line and "+0 rows, 28 total" in line
    np.testing.assert_allclose(_stored(root)["t2m_global_K"], old)

    assert main([*argv, "--refresh"]) == 0 and len(cds_client.requests) == 1
    captured = capsys.readouterr()
    (line,) = _year_lines(captured)
    assert "refreshed" in line and "cached" not in line and "downloaded" not in line
    assert "+28 rows, 28 total" in line
    assert _header(captured).endswith("0 raw files already present, 1 deleted for refresh")
    np.testing.assert_allclose(_stored(root)["t2m_global_K"], old + 1.0, rtol=1e-6)
    assert _raw(root, "2026-02").read_bytes() == new


def test_refresh_touches_only_the_named_months_and_leaves_the_years_frozen(
    tmp_path, cds_client, capsys
):
    root = tmp_path / "root"
    base = ["--root", str(root), "--today", "2026-10-27"]
    cds_client.serve = {
        "1993": _year_bytes(tmp_path, 1993),
        "2026-01": _month_bytes(tmp_path, "2026-01", 31),
        "2026-02": _month_bytes(tmp_path, "2026-02", 28),
    }
    assert main([*base, "--years", "1993", "--months", "2026-01..2026-02"]) == 0
    frozen = {label: _raw(root, label).read_bytes() for label in ("1993", "2026-01")}
    before = _stored(root)
    cds_client.requests.clear()
    cds_client.serve["2026-02"] = _month_bytes(tmp_path, "2026-02", 28, offset=1.0)
    capsys.readouterr()

    assert main([*base, "--years", "1993", "--months", "2026-02", "--refresh"]) == 0
    assert [_label(request) for request in cds_client.requests] == ["2026-02"]
    assert {label: _raw(root, label).read_bytes() for label in frozen} == frozen
    after = _stored(root)
    pd.testing.assert_frame_equal(_before_february(after), _before_february(before))
    labelled = {line.partition(":")[0]: line for line in _year_lines(capsys.readouterr())}
    assert "cached" in labelled["1993"] and "+0 rows" in labelled["1993"]
    assert "refreshed" in labelled["2026-02"] and "+28 rows" in labelled["2026-02"]


def test_a_failed_refresh_keeps_the_stored_days_and_a_rerun_replaces_them(
    tmp_path, cds_client, capsys
):
    root = tmp_path / "root"
    argv = ["--root", str(root), "--months", "2026-02", "--today", "2026-10-27", "--refresh"]
    cds_client.serve = {"2026-02": _month_bytes(tmp_path, "2026-02", 28)}
    assert main(argv) == 0
    before = _stored(root)
    cds_client.serve["2026-02"] = RuntimeError("queue full")
    capsys.readouterr()

    assert main(argv) == 1
    assert "FAILED: era5_2026-02_t2m_6hourly.nc" in capsys.readouterr().out
    assert not _raw(root, "2026-02").exists()
    pd.testing.assert_frame_equal(_stored(root), before)

    cds_client.serve["2026-02"] = _month_bytes(tmp_path, "2026-02", 28, offset=1.0)
    assert main(argv) == 0
    np.testing.assert_allclose(
        _stored(root)["t2m_global_K"], before["t2m_global_K"] + 1.0, rtol=1e-6
    )


def test_refreshing_a_month_without_a_raw_file_downloads_it_and_still_replaces_its_days(
    tmp_path, cds_client, capsys
):
    root = tmp_path / "root"
    argv = ["--root", str(root), "--months", "2026-02", "--today", "2026-10-27"]
    cds_client.serve = {"2026-02": _month_bytes(tmp_path, "2026-02", 28)}
    assert main(argv) == 0
    old = _stored(root)["t2m_global_K"].to_numpy()
    _raw(root, "2026-02").unlink()
    cds_client.serve["2026-02"] = _month_bytes(tmp_path, "2026-02", 28, offset=1.0)
    capsys.readouterr()

    assert main([*argv, "--refresh"]) == 0
    captured = capsys.readouterr()
    (line,) = _year_lines(captured)
    assert "downloaded" in line and "refreshed" not in line and "+28 rows, 28 total" in line
    assert _header(captured).endswith("0 deleted for refresh")
    np.testing.assert_allclose(_stored(root)["t2m_global_K"], old + 1.0, rtol=1e-6)


def test_fetch_cli_month_with_no_available_day_is_a_usage_error(tmp_path, cds_client, capsys):
    root = tmp_path / "root"
    argv = ["--root", str(root), "--months", "2026-10,2026-11", "--today", "2026-11-03"]
    assert main(argv) == 2
    assert "no ERA5 day of 2026-11 is available by 2026-11-03" in capsys.readouterr().err
    assert cds_client.requests == [] and not root.exists()


def test_a_usage_error_about_a_month_deletes_no_raw_file(tmp_path, cds_client, capsys):
    root = tmp_path / "root"
    raw = _raw(root, "2026-10")
    raw.parent.mkdir(parents=True)
    raw.write_bytes(_month_bytes(tmp_path, "2026-10", 3))
    argv = ["--root", str(root), "--months", "2026-10,2026-11", "--today", "2026-11-03"]
    assert main([*argv, "--refresh"]) == 2
    assert cds_client.requests == [] and raw.stat().st_size > 0


def test_fetch_cli_refresh_needs_months(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "--refresh"]) == 2
    assert "--refresh needs --months" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "--years", "1993", "--refresh"]) == 2
    assert not (tmp_path / "raw").exists()


@pytest.fixture
def clock(monkeypatch):
    """The script's datetime replaced by one at 2026-10-27 23:30 that records how it is asked."""
    asked = []

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            asked.append(tz)
            return datetime(2026, 10, 27, 23, 30, tzinfo=tz)

    monkeypatch.setattr(gtm_nn_fetch, "datetime", FrozenDatetime)
    return asked


def test_fetch_cli_counts_back_from_the_utc_date_once_when_today_is_not_given(
    tmp_path, monkeypatch, clock
):
    seen = _spy_on_run_tasks(monkeypatch)
    main(["--root", str(tmp_path), "--months", "2026-10,2026-09"])
    assert clock == [timezone.utc]
    assert [task[1]["day"][-1] for task in seen["tasks"]] == ["30", "22"]


def test_fetch_cli_reads_no_clock_when_today_is_given(tmp_path, monkeypatch, clock):
    seen = _spy_on_run_tasks(monkeypatch)
    main(["--root", str(tmp_path), "--months", "2026-10", "--today", "2026-10-30"])
    assert clock == [] and seen["tasks"][0][1]["day"][-1] == "25"


@pytest.mark.parametrize(
    "argv",
    [
        ["--years", "abc"],
        ["--years", ""],
        ["--years", "2025-1993"],
        ["--jobs", "0"],
        ["--soft-timeout", "0"],
        ["--soft-timeout", "-5"],
        ["--months", "abc"],
        ["--months", ""],
        ["--months", "2026-13"],
        ["--months", "2026-03..2026-01"],
        ["--today", "abc"],
        ["--today", "2026-13-01"],
        ["--today", "2026-02-30"],
        ["--today", "20261027"],
        ["--today", "2026-1-5"],
        ["--today", "2026-10-27x"],
        ["--refresh"],
    ],
)
def test_fetch_cli_exits_2_on_a_bad_option_before_any_request(tmp_path, cds_client, argv, capsys):
    root = tmp_path / "root"
    assert main(["--root", str(root), "--years", "1993", *argv]) == 2
    assert "usage" in capsys.readouterr().err.lower()
    assert cds_client.requests == [] and not root.exists()


def test_fetch_cli_help_exits_0(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert all(option in out for option in ("--soft-timeout", "--months", "--refresh", "--today"))


# process level


NO_TORCH = """
import sys
sys.path.insert(0, sys.argv[1])
import climate.nn.daily
import scripts.research.gtm_nn_fetch
assert not [name for name in sys.modules if name == "torch" or name.startswith("torch.")]
"""


def test_the_fetch_script_runs_as_a_program_from_any_directory(tmp_path):
    helped = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, cwd=tmp_path
    )
    assert helped.returncode == 0 and "usage" in helped.stdout.lower(), helped.stderr
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / MIRROR_MARKER).write_text("x")
    argv = [sys.executable, str(SCRIPT), "--root", str(mirror), "--years", "1993"]
    refused = subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path)
    assert refused.returncode == 2 and "mirror" in refused.stderr, refused.stderr


def test_the_daily_module_and_the_fetch_script_import_without_torch():
    done = subprocess.run(
        [sys.executable, "-c", NO_TORCH, str(REPO_ROOT)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
