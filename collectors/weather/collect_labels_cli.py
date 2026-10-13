"""Collect NWS CLI daily climate labels for Polymarket US weather markets."""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from weather.finalization import MIN_FINALIZATION_HOURS, usable_for_training
from weather.storage import init_weather_db, utc_now_iso

log = logging.getLogger("collect_labels_cli")

__all__ = ["MIN_FINALIZATION_HOURS", "usable_for_training"]

# Map ICAO → CLI product id (from Polymarket US Weather FAQs).
CLI_PRODUCTS = {
    "KNYC": "NYC",
    "KSFO": "SFO",
    "KMIA": "MIA",
    "KMDW": "MDW",
    "KLAX": "LAX",
}

MAX_RE = re.compile(r"MAXIMUM\s+(-?\d{1,3})", re.I)
MIN_RE = re.compile(r"MINIMUM\s+(-?\d{1,3})", re.I)

IEM_CLI_JSON = "https://mesonet.agron.iastate.edu/json/cli.py"
IEM_TEXT_BASE = "https://mesonet.agron.iastate.edu"
USER_AGENT = "polymarket-trader/0.1 (+collectors.weather.collect_labels_cli)"
CLI_HISTORY_SOURCE = "iem_cli"
CLI_MIN_F, CLI_MAX_F = -80.0, 130.0


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def cli_url(product: str) -> str:
    # Latest CLI text product
    return f"https://forecast.weather.gov/product.php?site=NWS&issuedby={product}&product=CLI&format=txt&version=1&glossary=0"


def parse_cli_text(text: str, metric: str = "high") -> float | None:
    m = MIN_RE.search(text) if metric == "low" else MAX_RE.search(text)
    return float(m.group(1)) if m else None


