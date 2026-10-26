"""Daily ERA5 global means for the nowcast: the request, the reducer and the fetch script."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from climate import cds
from climate.nn.daily import (
    DAILY_KEYS,
    DAILY_PARQUET,
    DAILY_TIMES,
    ERA5_HOURLY_DATASET,
    build_era5_daily_request,
    daily_task,
    era5_daily_frame,
    reduce_era5_daily_file,
)
from scripts.research import gtm_nn_fetch
from scripts.research.gtm_nn_fetch import main

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
) -> Path:
    """A 6-hourly float32 t2m file: four steps a day, the last day with steps_on_last_day."""
    stamps, fields = [], []
    for day in range(days):
        for step in range(steps_on_last_day if day == days - 1 else 4):
            stamps.append(pd.Timestamp(start) + pd.Timedelta(days=day, hours=6 * step))
            fields.append(_field(day, step))
    data = np.stack(fields).astype("float32")
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


def test_reduce_daily_is_idempotent(tmp_path):
    nc = _synthetic_hourly_file(tmp_path, days=3, steps_on_last_day=4)
    assert reduce_era5_daily_file(nc, tmp_path) == (3, 3)
    assert reduce_era5_daily_file(nc, tmp_path) == (0, 3)


def test_reduce_daily_writes_the_parquet_the_nowcast_loads(tmp_path):
    reduce_era5_daily_file(_synthetic_hourly_file(tmp_path, 3, 4), tmp_path)
    stored = pd.read_parquet(tmp_path.joinpath(*DAILY_PARQUET))
    assert list(stored.columns) == ["date", "t2m_global_K"] and len(stored) == 3
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


# the fetch script


class FakeCds:
    """A CDS client: serve[year] is the bytes to deliver, an exception to raise or None to hang."""

    def __init__(self):
        self.requests = []
        self.serve = {}
        self.release = threading.Event()

    def retrieve(self, dataset, request, target):
        self.requests.append(request)
        item = self.serve[request["year"]]
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


def _raw(root, year):
    return root / "raw" / f"era5_{year}_t2m_6hourly.nc"


def _stored(root):
    return pd.read_parquet(root.joinpath(*DAILY_PARQUET))


def _year_lines(captured):
    """The per-year lines of the script's output: those that start with a year."""
    return [line for line in captured.out.splitlines() if line[:4].isdigit()]


def test_fetch_cli_refuses_a_mirror_root(tmp_path, cds_client, capsys):
    (tmp_path / ".mirror-of-spark").write_text("x")
    assert main(["--root", str(tmp_path), "--years", "1993"]) == 2
    assert "mirror" in capsys.readouterr().err
    assert cds_client.requests == [] and not (tmp_path / "raw").exists()


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


@pytest.mark.parametrize("kind", ["not a netcdf", "no complete day"])
def test_an_unusable_download_fails_its_year_and_the_others_are_still_reduced(
    tmp_path, cds_client, capsys, kind
):
    root = tmp_path / "root"
    if kind == "not a netcdf":
        bad = b"this is not a netcdf file"
    else:
        bad = _synthetic_hourly_file(tmp_path, 1, 3, name="bad.nc", start="1994-01-01").read_bytes()
    cds_client.serve = {"1993": _year_bytes(tmp_path, 1993), "1994": bad}
    assert main(["--root", str(root), "--years", "1993-1994"]) == 1
    out = capsys.readouterr().out
    assert "1994: FAILED to reduce era5_1994_t2m_6hourly.nc" in out and "delete it" in out
    assert "1993: era5_1993_t2m_6hourly.nc" in out and "done: 1/2" in out
    assert len(_stored(root)) == 3 and _raw(root, 1994).exists()


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


@pytest.mark.parametrize(
    "argv",
    [
        ["--years", "abc"],
        ["--years", ""],
        ["--years", "2025-1993"],
        ["--jobs", "0"],
        ["--soft-timeout", "0"],
        ["--soft-timeout", "-5"],
    ],
)
def test_fetch_cli_exits_2_on_a_bad_option_before_any_request(tmp_path, cds_client, argv, capsys):
    root = tmp_path / "root"
    assert main(["--root", str(root), "--years", "1993", *argv]) == 2
    assert "usage" in capsys.readouterr().err.lower()
    assert cds_client.requests == [] and not root.exists()


def test_fetch_cli_help_exits_0(capsys):
    assert main(["--help"]) == 0
    assert "--soft-timeout" in capsys.readouterr().out


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
    (mirror / ".mirror-of-spark").write_text("x")
    argv = [sys.executable, str(SCRIPT), "--root", str(mirror), "--years", "1993"]
    refused = subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path)
    assert refused.returncode == 2 and "mirror" in refused.stderr, refused.stderr


def test_the_daily_module_and_the_fetch_script_import_without_torch():
    done = subprocess.run(
        [sys.executable, "-c", NO_TORCH, str(REPO_ROOT)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
