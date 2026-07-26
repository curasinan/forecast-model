"""Phase-0 parser proof: ≥20 real-shaped weather market fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collectors.weather.parse_markets import (
    is_weather_question,
    parse_bucket_bounds,
    parse_weather_market,
    upsert_parsed_markets,
)
from weather.stations import load_station_registry
from weather.storage import init_weather_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "weather_markets.json"


@pytest.fixture(scope="module")
def fixtures() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def test_fixture_count_at_least_20(fixtures):
    assert len(fixtures) >= 20


def test_parse_all_fixtures(fixtures):
    registry = load_station_registry()
    parsed = []
    for m in fixtures:
        p = parse_weather_market(m, registry)
        assert p is not None, f"failed to parse {m['id']}: {m['question']}"
        assert p.station_icao
        assert p.valid_date
        assert p.platform in ("global", "us")
        assert p.unit in ("F", "C")
        assert p.metric in ("high", "low")
        parsed.append(p)

    assert len(parsed) >= 20
    # Platforms both present
    platforms = {p.platform for p in parsed}
    assert "global" in platforms
    assert "us" in platforms
    # Known stations
    assert any(p.station_icao == "KLGA" for p in parsed)
    assert any(p.station_icao == "KORD" for p in parsed)
    assert any(p.station_icao == "KNYC" for p in parsed)  # US Central Park
    assert any(p.station_icao == "KMDW" for p in parsed)


def test_us_vs_global_resolution(fixtures):
    registry = load_station_registry()
    by_id = {m["id"]: parse_weather_market(m, registry) for m in fixtures}
    assert by_id["wx-nyc-1"].platform == "global"
    assert by_id["wx-nyc-1"].resolution_url and "wunderground" in by_id["wx-nyc-1"].resolution_url
    assert by_id["wx-us-nyc-1"].platform == "us"
    assert by_id["wx-us-nyc-1"].station_icao == "KNYC"


def test_bucket_bounds():
    lo, hi, label = parse_bucket_bounds("x", "84-85°F")
    assert lo == 84.0 and hi == 86.0
    lo, hi, label = parse_bucket_bounds("x", "86°F or higher")
    assert lo == 86.0 and hi is None
    lo, hi, label = parse_bucket_bounds("x", "20°F or lower")
    assert lo is None and hi == 21.0
    # ISO dates must not become buckets
    lo, hi, label = parse_bucket_bounds(
        "Will high be 80°F or above on 2026-07-10?", "Yes"
    )
    assert lo == 80.0 and hi is None


def test_upsert_parsed(tmp_path, fixtures):
    db = tmp_path / "weather.db"
    init_weather_db(db)
    registry = load_station_registry()
    parsed = [parse_weather_market(m, registry) for m in fixtures]
    parsed = [p for p in parsed if p]
    n = upsert_parsed_markets(parsed, db_path=db)
    assert n == len(parsed)


def test_non_weather_rejected():
    assert parse_weather_market({"id": "1", "question": "Will Trump win?", "description": ""}) is None
    assert is_weather_question("Highest temperature in NYC") is True


def test_parse_date_without_year_uses_end_date():
    from collectors.weather.parse_markets import parse_valid_date

    assert (
        parse_valid_date(
            "Will the highest temperature in London be 26°C on July 22?",
            "",
            end_date="2026-07-22T12:00:00Z",
        )
        == "2026-07-22"
    )


def test_parse_live_city_question_no_description():
    registry = load_station_registry()
    p = parse_weather_market(
        {
            "id": "2996928",
            "question": "Will the lowest temperature in London be 13°C on July 22?",
            "description": "",
            "endDate": "2026-07-22T12:00:00Z",
            "clobTokenIds": ["123"],
        },
        registry,
    )
    assert p is not None
    assert p.station_icao == "EGLC"
    assert p.valid_date == "2026-07-22"
    assert p.metric == "low"
    assert p.bucket_lo == 13.0


def test_global_climate_rejected():
    assert (
        parse_weather_market(
            {
                "id": "2480292",
                "question": "Will global temperature increase by less than 1.10°C in July 2026?",
                "description": "",
                "endDate": "2026-08-01T03:59:00Z",
            }
        )
        is None
    )
