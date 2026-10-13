"""One weather collection cycle: discover live markets → forecasts → obs."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from collectors.weather.collect_forecasts import collect_station  # noqa: E402
from collectors.weather.collect_obs_iem import collect_station as collect_obs  # noqa: E402
from collectors.weather.discover_live_markets import discover_and_upsert  # noqa: E402
from src.config import settings  # noqa: E402
from weather.open_meteo import OpenMeteoClient, OpenMeteoLicenseError  # noqa: E402
from weather.storage import init_weather_db  # noqa: E402

log = logging.getLogger("weather_collect_cycle")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def stations_from_weather_db() -> list[str]:
    db = init_weather_db()
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT DISTINCT station_icao FROM weather_markets ORDER BY station_icao"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def main(argv: list[str] | None = None) -> int:
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("scripts/run_weather_collect_cycle.py")
    setup_logging()
    parser = argparse.ArgumentParser(description="Weather collect cycle")
    parser.add_argument(
        "--allow-noncommercial",
        action="store_true",
        help="Research-only Open-Meteo public host",
    )
    parser.add_argument("--skip-discover", action="store_true")
    parser.add_argument("--skip-refresh", action="store_true")
    parser.add_argument("--skip-forecasts", action="store_true")
    parser.add_argument("--skip-obs", action="store_true")
    parser.add_argument("--skip-gamma", action="store_true")
    parser.add_argument("--include-ai", action="store_true")
    parser.add_argument("--obs-hours", type=int, default=48)
    parser.add_argument("--refresh-timeout", type=float, default=12.0)
    args = parser.parse_args(argv)

    if args.allow_noncommercial:
        settings.open_meteo_allow_noncommercial = True

    if not args.skip_refresh and not args.skip_discover:
        try:
            from collectors.weather.refresh_markets import (
                fetch_weather_markets,
                upsert_gamma_markets,
            )

            live = fetch_weather_markets(timeout=args.refresh_timeout)
            if live:
                n = upsert_gamma_markets(live)
                log.info("refreshed %s weather markets into polymarket.db", n)
            else:
                log.warning("weather market refresh returned 0; using local DB only")
        except Exception as exc:  # noqa: BLE001
            log.warning("weather market refresh failed: %s", exc)

    if not args.skip_discover:
        summary = discover_and_upsert(skip_gamma=args.skip_gamma)
        log.info("discover: %s", summary)

    stations = stations_from_weather_db()
    if not stations:
        log.warning("No stations in weather_markets; nothing to forecast/obs")
        return 0

    if not args.skip_forecasts:
        try:
            client = OpenMeteoClient(for_trading=True)
        except OpenMeteoLicenseError as exc:
            log.error("%s", exc)
            return 2
        total = 0
        for icao in stations:
            try:
                total += collect_station(
                    client, icao, include_ai_members=args.include_ai
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("forecast %s failed: %s", icao, exc)
        log.info("forecast rows~=%s stations=%s", total, len(stations))

    if not args.skip_obs:
        for icao in stations:
            if not str(icao).upper().startswith("K"):
                # IEM US ASOS codes; skip non-US ICAOs for now
                continue
            try:
                collect_obs(icao, hours=args.obs_hours)
            except Exception as exc:  # noqa: BLE001
                log.warning("obs %s failed: %s", icao, exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
