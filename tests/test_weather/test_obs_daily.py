"""Local-calendar obs aggregation, coverage, finalization. Synthetic only."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from weather.obs_daily import (
    finalize_obs_daily,
    observations_to_daily,
    upsert_obs_daily,
)
from weather.storage import init_weather_db

TZ_CHI = "America/Chicago"


def _obs_frame(timestamps: list[str], temps: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "station_icao": "KORD",
            "valid_utc": timestamps,
            "temp_f": temps,
            "temp_c": [(t - 32.0) * 5.0 / 9.0 for t in temps],
        }
    )


@pytest.fixture()
def fall_back_day() -> pd.DataFrame:
    start = datetime(2024, 11, 3, 5, 0, tzinfo=timezone.utc)
    stamps = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S+0000") for i in range(25)]
    temps = [50.0 + i for i in range(25)]  # max 74 at the last hour, min 50
    return _obs_frame(stamps, temps)


def test_fall_back_day_grouping(fall_back_day):
    daily = observations_to_daily(fall_back_day, timezone_name=TZ_CHI)
    assert list(daily["valid_local_date"]) == ["2024-11-03"]
    r = daily.iloc[0]
    assert r["n_obs"] == 25
    assert r["n_hours_covered"] == 25  # 25 distinct elapsed UTC hours
    assert r["tmax_f"] == 74.0
    assert r["tmin_f"] == 50.0


def test_c_and_f_columns_consistent(fall_back_day):
    daily = observations_to_daily(fall_back_day, timezone_name=TZ_CHI)
    r = daily.iloc[0]
    assert r["tmax_c"] == pytest.approx((r["tmax_f"] - 32.0) * 5.0 / 9.0)
    assert r["tmin_c"] == pytest.approx((r["tmin_f"] - 32.0) * 5.0 / 9.0)


def test_partial_day_not_finalized():
    stamps = [f"2024-06-10T{h:02d}:15:00+0000" for h in range(10, 16)]  # 6 obs
    daily = observations_to_daily(_obs_frame(stamps, [70.0] * 6), timezone_name=TZ_CHI)
    assert daily.iloc[0]["n_hours_covered"] == 6
    out = finalize_obs_daily(
        daily,
        timezone_name=TZ_CHI,
        min_hours=18,
        now=datetime(2024, 6, 20, tzinfo=timezone.utc),
    )
    assert out.iloc[0]["finalized"] == 0
    assert out.iloc[0]["usable_for_training"] == 0


def test_finalized_and_72h_transition(fall_back_day):
    daily = observations_to_daily(fall_back_day, timezone_name=TZ_CHI)
    early = finalize_obs_daily(
        daily, timezone_name=TZ_CHI, now=datetime(2024, 11, 5, 0, 0, tzinfo=timezone.utc)
    )
    assert early.iloc[0]["finalized"] == 1
    assert early.iloc[0]["usable_for_training"] == 0
    # >= 72h -> usable.
    late = finalize_obs_daily(
        daily, timezone_name=TZ_CHI, now=datetime(2024, 11, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert late.iloc[0]["finalized"] == 1
    assert late.iloc[0]["usable_for_training"] == 1


def test_day_not_complete_not_finalized(fall_back_day):
    daily = observations_to_daily(fall_back_day, timezone_name=TZ_CHI)
    # Local day ends 2024-11-04T06:00Z; just before that it is incomplete.
    out = finalize_obs_daily(
        daily, timezone_name=TZ_CHI, now=datetime(2024, 11, 4, 5, 59, tzinfo=timezone.utc)
    )
    assert out.iloc[0]["finalized"] == 0


def test_upsert_idempotent(tmp_path, fall_back_day):
    db = init_weather_db(tmp_path / "weather.db")
    daily = finalize_obs_daily(
        observations_to_daily(fall_back_day, timezone_name=TZ_CHI),
        timezone_name=TZ_CHI,
        now=datetime(2024, 11, 10, tzinfo=timezone.utc),
    )
    assert upsert_obs_daily(db, "KORD", daily) == 1
    assert upsert_obs_daily(db, "KORD", daily) == 1
    conn = sqlite3.connect(db)
    try:
        n, usable = conn.execute(
            "SELECT COUNT(*), SUM(usable_for_training) FROM obs_daily"
        ).fetchone()
    finally:
        conn.close()
    assert n == 1
    assert usable == 1


def test_empty_frame():
    daily = observations_to_daily(pd.DataFrame(), timezone_name=TZ_CHI)
    assert daily.empty
    assert upsert_obs_daily(None, "KORD", daily) == 0
