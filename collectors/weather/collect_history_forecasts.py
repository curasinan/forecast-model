"""Backfill Open-Meteo Previous Runs forecasts into forecast_history_daily."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

from src.config import settings
from weather.history import previous_runs_payload_to_daily
from weather.open_meteo import (
    DEFAULT_ENSEMBLE_MODELS,
    OpenMeteoClient,
    OpenMeteoLicenseError,
)
from weather.paths import resolve_weather_data_root
from weather.stations import load_station_registry
from weather.storage import init_weather_db

log = logging.getLogger("collect_history_forecasts")

DEFAULT_STATIONS = ("KLGA", "KORD", "KLAX")
DEFAULT_START = "2024-01-01"

UNIT_PARAM = {"F": "fahrenheit", "C": "celsius"}


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def chunk_dates(start: str, end: str, chunk_days: int = 90) -> list[tuple[str, str]]:
    """Partition [start, end] into consecutive chunks of up to chunk_days."""
    if chunk_days < 1:
        raise ValueError(f"chunk_days must be >= 1, got {chunk_days}")
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    chunks: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), e)
        chunks.append((cur.isoformat(), chunk_end.isoformat()))
        cur = chunk_end + timedelta(days=1)
    return chunks


def fetch_window(chunk_start: str, chunk_end: str) -> tuple[str, str]:
    """Expand a requested chunk by 1 UTC day on each side."""
    a = (date.fromisoformat(chunk_start) - timedelta(days=1)).isoformat()
    b = (date.fromisoformat(chunk_end) + timedelta(days=1)).isoformat()
    return a, b


def raw_cache_path(
    root: Path,
    station_icao: str,
    model: str,
    fetch_start: str,
    fetch_end: str,
    temperature_unit: str,
) -> Path:
    return (
        root
        / "raw"
        / "previous_runs"
        / f"station={station_icao}"
        / f"model={model}"
        / f"{fetch_start}_{fetch_end}_{temperature_unit}.json"
    )


def store_history_rows(db_path: Path | str, df: pd.DataFrame) -> int:
    """Batch upsert; a fuller day (higher n_hours) is never clobbered by a stub."""
    if df.empty:
        return 0
    params = [
        (
            r["station_icao"],
            r["model_name"],
            r["source_api"],
            r["forecast_init_utc"],
            int(r["lead_day"]),
            r["valid_local_date"],
            None if pd.isna(r["temp_max"]) else float(r["temp_max"]),
            None if pd.isna(r["temp_min"]) else float(r["temp_min"]),
            int(r["n_hours"]),
            r["temperature_unit"],
            r["fetched_at"],
            r["feature_schema_version"],
        )
        for _, r in df.iterrows()
    ]
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO forecast_history_daily (
                station_icao, model_name, source_api, forecast_init_utc,
                lead_day, valid_local_date, temp_max, temp_min,
                n_hours, temperature_unit, fetched_at, feature_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET
                forecast_init_utc=excluded.forecast_init_utc,
                temp_max=excluded.temp_max,
                temp_min=excluded.temp_min,
                n_hours=excluded.n_hours,
                temperature_unit=excluded.temperature_unit,
                fetched_at=excluded.fetched_at,
                feature_schema_version=excluded.feature_schema_version
            WHERE excluded.n_hours >= forecast_history_daily.n_hours
            """,
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return len(params)


