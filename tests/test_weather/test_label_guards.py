"""Failed label fetches must never clobber stored good labels. Synthetic only."""

from __future__ import annotations

import sqlite3

from collectors.weather import collect_labels_cli as cli
from collectors.weather import collect_labels_wu as wu
from weather.storage import init_weather_db

OLD_DATE = "2024-06-15"  # finalized and well past 72h at any realistic test time


def _label_row(db, platform):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT label_value, usable_for_training, label_source FROM resolution_labels "
            "WHERE platform = ?",
            (platform,),
        ).fetchall()
    finally:
        conn.close()


# ---- NWS CLI (ON CONFLICT DO UPDATE ... WHERE guard) ---- #


def test_cli_failed_fetch_keeps_good_label(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    cli.upsert_label(
        station_icao="KNYC", valid_local_date=OLD_DATE, metric="high",
        label_value=91.0, resolution_url="u", finalized=True, db_path=db,
    )
    assert _label_row(db, "us") == [(91.0, 1, "nws_cli")]
    # Outage re-run: fetch failed, label_value=None must not regress the row.
    cli.upsert_label(
        station_icao="KNYC", valid_local_date=OLD_DATE, metric="high",
        label_value=None, resolution_url="u", finalized=True, db_path=db,
    )
    assert _label_row(db, "us") == [(91.0, 1, "nws_cli")]


def test_cli_placeholder_then_real_value(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    cli.upsert_label(
        station_icao="KNYC", valid_local_date=OLD_DATE, metric="high",
        label_value=None, resolution_url="u", finalized=True, db_path=db,
    )
    assert _label_row(db, "us") == [(None, 0, "nws_cli")]
    cli.upsert_label(
        station_icao="KNYC", valid_local_date=OLD_DATE, metric="high",
        label_value=88.0, resolution_url="u", finalized=True, db_path=db,
    )
    assert _label_row(db, "us") == [(88.0, 1, "nws_cli")]


# ---- Wunderground (guarded DELETE-then-INSERT) ---- #


def test_wu_failed_fetch_keeps_good_label(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    wu.upsert_label(
        station_icao="KLGA", valid_local_date=OLD_DATE, metric="high",
        label_value=74.0, resolution_url="u", finalized=True,
        label_source="wunderground_api", db_path=db,
    )
    assert _label_row(db, "global") == [(74.0, 1, "wunderground_api")]
    # Transient API outage: the failed-fetch row must not replace stored truth.
    wu.upsert_label(
        station_icao="KLGA", valid_local_date=OLD_DATE, metric="high",
        label_value=None, resolution_url="u", finalized=True,
        label_source="wunderground_failed", db_path=db,
    )
    assert _label_row(db, "global") == [(74.0, 1, "wunderground_api")]


def test_wu_real_value_still_dedupes_sources(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    wu.upsert_label(
        station_icao="KLGA", valid_local_date=OLD_DATE, metric="high",
        label_value=73.0, resolution_url="u", finalized=True,
        label_source="wunderground_html", db_path=db,
    )
    wu.upsert_label(
        station_icao="KLGA", valid_local_date=OLD_DATE, metric="high",
        label_value=74.0, resolution_url="u", finalized=True,
        label_source="wunderground_api", db_path=db,
    )
    # DELETE-then-INSERT dedup preserved for real values: one row, API source.
    assert _label_row(db, "global") == [(74.0, 1, "wunderground_api")]


def test_wu_placeholder_created_when_nothing_stored(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    wu.upsert_label(
        station_icao="KLGA", valid_local_date=OLD_DATE, metric="high",
        label_value=None, resolution_url="u", finalized=True,
        label_source="wunderground_pending", db_path=db,
    )
    assert _label_row(db, "global") == [(None, 0, "wunderground_pending")]
