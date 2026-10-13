"""Tests for the CDS forecast pipeline (no network, no cdsapi import)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from climate import cds
from climate.cds import build_era5_monthly_request, build_seas5_request, fetch
from climate.reduce import (
    append_parquet,
    era5_frame,
    global_mean_series,
    reduce_era5_file,
    reduce_seas5_file,
    seas5_frame,
)
from scripts.fetch_climate_forecasts import parse_years, recent_init_months

def test_build_seas5_request_snapshot():
    req = build_seas5_request(2026, [7, 8], range(1, 7))
    assert req == {
        "originating_centre": "ecmwf",
        "system": "51",
        "variable": "2m_temperature",
        "product_type": "monthly_mean",
        "year": "2026",
        "month": ["07", "08"],
        "leadtime_month": ["1", "2", "3", "4", "5", "6"],
        "data_format": "netcdf",
        "grid": "1.0/1.0",
    }


def test_build_seas5_request_custom_grid_and_string_inputs():
    req = build_seas5_request("1999", ["01"], ["1"], grid="2.5/2.5")
    assert req["year"] == "1999"
    assert req["month"] == ["01"]
    assert req["leadtime_month"] == ["1"]
    assert req["grid"] == "2.5/2.5"


def test_build_era5_monthly_request_snapshot():
    req = build_era5_monthly_request([2024, 2025])
    assert req == {
        "product_type": "monthly_averaged_reanalysis",
        "variable": "2m_temperature",
        "year": ["2024", "2025"],
        "month": ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"],
        "time": "00:00",
        "data_format": "netcdf",
        "grid": "1.0/1.0",
    }


def _synthetic_seas5(nc_path: Path) -> None:
    """t2m = latitude value + member index; lat-symmetric grid."""
    lats = np.array([-60.0, -30.0, 0.0, 30.0, 60.0])
    lons = np.array([0.0, 90.0, 180.0, 270.0])
    members = np.array([0, 1, 2])
    leads = np.array([1, 2])
    init = np.array([np.datetime64("2026-08-01")])

    data = np.zeros((1, len(members), len(leads), len(lats), len(lons)))
    for mi, m in enumerate(members):
        data[0, mi, :, :, :] = m
    data += lats[None, None, None, :, None]  # weighted-mean contribution: 0

    ds = xr.Dataset(
        {
            "t2m": (
                ("forecast_reference_time", "number", "forecastMonth", "latitude", "longitude"),
                data,
            )
        },
        coords={
            "forecast_reference_time": init,
            "number": members,
            "forecastMonth": leads,
            "latitude": lats,
            "longitude": lons,
        },
    )
    ds.to_netcdf(nc_path)


def _synthetic_era5(nc_path: Path) -> None:
    lats = np.array([-45.0, 0.0, 45.0])
    lons = np.array([0.0, 180.0])
    times = np.array([np.datetime64("2024-01-01"), np.datetime64("2024-02-01")])
    data = np.zeros((len(times), len(lats), len(lons)))
    data[0] = 280.0
    data[1] = 281.5
    ds = xr.Dataset(
        {"t2m": (("valid_time", "latitude", "longitude"), data)},
        coords={"valid_time": times, "latitude": lats, "longitude": lons},
    )
    ds.to_netcdf(nc_path)


def test_global_mean_series_analytic_zero(tmp_path):
    nc = tmp_path / "seas5.nc"
    _synthetic_seas5(nc)
    reduced = global_mean_series(nc)
    # member 0: latitude-only field -> weighted mean exactly 0
    val = float(reduced.sel(number=0, forecastMonth=1).squeeze())
    assert abs(val) < 1e-9


def test_seas5_frame_shape_and_values(tmp_path):
    nc = tmp_path / "seas5.nc"
    _synthetic_seas5(nc)
    df = seas5_frame(nc)
    assert list(df.columns) == ["init_date", "lead_month", "member", "t2m_global_K"]
    assert len(df) == 3 * 2  # members x leads
    assert set(df["lead_month"]) == {1, 2}
    assert set(df["member"]) == {0, 1, 2}
    assert (df["init_date"] == pd.Timestamp("2026-08-01")).all()
    for m in (0, 1, 2):
        vals = df.loc[df["member"] == m, "t2m_global_K"]
        assert np.allclose(vals, float(m), atol=1e-9)


def test_era5_frame_months_and_values(tmp_path):
    nc = tmp_path / "era5.nc"
    _synthetic_era5(nc)
    df = era5_frame(nc)
    assert list(df.columns) == ["month", "t2m_global_K"]
    assert list(df["month"]) == [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-02-01")]
    assert np.allclose(df["t2m_global_K"], [280.0, 281.5], atol=1e-9)


def test_era5_frame_handles_expver_dim(tmp_path):
    lats = np.array([-30.0, 30.0])
    lons = np.array([0.0, 180.0])
    times = np.array([np.datetime64("2026-05-01")])
    data = np.full((2, 1, 2, 2), np.nan)
    data[1, 0, :, :] = 290.0  # only ERA5T slice populated
    ds = xr.Dataset(
        {"t2m": (("expver", "valid_time", "latitude", "longitude"), data)},
        coords={"expver": ["0001", "0005"], "valid_time": times,
                "latitude": lats, "longitude": lons},
    )
    nc = tmp_path / "era5t.nc"
    ds.to_netcdf(nc)
    df = era5_frame(nc)
    assert np.allclose(df["t2m_global_K"], [290.0], atol=1e-9)


def test_append_parquet_dedupes_and_keeps_first(tmp_path):
    path = tmp_path / "out.parquet"
    df1 = pd.DataFrame({"k": [1, 2], "v": [10.0, 20.0]})
    added, total = append_parquet(df1, path, ["k"])
    assert (added, total) == (2, 2)

    # re-append with an overlapping key and a changed value: existing wins
    df2 = pd.DataFrame({"k": [2, 3], "v": [999.0, 30.0]})
    added, total = append_parquet(df2, path, ["k"])
    assert (added, total) == (1, 3)
    stored = pd.read_parquet(path)
    assert float(stored.loc[stored["k"] == 2, "v"].iloc[0]) == 20.0


def test_reduce_files_are_idempotent(tmp_path):
    root = tmp_path
    nc = tmp_path / "seas5.nc"
    _synthetic_seas5(nc)
    _, total1 = reduce_seas5_file(nc, root)
    _, total2 = reduce_seas5_file(nc, root)
    assert total1 == total2 == 6

    nc2 = tmp_path / "era5.nc"
    _synthetic_era5(nc2)
    _, etotal1 = reduce_era5_file(nc2, root)
    _, etotal2 = reduce_era5_file(nc2, root)
    assert etotal1 == etotal2 == 2


def test_fetch_skips_existing_file_without_client(tmp_path, monkeypatch):
    out = tmp_path / "cached.nc"
    out.write_bytes(b"already here")

    def _boom():
        raise AssertionError("client must not be constructed on cache hit")

    monkeypatch.setattr(cds, "_make_client", _boom)
    result = fetch("any-dataset", {"x": 1}, out)
    assert result == out
    assert out.read_bytes() == b"already here"


def test_fetch_downloads_via_client_and_renames(tmp_path, monkeypatch):
    out = tmp_path / "fresh.nc"
    calls = {}

    class FakeClient:
        def retrieve(self, dataset, request, target):
            calls["args"] = (dataset, request, target)
            Path(target).write_bytes(b"netcdf-bytes")

    monkeypatch.setattr(cds, "_make_client", lambda: FakeClient())
    result = fetch("some-dataset", {"y": 2}, out)
    assert result == out
    assert out.read_bytes() == b"netcdf-bytes"
    assert calls["args"][0] == "some-dataset"
    assert calls["args"][2].endswith(".nc.part")  # partial-download guard
    assert not out.with_suffix(".nc.part").exists()


def test_fetch_soft_timeout_raises(tmp_path, monkeypatch):
    import threading

    release = threading.Event()

    class SlowClient:
        def retrieve(self, dataset, request, target):
            release.wait(5.0)

    monkeypatch.setattr(cds, "_make_client", lambda: SlowClient())
    with pytest.raises(cds.CdsSoftTimeout):
        fetch("slow-dataset", {}, tmp_path / "slow.nc", soft_timeout=0.05)
    release.set()


def test_fetch_reraises_client_errors(tmp_path, monkeypatch):
    class FailingClient:
        def retrieve(self, dataset, request, target):
            raise RuntimeError("required licences not accepted")

    monkeypatch.setattr(cds, "_make_client", lambda: FailingClient())
    with pytest.raises(RuntimeError, match="licences not accepted"):
        fetch("d", {}, tmp_path / "fail.nc")
    assert not (tmp_path / "fail.nc").exists()


def test_parse_years_range_and_list():
    assert parse_years("1993-1996") == [1993, 1994, 1995, 1996]
    assert parse_years("1995,1997") == [1995, 1997]


def test_recent_init_months_mid_month():
    # after the ~13th release, current month counts as available
    assert recent_init_months(date(2026, 8, 18)) == [(2026, 8), (2026, 7)]


def test_recent_init_months_early_month_and_year_boundary():
    assert recent_init_months(date(2026, 8, 5)) == [(2026, 7), (2026, 6)]
    assert recent_init_months(date(2026, 1, 3)) == [(2025, 12), (2025, 11)]
