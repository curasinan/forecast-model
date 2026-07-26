"""Collect Open-Meteo ensemble forecasts for registered weather stations."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from weather.daily_agg import ensemble_daily_from_open_meteo, lead_hours
from weather.open_meteo import DEFAULT_ENSEMBLE_MODELS, OpenMeteoClient, OpenMeteoLicenseError
from weather.paths import resolve_weather_data_root
from weather.stations import load_station_registry
from weather.storage import (
    FEATURE_SCHEMA_VERSION,
    content_hash,
    init_weather_db,
    utc_now_iso,
    write_parquet,
)

log = logging.getLogger("collect_weather_forecasts")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def persist_payload(root: Path, station_icao: str, model: str, payload: dict) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = root / "raw" / "forecasts" / f"date={day}" / f"station={station_icao}" / f"{model}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def store_daily_rows(
    db_path: Path,
    station_icao: str,
    model_name: str,
    forecast_init_utc: str,
    daily_df: pd.DataFrame,
    timezone: str,
) -> int:
    if daily_df.empty:
        return 0
    conn = sqlite3.connect(db_path)
    n = 0
    fetched = utc_now_iso()
    try:
        for _, row in daily_df.iterrows():
            member_id = str(row.get("member_id", "control"))
            valid = str(row["valid_local_date"])
            lh = lead_hours(forecast_init_utc, valid, timezone)
            conn.execute(
                """
                INSERT INTO forecast_daily (
                    station_icao, model_name, forecast_init_utc, valid_local_date,
                    lead_hours, member_id, temp_max, temp_min,
                    ensemble_mean_max, ensemble_std_max,
                    ensemble_mean_min, ensemble_std_min,
                    fetched_at, feature_schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO UPDATE SET
                    temp_max=excluded.temp_max,
                    temp_min=excluded.temp_min,
                    ensemble_mean_max=excluded.ensemble_mean_max,
                    ensemble_std_max=excluded.ensemble_std_max,
                    ensemble_mean_min=excluded.ensemble_mean_min,
                    ensemble_std_min=excluded.ensemble_std_min,
                    lead_hours=excluded.lead_hours,
                    fetched_at=excluded.fetched_at
                """,
                (
                    station_icao,
                    model_name,
                    forecast_init_utc,
                    valid,
                    lh,
                    member_id,
                    row.get("temp_max"),
                    row.get("temp_min"),
                    row.get("ensemble_mean_max"),
                    row.get("ensemble_std_max"),
                    row.get("ensemble_mean_min"),
                    row.get("ensemble_std_min"),
                    fetched,
                    FEATURE_SCHEMA_VERSION,
                ),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def collect_station(
    client: OpenMeteoClient,
    station_icao: str,
    *,
    models: tuple[str, ...] = DEFAULT_ENSEMBLE_MODELS,
    include_ai_members: bool = False,
) -> int:
    registry = load_station_registry()
    station = registry.require(station_icao)
    root = resolve_weather_data_root()
    db_path = init_weather_db()
    model_list = list(models)
    if include_ai_members:
        # AIFS / AI-NWP members exposed by Open-Meteo Ensemble API (P7).
        for extra in ("ecmwf_aifs025",):
            if extra not in model_list:
                model_list.append(extra)

    unit = "fahrenheit" if station.unit_default.upper() == "F" else "celsius"
    # Fetch each model separately for cleaner init tagging
    total = 0
    for model in model_list:
        try:
            payload = client.fetch_ensemble(
                station.lat,
                station.lon,
                models=[model],
                temperature_unit=unit,
                timezone=station.timezone,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Forecast fetch failed %s %s: %s", station_icao, model, exc)
            continue

        forecast_init_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        # Prefer generationtime / issued if present
        if payload.get("utc_offset_seconds") is not None:
            pass
        ch = content_hash(payload)
        payload_path = persist_payload(root, station_icao, model, payload)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO forecast_runs (
                    station_icao, model_name, forecast_init_utc, fetched_at,
                    temperature_unit, payload_path, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    station_icao,
                    model,
                    forecast_init_utc,
                    utc_now_iso(),
                    unit,
                    str(payload_path),
                    ch,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        daily = ensemble_daily_from_open_meteo(payload, station.timezone, unit)
        if daily.empty:
            log.warning("No daily rows for %s %s", station_icao, model)
            continue
        daily = daily.assign(
            station_icao=station_icao,
            model_name=model,
            forecast_init_utc=forecast_init_utc,
        )
        pq = root / "forecasts" / f"station={station_icao}" / f"model={model}" / f"{ch}.parquet"
        write_parquet(daily, pq)
        total += store_daily_rows(
            db_path, station_icao, model, forecast_init_utc, daily, station.timezone
        )
        log.info("Stored %s daily rows for %s/%s", len(daily), station_icao, model)
    return total


def collect_from_weather_markets(*, include_ai_members: bool = False) -> int:
    db_path = init_weather_db()
    conn = sqlite3.connect(db_path)
    try:
        icaos = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT station_icao FROM weather_markets"
            ).fetchall()
        ]
    finally:
        conn.close()
    if not icaos:
        # Fallback: all registry stations
        icaos = [s.icao for s in load_station_registry().all()]
    client = OpenMeteoClient(for_trading=True)
    total = 0
    for icao in icaos:
        total += collect_station(client, icao, include_ai_members=include_ai_members)
    return total


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Collect Open-Meteo ensemble forecasts")
    parser.add_argument("--station", action="append", help="ICAO code (repeatable)")
    parser.add_argument("--include-ai", action="store_true", help="Also pull AIFS members")
    parser.add_argument(
        "--allow-noncommercial",
        action="store_true",
        help="Research-only: allow public Open-Meteo host",
    )
    args = parser.parse_args(argv)
    if args.allow_noncommercial:
        from src.config import settings

        settings.open_meteo_allow_noncommercial = True
    try:
        if args.station:
            client = OpenMeteoClient(for_trading=True)
            n = 0
            for s in args.station:
                n += collect_station(client, s.upper(), include_ai_members=args.include_ai)
        else:
            n = collect_from_weather_markets(include_ai_members=args.include_ai)
    except OpenMeteoLicenseError as exc:
        log.error("%s", exc)
        return 2
    log.info("Done. rows_written~=%s", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
