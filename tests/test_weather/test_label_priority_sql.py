"""Multi-source labels must yield exactly one calibration row per prediction."""

from __future__ import annotations

import sqlite3

import pytest

from weather.storage import init_weather_db
from weather.training import label_priority_rank_sql

STAMP = "2024-06-20T00:00:00+00:00"

# Mirrors the dedup predicate in scripts/run_weather_paper.py.
DEDUP_SQL = f"""
SELECT l.label_value, l.label_source
  FROM resolution_labels l
 WHERE l.usable_for_training = 1
   AND NOT EXISTS (
       SELECT 1 FROM resolution_labels b
        WHERE b.station_icao = l.station_icao
          AND b.valid_local_date = l.valid_local_date
          AND b.metric = l.metric
          AND b.usable_for_training = 1
          AND ({label_priority_rank_sql('b')}, b.label_source)
            < ({label_priority_rank_sql('l')}, l.label_source)
   )
"""


def _seed(conn, source, value, *, usable=1, metric="high", date="2024-06-15"):
    conn.execute(
        "INSERT INTO resolution_labels VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("KLGA", "us", date, metric, value, source, STAMP, 1, usable, "u"),
    )


@pytest.fixture()
def db(tmp_path):
    return init_weather_db(tmp_path / "weather.db")


def test_single_source_unaffected(db):
    conn = sqlite3.connect(db)
    _seed(conn, "wunderground_api", 83.0)
    conn.commit()
    assert conn.execute(DEDUP_SQL).fetchall() == [(83.0, "wunderground_api")]
    conn.close()


def test_cli_wins_over_wunderground(db):
    conn = sqlite3.connect(db)
    _seed(conn, "wunderground_api", 83.0)
    _seed(conn, "iem_cli", 84.0)
    conn.commit()
    assert conn.execute(DEDUP_SQL).fetchall() == [(84.0, "iem_cli")]
    conn.close()


def test_unknown_source_loses_to_known(db):
    conn = sqlite3.connect(db)
    _seed(conn, "some_scraper", 80.0)
    _seed(conn, "nws_cli", 84.0)
    conn.commit()
    assert conn.execute(DEDUP_SQL).fetchall() == [(84.0, "nws_cli")]
    conn.close()


def test_two_unknown_sources_deduped_deterministically(db):
    conn = sqlite3.connect(db)
    _seed(conn, "zzz_scraper", 80.0)
    _seed(conn, "aaa_scraper", 81.0)
    conn.commit()
    # Equal rank -> label_source breaks the tie; exactly one row survives.
    assert conn.execute(DEDUP_SQL).fetchall() == [(81.0, "aaa_scraper")]
    conn.close()


def test_unusable_rows_do_not_suppress_usable_ones(db):
    conn = sqlite3.connect(db)
    _seed(conn, "iem_cli", 84.0, usable=0)  # higher priority but not usable
    _seed(conn, "wunderground_api", 83.0)
    conn.commit()
    assert conn.execute(DEDUP_SQL).fetchall() == [(83.0, "wunderground_api")]
    conn.close()


def test_distinct_dates_and_metrics_kept(db):
    conn = sqlite3.connect(db)
    _seed(conn, "iem_cli", 84.0, date="2024-06-15")
    _seed(conn, "iem_cli", 70.0, date="2024-06-15", metric="low")
    _seed(conn, "iem_cli", 86.0, date="2024-06-16")
    conn.commit()
    assert len(conn.execute(DEDUP_SQL).fetchall()) == 3
    conn.close()


def test_rank_sql_rejects_injection_like_sources():
    with pytest.raises(ValueError):
        label_priority_rank_sql("l", ("ok", "bad'; DROP TABLE x --"))
