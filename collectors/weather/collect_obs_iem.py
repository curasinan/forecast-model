"""Collect ASOS/METAR observations from Iowa Environmental Mesonet."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd

from weather.obs_daily import (
    DEFAULT_MIN_HOURS_COVERED,
    finalize_obs_daily,
    observations_to_daily,
    upsert_obs_daily,
)
from weather.paths import resolve_weather_data_root
from weather.stations import load_station_registry
from weather.storage import init_weather_db, utc_now_iso, write_parquet

log = logging.getLogger("collect_obs_iem")

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
USER_AGENT = "polymarket-trader/0.1 (+collectors.weather.collect_obs_iem)"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def icao_to_iem_station(icao: str) -> str:
    """IEM typically wants the 3-letter US station code (drop leading K)."""
    icao = icao.upper()
    if len(icao) == 4 and icao.startswith("K"):
        return icao[1:]
    return icao


def fetch_iem_asos(
    icao: str,
    *,
    start: datetime,
    end: datetime,
    timeout: float = 60.0,
) -> pd.DataFrame:
    station = icao_to_iem_station(icao)
    params = {
        "station": station,
        "data": "tmpf",
        "tz": "UTC",
        "format": "comma",
        "latlon": "no",
        "elev": "no",
        "year1": start.year,
        "month1": start.month,
        "day1": start.day,
        "hour1": start.hour,
        "minute1": start.minute,
        "year2": end.year,
        "month2": end.month,
        "day2": end.day,
        "hour2": end.hour,
        "minute2": end.minute,
    }
    with httpx.Client(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        resp = client.get(IEM_URL, params=params)
        resp.raise_for_status()
        text = resp.text
    # Skip comment lines starting with #
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(lines) < 2:
        return pd.DataFrame(columns=["station_icao", "valid_utc", "temp_f", "temp_c"])
    df = pd.read_csv(StringIO("\n".join(lines)))
    # Columns typically: station, valid, tmpf
    if "valid" not in df.columns:
        return pd.DataFrame(columns=["station_icao", "valid_utc", "temp_f", "temp_c"])
    out = pd.DataFrame(
        {
            "station_icao": icao.upper(),
            "valid_utc": pd.to_datetime(df["valid"], utc=True).dt.strftime(
                "%Y-%m-%dT%H:%M:%S%z"
            ),
            "temp_f": pd.to_numeric(df.get("tmpf"), errors="coerce"),
        }
    )
    out["temp_c"] = (out["temp_f"] - 32.0) * 5.0 / 9.0
    out = out.dropna(subset=["temp_f"])
    return out


def store_observations(
    df: pd.DataFrame, db_path: Path | None = None, parquet_name: str | None = None
) -> int:
    if df.empty:
        return 0
    path = init_weather_db(db_path)
    fetched = utc_now_iso()
    params = [
        (
            row["station_icao"],
            row["valid_utc"],
            float(row["temp_f"]),
            float(row["temp_c"]) if pd.notna(row["temp_c"]) else None,
            "iem_asos",
            fetched,
        )
        for _, row in df.iterrows()
    ]
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            """
            INSERT INTO observations (
                station_icao, valid_utc, temp_f, temp_c, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET
                temp_f=excluded.temp_f,
                temp_c=excluded.temp_c,
                fetched_at=excluded.fetched_at
            """,
            params,
        )
        conn.commit()
    finally:
        conn.close()
    root = resolve_weather_data_root()
    icao = str(df["station_icao"].iloc[0])
    name = parquet_name or datetime.now(timezone.utc).strftime("%Y%m%d")
    write_parquet(df, root / "obs" / f"station={icao}" / f"{name}.parquet")
    return len(params)


def collect_station(icao: str, hours: int = 48) -> int:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    df = fetch_iem_asos(icao, start=start, end=end)
    n = store_observations(df)
    log.info("IEM %s: stored %s rows", icao, n)
    return n


def backfill_station(
    icao: str,
    start: str,
    end: str,
    *,
    chunk_days: int = 366,
    sleep_s: float = 2.0,
    db_path: Path | None = None,
) -> int:
    """Historical backfill over [start, end] local dates (YYYY-MM-DD).

    The fetch window is extended by one UTC day on each side so boundary
    local days aggregate completely regardless of station timezone.
    """
    fetch_start = date.fromisoformat(start) - timedelta(days=1)
    fetch_end = date.fromisoformat(end) + timedelta(days=2)  # exclusive-ish end of day
    total = 0
    cur = fetch_start
    first = True
    while cur < fetch_end:
        chunk_end = min(cur + timedelta(days=chunk_days), fetch_end)
        if not first and sleep_s > 0:
            time.sleep(sleep_s)
        first = False
        df = fetch_iem_asos(
            icao,
            start=datetime(cur.year, cur.month, cur.day, tzinfo=timezone.utc),
            end=datetime(chunk_end.year, chunk_end.month, chunk_end.day, tzinfo=timezone.utc),
        )
        n = store_observations(
            df, db_path=db_path, parquet_name=f"{cur.isoformat()}_{chunk_end.isoformat()}"
        )
        total += n
        log.info("IEM backfill %s %s..%s: %s rows", icao, cur, chunk_end, n)
        cur = chunk_end
    return total


def aggregate_station_daily(
    icao: str,
    start: str,
    end: str,
    *,
    db_path: Path | None = None,
    min_hours: int = DEFAULT_MIN_HOURS_COVERED,
    now: datetime | None = None,
) -> int:
    """Aggregate stored obs into obs_daily for local dates [start, end].

    Reads back from SQLite (not the fetched frame) so re-runs heal days that
    were partial when first aggregated.
    """
    station = load_station_registry().require(icao)
    fetch_start = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    fetch_end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    path = init_weather_db(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT station_icao, valid_utc, temp_f, temp_c
              FROM observations
             WHERE station_icao = ? AND source = 'iem_asos'
               AND substr(valid_utc, 1, 10) BETWEEN ? AND ?
            """,
            (icao.upper(), fetch_start, fetch_end),
        ).fetchall()
    finally:
        conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    daily = observations_to_daily(df, timezone_name=station.timezone)
    if daily.empty:
        log.warning("No observations to aggregate for %s %s..%s", icao, start, end)
        return 0
    daily = daily[(daily["valid_local_date"] >= start) & (daily["valid_local_date"] <= end)]
    daily = finalize_obs_daily(daily, timezone_name=station.timezone, min_hours=min_hours, now=now)
    n = upsert_obs_daily(path, station.icao, daily)
    n_usable = int(daily["usable_for_training"].sum())
    log.info("obs_daily %s %s..%s: %s rows (%s usable)", icao, start, end, n, n_usable)
    return n


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Collect IEM ASOS observations")
    parser.add_argument("--station", action="append", required=False)
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--start", help="YYYY-MM-DD; historical backfill mode (needs --end)")
    parser.add_argument("--end", help="YYYY-MM-DD; historical backfill mode (needs --start)")
    parser.add_argument(
        "--aggregate-daily",
        action="store_true",
        help="Aggregate stored obs into obs_daily for the covered local dates",
    )
    parser.add_argument("--sleep", type=float, default=2.0, dest="sleep_s")
    args = parser.parse_args(argv)

    backfill_mode = bool(args.start or args.end)
    if backfill_mode and not (args.start and args.end):
        parser.error("--start and --end must be given together")
    if backfill_mode and args.start > args.end:
        parser.error("--start must be <= --end")

    stations = [s.upper() for s in (args.station or [])]
    if not stations:
        db = init_weather_db()
        conn = sqlite3.connect(db)
        try:
            stations = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT station_icao FROM weather_markets"
                ).fetchall()
            ]
        finally:
            conn.close()
        if not stations:
            stations = [s.icao for s in load_station_registry().all() if s.icao.startswith("K")]
    total = 0
    for s in stations:
        try:
            if backfill_mode:
                total += backfill_station(s, args.start, args.end, sleep_s=args.sleep_s)
            else:
                total += collect_station(s, hours=args.hours)
            if args.aggregate_daily:
                if backfill_mode:
                    agg_start, agg_end = args.start, args.end
                else:
                    now = datetime.now(timezone.utc)
                    agg_start = (now - timedelta(hours=args.hours)).date().isoformat()
                    agg_end = now.date().isoformat()
                aggregate_station_daily(s, agg_start, agg_end)
        except Exception as exc:  # noqa: BLE001
            log.warning("IEM collect failed for %s: %s", s, exc)
    log.info("Done. total_rows=%s", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
