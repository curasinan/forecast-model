"""No-look-ahead training join over synthetic tmp_path DBs. No network."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from weather.history import effective_init_utc
from weather.stations import Station, StationRegistry
from weather.storage import init_weather_db
from weather.training import TrainingFrameSpec, build_training_frame

TZ_NY = "America/New_York"
MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")
OFFSETS = {"ecmwf_ifs025": -4.0, "gfs_seamless": 0.0, "icon_seamless": 4.0}
STAMP = "2024-01-01T00:00:00+00:00"


def _registry() -> StationRegistry:
    stations = {
        "KTST": Station("KTST", "Test", "Testville", 40.0, -73.0, TZ_NY, "F", False, ("global",)),
        "LTST": Station("LTST", "TestC", "Ctown", 48.0, 2.0, "Europe/Paris", "C", False, ("global",)),
    }
    lags = {"ecmwf_ifs025": 7.0, "gfs_seamless": 4.0, "icon_seamless": 3.0, "default": 6.0}
    return StationRegistry(stations, lags)


def _seed_forecast(conn, *, station="KTST", model, valid_date, lead_day, tmax, tmin=None,
                   n_hours=24, tz=TZ_NY, unit="fahrenheit"):
    conn.execute(
        "INSERT OR REPLACE INTO forecast_history_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            station, model, "previous_runs",
            effective_init_utc(valid_date, lead_day + 1, tz),
            lead_day, valid_date,
            tmax, tmin if tmin is not None else tmax - 10.0,
            n_hours, unit, STAMP, "weather_v1",
        ),
    )


def _seed_obs(conn, *, station="KTST", valid_date, tmax_f, tmin_f=None,
              finalized=1, usable=1, n_hours=24):
    tmin_f = tmin_f if tmin_f is not None else tmax_f - 12.0
    conn.execute(
        "INSERT OR REPLACE INTO obs_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            station, valid_date, "iem_asos",
            tmax_f, tmin_f,
            (tmax_f - 32.0) * 5.0 / 9.0, (tmin_f - 32.0) * 5.0 / 9.0,
            24, n_hours, None, None, finalized, usable, STAMP,
        ),
    )


def _dates(n: int, start: str = "2024-05-01") -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


@pytest.fixture()
def db(tmp_path):
    return init_weather_db(tmp_path / "weather.db")


@pytest.fixture()
def seeded(db):
    conn = sqlite3.connect(db)
    for i, d in enumerate(_dates(10)):
        for m in MODELS:
            _seed_forecast(conn, model=m, valid_date=d, lead_day=1, tmax=70.0 + i + OFFSETS[m])
        _seed_obs(conn, valid_date=d, tmax_f=71.0 + i)
    conn.commit()
    conn.close()
    return db


def _spec(**kw) -> TrainingFrameSpec:
    base = dict(station_icao="KTST", metric="high", lead_day=1, models=MODELS)
    base.update(kw)
    return TrainingFrameSpec(**base)


# ---- happy path ---- #


def test_join_happy_path(seeded):
    frame = build_training_frame(seeded, _spec(), registry=_registry())
    assert len(frame) == 10
    r = frame.iloc[0]
    assert r["valid_local_date"] == "2024-05-01"
    assert r["obs_value"] == 71.0
    assert r["ens_mean"] == pytest.approx(70.0)
    assert r["ens_var"] == pytest.approx((16.0 + 0.0 + 16.0) / 3.0)  # ddof=0
    assert r["n_models"] == 3


# ---- temporal guards ---- #


def test_availability_filter_drops_slow_model(seeded):
    # decision 06:00 local on day D-1: ecmwf (lag 7h) misses it, gfs/icon make it.
    reg = _registry()
    strict = build_training_frame(seeded, _spec(decision_hour_local=6), registry=reg)
    assert strict.empty  # full ensemble required, one model dropped
    partial = build_training_frame(
        seeded, _spec(decision_hour_local=6, require_full_ensemble=False), registry=reg
    )
    assert len(partial) == 10
    assert (partial["n_models"] == 2).all()
    # gfs (0) + icon (+4): mean shifts up to 72 + i
    assert partial.iloc[0]["ens_mean"] == pytest.approx(72.0)


def test_query_time_usable_recheck(db):
    # Stored flag says usable, but at `now` the label is younger than 72h.
    conn = sqlite3.connect(db)
    for m in MODELS:
        _seed_forecast(conn, model=m, valid_date="2024-05-01", lead_day=1, tmax=70.0)
    _seed_obs(conn, valid_date="2024-05-01", tmax_f=71.0, finalized=1, usable=1)
    conn.commit()
    conn.close()
    reg = _registry()
    fresh = build_training_frame(
        db, _spec(), registry=reg, now=datetime(2024, 5, 2, tzinfo=timezone.utc)
    )
    assert fresh.empty
    mature = build_training_frame(
        db, _spec(), registry=reg, now=datetime(2024, 5, 10, tzinfo=timezone.utc)
    )
    assert len(mature) == 1


def test_unusable_obs_excluded(seeded):
    conn = sqlite3.connect(seeded)
    _seed_obs(conn, valid_date="2024-05-03", tmax_f=73.0, usable=0)
    conn.commit()
    conn.close()
    frame = build_training_frame(seeded, _spec(), registry=_registry())
    assert "2024-05-03" not in set(frame["valid_local_date"])
    assert len(frame) == 9


# ---- coverage and ensemble-composition guards ---- #


def test_low_n_hours_forecast_drops_date(seeded):
    conn = sqlite3.connect(seeded)
    _seed_forecast(conn, model="icon_seamless", valid_date="2024-05-05", lead_day=1,
                   tmax=78.0, n_hours=10)
    conn.commit()
    conn.close()
    frame = build_training_frame(seeded, _spec(), registry=_registry())
    assert "2024-05-05" not in set(frame["valid_local_date"])


def test_missing_model_composition(db):
    conn = sqlite3.connect(db)
    for m in ("gfs_seamless", "icon_seamless"):  # ecmwf absent
        _seed_forecast(conn, model=m, valid_date="2024-05-01", lead_day=1, tmax=70.0 + OFFSETS[m])
    _seed_obs(conn, valid_date="2024-05-01", tmax_f=71.0)
    conn.commit()
    conn.close()
    reg = _registry()
    assert build_training_frame(db, _spec(), registry=reg).empty
    relaxed = build_training_frame(db, _spec(require_full_ensemble=False), registry=reg)
    assert len(relaxed) == 1
    assert relaxed.iloc[0]["n_models"] == 2


def test_single_model_never_used(db):
    conn = sqlite3.connect(db)
    _seed_forecast(conn, model="gfs_seamless", valid_date="2024-05-01", lead_day=1, tmax=70.0)
    _seed_obs(conn, valid_date="2024-05-01", tmax_f=71.0)
    conn.commit()
    conn.close()
    frame = build_training_frame(
        db, _spec(require_full_ensemble=False), registry=_registry()
    )
    assert frame.empty  # one model has no spread


# ---- metric and unit selection ---- #


def test_metric_low_uses_temp_min(seeded):
    frame = build_training_frame(seeded, _spec(metric="low"), registry=_registry())
    r = frame.iloc[0]
    assert r["obs_value"] == 59.0  # tmin_f = tmax - 12
    assert r["ens_mean"] == pytest.approx(60.0)  # temp_min = temp_max - 10


def test_c_unit_station_uses_celsius_obs(db):
    conn = sqlite3.connect(db)
    tz_paris = "Europe/Paris"
    for m in MODELS:
        _seed_forecast(conn, station="LTST", model=m, valid_date="2024-05-01", lead_day=1,
                       tmax=25.0 + OFFSETS[m] / 4.0, tz=tz_paris, unit="celsius")
    _seed_obs(conn, station="LTST", valid_date="2024-05-01", tmax_f=77.0)  # 25 C
    conn.commit()
    conn.close()
    frame = build_training_frame(db, _spec(station_icao="LTST"), registry=_registry())
    assert len(frame) == 1
    assert frame.iloc[0]["obs_value"] == pytest.approx(25.0)


def test_unit_mismatch_rows_excluded(seeded):
    # A celsius-labelled forecast row for an F station must not join.
    conn = sqlite3.connect(seeded)
    _seed_forecast(conn, model="icon_seamless", valid_date="2024-05-02", lead_day=1,
                   tmax=21.0, unit="celsius")
    conn.commit()
    conn.close()
    frame = build_training_frame(seeded, _spec(), registry=_registry())
    # icon's F row for 05-02 was replaced by the celsius one -> composition breaks.
    assert "2024-05-02" not in set(frame["valid_local_date"])


def test_label_source_filter(seeded):
    conn = sqlite3.connect(seeded)
    conn.execute(
        "INSERT OR REPLACE INTO obs_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("KTST", "2024-05-01", "other_source", 99.0, 80.0, 37.2, 26.7,
         24, 24, None, None, 1, 1, STAMP),
    )
    conn.commit()
    conn.close()
    frame = build_training_frame(seeded, _spec(), registry=_registry())
    r = frame[frame["valid_local_date"] == "2024-05-01"].iloc[0]
    assert r["obs_value"] == 71.0  # iem_asos row, not the 99.0 impostor


def test_invalid_metric_raises(seeded):
    with pytest.raises(ValueError):
        build_training_frame(seeded, _spec(metric="median"), registry=_registry())


def test_empty_db_returns_empty_frame(db):
    frame = build_training_frame(db, _spec(), registry=_registry())
    assert frame.empty
    assert list(frame.columns) == [
        "valid_local_date", "obs_value", "ens_mean", "ens_var", "n_models", "label_source",
    ]


# ---- label source priority: settlement truth beats ASOS ---- #


def _seed_resolution_label(conn, *, station="KTST", valid_date, metric="high",
                          value, source="iem_cli", finalized=1, usable=1):
    conn.execute(
        "INSERT OR REPLACE INTO resolution_labels VALUES (?,?,?,?,?,?,?,?,?,?)",
        (station, "us", valid_date, metric, value, source, STAMP,
         finalized, usable, "http://example/cli"),
    )


def test_cli_label_preferred_over_asos(seeded):
    conn = sqlite3.connect(seeded)
    _seed_resolution_label(conn, valid_date="2024-05-01", value=72.0)  # ASOS says 71.0
    conn.commit()
    conn.close()
    frame = build_training_frame(seeded, _spec(), registry=_registry())
    r = frame[frame["valid_local_date"] == "2024-05-01"].iloc[0]
    assert r["obs_value"] == 72.0
    assert r["label_source"] == "iem_cli"
    # Dates without a CLI label still fall back to ASOS.
    fallback = frame[frame["valid_local_date"] == "2024-05-02"].iloc[0]
    assert fallback["obs_value"] == 72.0  # ASOS value for day index 1
    assert fallback["label_source"] == "iem_asos"


def test_priority_order_respected(seeded):
    conn = sqlite3.connect(seeded)
    _seed_resolution_label(conn, valid_date="2024-05-01", value=72.0, source="iem_cli")
    _seed_resolution_label(conn, valid_date="2024-05-01", value=99.0, source="wunderground_api")
    conn.commit()
    conn.close()
    frame = build_training_frame(seeded, _spec(), registry=_registry())
    r = frame[frame["valid_local_date"] == "2024-05-01"].iloc[0]
    assert (r["obs_value"], r["label_source"]) == (72.0, "iem_cli")


def test_label_sources_can_exclude_asos(seeded):
    conn = sqlite3.connect(seeded)
    _seed_resolution_label(conn, valid_date="2024-05-01", value=72.0)
    conn.commit()
    conn.close()
    frame = build_training_frame(
        seeded, _spec(label_sources=("iem_cli",)), registry=_registry()
    )
    assert len(frame) == 1  # only the CLI-labelled date survives
    assert frame.iloc[0]["label_source"] == "iem_cli"


def test_settlement_label_needs_72h_at_query_time(db):
    conn = sqlite3.connect(db)
    for m in MODELS:
        _seed_forecast(conn, model=m, valid_date="2024-05-01", lead_day=1, tmax=70.0)
    _seed_resolution_label(conn, valid_date="2024-05-01", value=72.0)
    conn.commit()
    conn.close()
    reg = _registry()
    fresh = build_training_frame(
        db, _spec(), registry=reg, now=datetime(2024, 5, 2, tzinfo=timezone.utc)
    )
    assert fresh.empty
    mature = build_training_frame(
        db, _spec(), registry=reg, now=datetime(2024, 5, 10, tzinfo=timezone.utc)
    )
    assert len(mature) == 1


def test_metric_scoped_labels(seeded):
    # A 'high' CLI label must not leak into a 'low' training frame.
    conn = sqlite3.connect(seeded)
    _seed_resolution_label(conn, valid_date="2024-05-01", metric="high", value=72.0)
    conn.commit()
    conn.close()
    low = build_training_frame(seeded, _spec(metric="low"), registry=_registry())
    r = low[low["valid_local_date"] == "2024-05-01"].iloc[0]
    assert r["label_source"] == "iem_asos"
    assert r["obs_value"] == 59.0


def test_celsius_station_ignores_fahrenheit_settlement_labels(db):
    tz_paris = "Europe/Paris"
    conn = sqlite3.connect(db)
    for m in MODELS:
        _seed_forecast(conn, station="LTST", model=m, valid_date="2024-05-01", lead_day=1,
                       tmax=25.0 + OFFSETS[m] / 4.0, tz=tz_paris, unit="celsius")
    _seed_obs(conn, station="LTST", valid_date="2024-05-01", tmax_f=77.0)  # 25 C
    _seed_resolution_label(conn, station="LTST", valid_date="2024-05-01", value=77.0)
    conn.commit()
    conn.close()
    frame = build_training_frame(db, _spec(station_icao="LTST"), registry=_registry())
    r = frame.iloc[0]
    # 77 F must not be joined as if it were 77 C.
    assert r["obs_value"] == pytest.approx(25.0)
    assert r["label_source"] == "iem_asos"