def _load_or_fetch_payload(
    client: OpenMeteoClient,
    *,
    cache_path: Path,
    latitude: float,
    longitude: float,
    fetch_start: str,
    fetch_end: str,
    model: str,
    temperature_unit: str,
    refresh: bool,
    sleep_s: float,
) -> dict:
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    payload = client.fetch_previous_runs(
        latitude,
        longitude,
        start_date=fetch_start,
        end_date=fetch_end,
        model=model,
        temperature_unit=temperature_unit,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    if sleep_s > 0:
        time.sleep(sleep_s)
    return payload


def collect_station_history(
    client: OpenMeteoClient,
    station_icao: str,
    *,
    start: str,
    end: str,
    models: tuple[str, ...] = DEFAULT_ENSEMBLE_MODELS,
    chunk_days: int = 90,
    sleep_s: float = 1.0,
    refresh: bool = False,
    db_path: Path | str | None = None,
    root: Path | None = None,
) -> int:
    registry = load_station_registry()
    station = registry.require(station_icao)
    root = root or resolve_weather_data_root()
    path = init_weather_db(db_path)
    temperature_unit = UNIT_PARAM.get(station.unit_default.upper(), "fahrenheit")
    total = 0
    for model in models:
        for chunk_start, chunk_end in chunk_dates(start, end, chunk_days):
            fetch_start, fetch_end = fetch_window(chunk_start, chunk_end)
            cache_path = raw_cache_path(
                root, station.icao, model, fetch_start, fetch_end, temperature_unit
            )
            payload = _load_or_fetch_payload(
                client,
                cache_path=cache_path,
                latitude=station.lat,
                longitude=station.lon,
                fetch_start=fetch_start,
                fetch_end=fetch_end,
                model=model,
                temperature_unit=temperature_unit,
                refresh=refresh,
                sleep_s=sleep_s,
            )
            df = previous_runs_payload_to_daily(
                payload,
                station_icao=station.icao,
                model_name=model,
                timezone_name=station.timezone,
                temperature_unit=temperature_unit,
            )
            df = df[(df["valid_local_date"] >= start) & (df["valid_local_date"] <= end)]
            n = store_history_rows(path, df)
            total += n
            log.info(
                "%s %s %s..%s: %s daily rows", station.icao, model, chunk_start, chunk_end, n
            )
    return total


def log_coverage(db_path: Path | str, station_icaos: list[str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for icao in station_icaos:
            rows = conn.execute(
                """
                SELECT model_name, COUNT(*) AS n, MIN(valid_local_date), MAX(valid_local_date),
                       ROUND(AVG(n_hours), 1)
                  FROM forecast_history_daily
                 WHERE station_icao = ?
                 GROUP BY model_name
                """,
                (icao,),
            ).fetchall()
            for model, n, lo, hi, avg_h in rows:
                log.info(
                    "coverage %s %s: rows=%s dates=%s..%s avg_n_hours=%s",
                    icao, model, n, lo, hi, avg_h,
                )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Backfill Open-Meteo Previous Runs history into forecast_history_daily"
    )
    parser.add_argument("--station", action="append", help="ICAO; repeatable")
    parser.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD (default and cap: yesterday UTC)")
    parser.add_argument("--models", action="append", help="Open-Meteo model id; repeatable")
    parser.add_argument("--chunk-days", type=int, default=90)
    parser.add_argument("--sleep", type=float, default=1.0, dest="sleep_s")
    parser.add_argument("--refresh", action="store_true", help="ignore raw cache")
    parser.add_argument(
        "--allow-noncommercial",
        action="store_true",
        help="Research-only use of the free Open-Meteo API",
    )
    args = parser.parse_args(argv)

    if args.allow_noncommercial:
        settings.open_meteo_allow_noncommercial = True

    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    end = min(args.end or yesterday, yesterday)
    if args.end and args.end > yesterday:
        log.warning("--end %s clamped to yesterday (%s): today is incomplete", args.end, yesterday)
    if args.start > end:
        log.error("--start %s is after end %s", args.start, end)
        return 1

    stations = [s.upper() for s in (args.station or DEFAULT_STATIONS)]
    models = tuple(args.models) if args.models else DEFAULT_ENSEMBLE_MODELS

    try:
        client = OpenMeteoClient()
    except OpenMeteoLicenseError as exc:
        log.error("%s", exc)
        return 2

    db_path = init_weather_db()
    total = 0
    for icao in stations:
        try:
            total += collect_station_history(
                client,
                icao,
                start=args.start,
                end=end,
                models=models,
                chunk_days=args.chunk_days,
                sleep_s=args.sleep_s,
                refresh=args.refresh,
                db_path=db_path,
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            if exc.response.status_code == 400 and (
                "start_date" in body or "end_date" in body
            ):
                log.error(
                    "Previous Runs API rejected start_date/end_date: %s — "
                    "reduce the range or switch this collector to past_days windows.",
                    body,
                )
                return 3
            log.error("HTTP error for %s: %s %s", icao, exc.response.status_code, body)
            return 1
    log_coverage(db_path, stations)
    log.info("Done. total_rows=%s", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