def upsert_label(
    *,
    station_icao: str,
    valid_local_date: str,
    metric: str,
    label_value: float | None,
    resolution_url: str,
    finalized: bool,
    db_path: Path | str | None = None,
) -> None:
    path = init_weather_db(db_path)
    usable = 1 if usable_for_training(valid_local_date, finalized) and label_value is not None else 0
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO resolution_labels (
                station_icao, platform, valid_local_date, metric,
                label_value, label_source, label_fetched_at,
                finalized, usable_for_training, resolution_url
            ) VALUES (?, 'us', ?, ?, ?, 'nws_cli', ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET
                label_value=excluded.label_value,
                label_fetched_at=excluded.label_fetched_at,
                finalized=excluded.finalized,
                usable_for_training=excluded.usable_for_training,
                resolution_url=excluded.resolution_url
            WHERE excluded.label_value IS NOT NULL
               OR resolution_labels.label_value IS NULL
            """,
            (
                station_icao,
                valid_local_date,
                metric,
                label_value,
                utc_now_iso(),
                1 if finalized else 0,
                usable,
                resolution_url,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_cli(station_icao: str, metric: str = "high", timeout: float = 30.0) -> tuple[float | None, str]:
    product = CLI_PRODUCTS.get(station_icao.upper())
    if not product:
        raise KeyError(f"No CLI product mapping for {station_icao}")
    url = cli_url(product)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return parse_cli_text(resp.text, metric=metric), url


def collect_station(station_icao: str, valid_local_date: str, metric: str = "high") -> dict:
    finalized = date.fromisoformat(valid_local_date) < date.today()
    label_value = None
    url = cli_url(CLI_PRODUCTS.get(station_icao.upper(), "NYC"))
    if finalized and station_icao.upper() in CLI_PRODUCTS:
        try:
            label_value, url = fetch_cli(station_icao, metric=metric)
        except Exception as exc:  # noqa: BLE001
            log.warning("CLI fetch failed %s: %s", station_icao, exc)
    upsert_label(
        station_icao=station_icao.upper(),
        valid_local_date=valid_local_date,
        metric=metric,
        label_value=label_value,
        resolution_url=url,
        finalized=finalized,
    )
    return {
        "station_icao": station_icao.upper(),
        "platform": "us",
        "valid_local_date": valid_local_date,
        "metric": metric,
        "label_value": label_value,
        "finalized": finalized,
        "usable_for_training": usable_for_training(valid_local_date, finalized)
        and label_value is not None,
        "resolution_url": url,
    }


# ---- Historical backfill via the IEM CLI JSON archive ---- #


def _cli_number(value: Any) -> float | None:
    """CLI JSON uses 'M' (missing) / 'T' (trace) sentinels alongside numbers."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        text = str(value).strip()
        if not text or text.upper() in {"M", "T"}:
            return None
        try:
            out = float(text)
        except ValueError:
            return None
    if not CLI_MIN_F <= out <= CLI_MAX_F:
        return None
    return out


def fetch_cli_year(station_icao: str, year: int, timeout: float = 60.0) -> list[dict[str, Any]]:
    """One request per station-year; returns raw IEM CLI result records."""
    with httpx.Client(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        resp = client.get(
            IEM_CLI_JSON, params={"station": station_icao.upper(), "year": year}
        )
        resp.raise_for_status()
        payload = resp.json()
    return list(payload.get("results") or [])


def parse_cli_records(
    results: list[dict[str, Any]], station_icao: str
) -> list[dict[str, Any]]:
    """Flatten CLI records to (valid_local_date, metric, value, url) rows."""
    want = station_icao.upper()
    rows: list[dict[str, Any]] = []
    for rec in results:
        if str(rec.get("station", "")).upper() != want:
            continue
        valid = str(rec.get("valid") or "")
        if len(valid) != 10:
            continue
        link = rec.get("link") or ""
        url = f"{IEM_TEXT_BASE}{link}" if link.startswith("/") else str(link)
        for metric, key in (("high", "high"), ("low", "low")):
            value = _cli_number(rec.get(key))
            if value is None:
                continue
            rows.append(
                {
                    "valid_local_date": valid,
                    "metric": metric,
                    "label_value": value,
                    "resolution_url": url,
                }
            )
    return rows


def upsert_cli_history_labels(
    station_icao: str,
    rows: list[dict[str, Any]],
    *,
    db_path: Path | str | None = None,
    label_source: str = CLI_HISTORY_SOURCE,
) -> int:
    """Batch upsert CLI history labels; never regresses a stored value to NULL."""
    if not rows:
        return 0
    path = init_weather_db(db_path)
    fetched = utc_now_iso()
    params = [
        (
            station_icao.upper(),
            r["valid_local_date"],
            r["metric"],
            float(r["label_value"]),
            label_source,
            fetched,
            1,
            1 if usable_for_training(r["valid_local_date"], True) else 0,
            r["resolution_url"],
        )
        for r in rows
    ]
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            """
            INSERT INTO resolution_labels (
                station_icao, platform, valid_local_date, metric,
                label_value, label_source, label_fetched_at,
                finalized, usable_for_training, resolution_url
            ) VALUES (?, 'us', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET
                label_value=excluded.label_value,
                label_fetched_at=excluded.label_fetched_at,
                finalized=excluded.finalized,
                usable_for_training=excluded.usable_for_training,
                resolution_url=excluded.resolution_url
            WHERE excluded.label_value IS NOT NULL
               OR resolution_labels.label_value IS NULL
            """,
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return len(params)


def backfill_station_years(
    station_icao: str,
    start_year: int,
    end_year: int,
    *,
    db_path: Path | str | None = None,
    sleep_s: float = 1.0,
) -> int:
    """Backfill CLI labels for [start_year, end_year]; returns rows written."""
    total = 0
    for i, year in enumerate(range(start_year, end_year + 1)):
        if i and sleep_s > 0:
            time.sleep(sleep_s)
        try:
            results = fetch_cli_year(station_icao, year)
        except Exception as exc:  # noqa: BLE001
            log.warning("CLI history fetch failed %s %s: %s", station_icao, year, exc)
            continue
        rows = parse_cli_records(results, station_icao)
        n = upsert_cli_history_labels(station_icao, rows, db_path=db_path)
        total += n
        log.info("CLI history %s %s: %s labels from %s reports", station_icao, year, n, len(results))
    return total


def collect_pending_from_db() -> list[dict]:
    path = init_weather_db()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT station_icao, valid_local_date, metric
              FROM weather_markets
             WHERE platform = 'us'
            """
        ).fetchall()
    finally:
        conn.close()
    return [collect_station(i, d, m or "high") for i, d, m in rows]


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Collect NWS CLI resolution labels")
    parser.add_argument("--station", action="append")
    parser.add_argument("--date")
    parser.add_argument("--metric", default="high", choices=["high", "low"])
    parser.add_argument(
        "--history",
        action="store_true",
        help="Backfill historical labels from the IEM CLI JSON archive",
    )
    parser.add_argument("--start-year", type=int, default=2024)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--sleep", type=float, default=1.0, dest="sleep_s")
    args = parser.parse_args(argv)

    stations = [s.upper() for s in (args.station or [])]
    if args.history:
        if not stations:
            parser.error("--history requires at least one --station")
        if args.start_year > args.end_year:
            parser.error("--start-year must be <= --end-year")
        total = 0
        for icao in stations:
            total += backfill_station_years(
                icao, args.start_year, args.end_year, sleep_s=args.sleep_s
            )
        log.info("Done. total_labels=%s", total)
        return 0

    if stations and args.date:
        for icao in stations:
            print(collect_station(icao, args.date, args.metric))
    else:
        for r in collect_pending_from_db():
            log.info("%s", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
