"""Previous Runs payload parsing + effective-init arithmetic. Synthetic only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weather.history import (
    PREVIOUS_DAY_MAX,
    PREVIOUS_DAY_MIN,
    effective_init_utc,
    previous_runs_payload_to_daily,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TZ_NY = "America/New_York"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(
        (FIXTURES / "open_meteo_previous_runs_sample.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def parsed(payload):
    return previous_runs_payload_to_daily(
        payload,
        station_icao="KLGA",
        model_name="gfs_seamless",
        timezone_name=TZ_NY,
        temperature_unit="fahrenheit",
    )


# ---- effective init ---- #


def test_effective_init_arithmetic():
    # EDT (UTC-4): local day 2024-06-15 ends 2024-06-16T00:00-04:00 = 06-16T04:00Z.
    assert effective_init_utc("2024-06-15", 1, TZ_NY) == "2024-06-15T04:00:00+00:00"
    assert effective_init_utc("2024-06-15", 3, TZ_NY) == "2024-06-13T04:00:00+00:00"


def test_effective_init_dst_spring_forward():
    # 2024-03-10 is a 23h local day; it still ends at 2024-03-11T00:00-04:00
    # = 2024-03-11T04:00Z, so N=1 init is 03-10T04:00Z (tz-aware, not date*24).
    assert effective_init_utc("2024-03-10", 1, TZ_NY) == "2024-03-10T04:00:00+00:00"
    # The EST day before ends at 2024-03-10T00:00-05:00 = 03-10T05:00Z.
    assert effective_init_utc("2024-03-09", 1, TZ_NY) == "2024-03-09T05:00:00+00:00"


def test_effective_init_dst_fall_back():
    # 2024-11-03 is a 25h day (America/Chicago): ends 11-04T00:00-06:00 = 06:00Z.
    assert (
        effective_init_utc("2024-11-03", 1, "America/Chicago") == "2024-11-03T06:00:00+00:00"
    )


# ---- payload parsing ---- #


def test_lead_day_mapping(parsed):
    # previous_day1 -> lead 0, previous_day2 -> lead 1; all-null day3 skipped;
    # previous_day0 (values >= 1000 in the fixture) never ingested.
    assert set(parsed["lead_day"]) == {0, 1}
    assert PREVIOUS_DAY_MIN == 1 and PREVIOUS_DAY_MAX == 7
    assert (parsed["temp_max"] < 1000).all()
    assert (parsed["source_api"] == "previous_runs").all()


def test_full_local_day_aggregation(parsed):
    # Local 2024-06-15 (EDT) = UTC indices 4..27 of the fixture ramp 0..47.
    row = parsed[(parsed["valid_local_date"] == "2024-06-15") & (parsed["lead_day"] == 0)]
    assert len(row) == 1
    r = row.iloc[0]
    assert r["temp_max"] == 27.0
    assert r["temp_min"] == 4.0
    assert r["n_hours"] == 24
    assert r["forecast_init_utc"] == "2024-06-15T04:00:00+00:00"


def test_null_handling_reduces_n_hours(parsed):
    # previous_day2 has 4 nulls at UTC indices 10..13, all inside local 06-15.
    row = parsed[(parsed["valid_local_date"] == "2024-06-15") & (parsed["lead_day"] == 1)]
    r = row.iloc[0]
    assert r["n_hours"] == 20
    assert r["temp_min"] == 104.0
    assert r["temp_max"] == 127.0


def test_partial_boundary_days_kept_with_true_counts(parsed):
    lead0 = parsed[parsed["lead_day"] == 0].set_index("valid_local_date")
    assert lead0.loc["2024-06-14", "n_hours"] == 4  # UTC 00..03 only
    assert lead0.loc["2024-06-16", "n_hours"] == 20  # UTC 28..47
    assert lead0.loc["2024-06-16", "temp_max"] == 47.0


def test_empty_payload():
    df = previous_runs_payload_to_daily(
        {},
        station_icao="KLGA",
        model_name="gfs_seamless",
        timezone_name=TZ_NY,
        temperature_unit="fahrenheit",
    )
    assert df.empty
    assert "lead_day" in df.columns
