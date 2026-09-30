"""Seed weather.db from tests/fixtures/weather_markets.json (Phase-0 parser proof)."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from collectors.weather.parse_markets import (  # noqa: E402
    ParsedWeatherMarket,
    parse_weather_market,
    upsert_parsed_markets,
)
from weather.paths import resolve_weather_data_root, warn_if_onedrive  # noqa: E402
from weather.stations import load_station_registry  # noqa: E402
from weather.storage import init_weather_db  # noqa: E402


def _live_demo_markets() -> list[ParsedWeatherMarket]:
    """Near-term KLGA buckets so paper loop can join live ensemble rows."""
    target = (date.today() + timedelta(days=2)).isoformat()
    station = load_station_registry().require("KLGA")
    out = []
    for mid, lo, hi, label in (
        ("wx-live-klga-mid", 80.0, 82.0, "80-81"),
        ("wx-live-klga-high", 86.0, None, "86+"),
        ("wx-live-klga-lowbin", 70.0, 72.0, "70-71"),
    ):
        out.append(
            ParsedWeatherMarket(
                market_id=mid,
                question=f"Highest temperature in NYC on {target}?",
                platform="global",
                station_icao="KLGA",
                lat=station.lat,
                lon=station.lon,
                unit="F",
                valid_date=target,
                metric="high",
                bucket_lo=lo,
                bucket_hi=hi,
                bucket_label=label,
                resolution_url="https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
                parse_notes=["live_demo_seed"],
            )
        )
    return out


def main() -> int:
    fixtures_path = REPO / "tests" / "fixtures" / "weather_markets.json"
    fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    registry = load_station_registry()
    parsed = []
    failed = []
    for m in fixtures:
        p = parse_weather_market(m, registry)
        if p is None:
            failed.append(m["id"])
        else:
            parsed.append(p)
    parsed.extend(_live_demo_markets())
    root = resolve_weather_data_root()
    warning = warn_if_onedrive(root)
    if warning:
        print(f"WARNING: {warning}")
    db = init_weather_db()
    n = upsert_parsed_markets(parsed, db_path=db)
    print(f"data_root={root}")
    print(f"db={db}")
    print(f"seeded={n} failed={failed}")
    for p in parsed:
        line = (
            f"  {p.market_id}: {p.platform} {p.station_icao} {p.valid_date} "
            f"{p.metric} {p.unit} bucket={p.bucket_label} notes={p.parse_notes}"
        )
        print(line.encode("ascii", errors="replace").decode("ascii"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
