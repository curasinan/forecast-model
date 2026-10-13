"""IEM CLI JSON history parsing + label upsert guards. Synthetic only."""

from __future__ import annotations

import sqlite3

import pytest

from collectors.weather.collect_labels_cli import (
    CLI_HISTORY_SOURCE,
    _cli_number,
    parse_cli_records,
    upsert_cli_history_labels,
)
from weather.storage import init_weather_db

OLD_DATE = "2024-06-15"


def _record(**kw) -> dict:
    base = {
        "station": "KLGA",
        "valid": OLD_DATE,
        "high": 84,
        "low": 68,
        "link": "/api/1/nwstext/202406160619-KOKX-CDUS41-CLILGA",
    }
    base.update(kw)
    return base


# ---- value coercion ---- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        (84, 84.0),
        (84.0, 84.0),
        ("84", 84.0),
        ("-12", -12.0),
        ("M", None),      # missing
        ("T", None),      # trace (precip sentinel)
        ("", None),
        (None, None),
        ("abc", None),
        (999, None),      # out of physical range
        (-200, None),
    ],
)
def test_cli_number_coercion(raw, expected):
    assert _cli_number(raw) == expected


# ---- record parsing ---- #


def test_parse_high_and_low():
    rows = parse_cli_records([_record()], "KLGA")
    assert len(rows) == 2
    by_metric = {r["metric"]: r for r in rows}
    assert by_metric["high"]["label_value"] == 84.0
    assert by_metric["low"]["label_value"] == 68.0
    assert by_metric["high"]["valid_local_date"] == OLD_DATE
    assert by_metric["high"]["resolution_url"].startswith("https://mesonet.agron.iastate.edu/")


def test_parse_skips_missing_metrics():
    rows = parse_cli_records([_record(high="M")], "KLGA")
    assert [r["metric"] for r in rows] == ["low"]


def test_parse_rejects_other_station():
    # IEM echoing a different station must never be attributed to ours.
    assert parse_cli_records([_record(station="KJFK")], "KLGA") == []


def test_parse_rejects_malformed_date():
    assert parse_cli_records([_record(valid="2024-6")], "KLGA") == []


# ---- upsert ---- #


def _rows(db, metric="high"):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT label_value, label_source, finalized, usable_for_training "
            "FROM resolution_labels WHERE metric = ?",
            (metric,),
        ).fetchall()
    finally:
        conn.close()


def test_upsert_writes_and_is_idempotent(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    rows = parse_cli_records([_record()], "KLGA")
    assert upsert_cli_history_labels("KLGA", rows, db_path=db) == 2
    assert upsert_cli_history_labels("KLGA", rows, db_path=db) == 2
    stored = _rows(db)
    assert stored == [(84.0, CLI_HISTORY_SOURCE, 1, 1)]


def test_upsert_updates_value_in_place(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    upsert_cli_history_labels("KLGA", parse_cli_records([_record()], "KLGA"), db_path=db)
    # A corrected CLI report (they do get amended) must overwrite.
    upsert_cli_history_labels(
        "KLGA", parse_cli_records([_record(high=85)], "KLGA"), db_path=db
    )
    assert _rows(db) == [(85.0, CLI_HISTORY_SOURCE, 1, 1)]


def test_upsert_coexists_with_asos_and_other_sources(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO resolution_labels VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("KLGA", "global", OLD_DATE, "high", 83.0, "wunderground_api",
         "2024-06-20T00:00:00+00:00", 1, 1, "u"),
    )
    conn.commit()
    conn.close()
    upsert_cli_history_labels("KLGA", parse_cli_records([_record()], "KLGA"), db_path=db)
    stored = sorted(_rows(db))
    assert stored == [(83.0, "wunderground_api", 1, 1), (84.0, CLI_HISTORY_SOURCE, 1, 1)]


def test_upsert_empty_is_noop(tmp_path):
    db = init_weather_db(tmp_path / "weather.db")
    assert upsert_cli_history_labels("KLGA", [], db_path=db) == 0
