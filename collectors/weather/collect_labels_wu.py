"""Fetch Wunderground / weather.com daily high/low labels for Polymarket Global."""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx

from weather.finalization import (
    MIN_FINALIZATION_HOURS,
    is_finalized_conservative_utc,
    usable_for_training,
)
from weather.storage import init_weather_db, utc_now_iso

log = logging.getLogger("collect_labels_wu")

__all__ = ["MIN_FINALIZATION_HOURS", "is_finalized", "usable_for_training"]

# Public key embedded in WU frontend JS (fallback if page scrape fails).
WU_PUBLIC_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
API_KEY_RE = re.compile(r"apiKey=([a-f0-9]{32})", re.IGNORECASE)

MAX_TEMP_RE = re.compile(
    r"(?:High|Max(?:imum)?)\s*(?:Temp(?:erature)?)?\s*[:=]?\s*(-?\d{1,3})\s*°?\s*F\b",
    re.IGNORECASE,
)
MIN_TEMP_RE = re.compile(
    r"(?:Low|Min(?:imum)?)\s*(?:Temp(?:erature)?)?\s*[:=]?\s*(-?\d{1,3})\s*°?\s*F\b",
    re.IGNORECASE,
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def default_wu_url(icao: str, valid_local_date: str) -> str:
    """Best-effort history URL; prefer market resolution_url when present."""
    y, m, d = valid_local_date.split("-")
    return f"https://www.wunderground.com/history/daily/{icao}/date/{y}-{m}-{d}"


def extract_api_key(html: str | None) -> str:
    if html:
        m = API_KEY_RE.search(html)
        if m:
            return m.group(1)
    return WU_PUBLIC_API_KEY


def resolve_location_id(icao: str, api_key: str, timeout: float = 30.0) -> str:
    """Map ICAO → weather.com v1 location id (e.g. KLGA:9:US)."""
    url = "https://api.weather.com/v3/location/point"
    params = {
        "apiKey": api_key,
        "language": "en-US",
        "icaoCode": icao.upper(),
        "format": "json",
    }
    country = "US"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(
                url, params=params, headers={"User-Agent": "weather-research-bot/0.1"}
            )
            resp.raise_for_status()
            loc = resp.json().get("location") or {}
            country = (loc.get("countryCode") or country).upper()
    except Exception as exc:  # noqa: BLE001
        log.warning("location.point failed for %s: %s; defaulting country=US", icao, exc)
    return f"{icao.upper()}:9:{country}"


def fetch_historical_observations(
    location_id: str,
    valid_local_date: str,
    api_key: str,
    *,
    units: str = "e",
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    ymd = valid_local_date.replace("-", "")
    url = f"https://api.weather.com/v1/location/{location_id}/observations/historical.json"
    params = {
        "apiKey": api_key,
        "units": units,
        "startDate": ymd,
        "endDate": ymd,
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, params=params, headers={"User-Agent": "weather-research-bot/0.1"})
        resp.raise_for_status()
        payload = resp.json()
    return list(payload.get("observations") or [])


def daily_extreme_from_observations(
    observations: list[dict[str, Any]],
    metric: str = "high",
) -> float | None:
    """Daily high/low from hourly temps (matches WU daily table closely)."""
    temps = [float(o["temp"]) for o in observations if o.get("temp") is not None]
    if not temps:
        return None
    return max(temps) if metric == "high" else min(temps)


def sanity_check_temp(value: float | None, *, unit: str = "F", metric: str = "high") -> bool:
    """Reject physically impossible temperatures."""
    if value is None:
        return False
    if unit.upper() == "C":
        return -90.0 <= value <= 60.0
    return -80.0 <= value <= 130.0


def parse_wu_html(html: str, metric: str = "high") -> float | None:
    """Strict HTML fallback — requires °F context. Prefer API path."""
    regex = MIN_TEMP_RE if metric == "low" else MAX_TEMP_RE
    m = regex.search(html or "")
    if not m:
        return None
    return float(m.group(1))


# Back-compat alias — logic now lives in weather.finalization.
is_finalized = is_finalized_conservative_utc


def upsert_label(
    *,
    station_icao: str,
    valid_local_date: str,
    metric: str,
    label_value: float | None,
    resolution_url: str,
    finalized: bool,
    label_source: str = "wunderground_api",
    db_path: Path | str | None = None,
) -> None:
    path = init_weather_db(db_path)
    ok = sanity_check_temp(label_value, metric=metric)
    usable = (
        1
        if usable_for_training(valid_local_date, finalized) and label_value is not None and ok
        else 0
    )
    if label_value is not None and not ok:
        log.warning(
            "Rejecting implausible label %s for %s %s %s",
            label_value,
            station_icao,
            valid_local_date,
            metric,
        )
        label_value = None
        usable = 0
    conn = sqlite3.connect(path)
    try:
        # A failed fetch (label_value=None) must never clobber stored truth.
        if label_value is None:
            existing = conn.execute(
                """
                SELECT 1 FROM resolution_labels
                 WHERE station_icao = ? AND platform = 'global'
                   AND valid_local_date = ? AND metric = ?
                   AND label_value IS NOT NULL
                """,
                (station_icao, valid_local_date, metric),
            ).fetchone()
            if existing:
                log.info(
                    "Keeping stored label for %s %s %s; fetch returned none",
                    station_icao, valid_local_date, metric,
                )
                return
        conn.execute(
            """
            DELETE FROM resolution_labels
             WHERE station_icao = ? AND platform = 'global'
               AND valid_local_date = ? AND metric = ?
            """,
            (station_icao, valid_local_date, metric),
        )
        conn.execute(
            """
            INSERT INTO resolution_labels (
                station_icao, platform, valid_local_date, metric,
                label_value, label_source, label_fetched_at,
                finalized, usable_for_training, resolution_url
            ) VALUES (?, 'global', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                station_icao,
                valid_local_date,
                metric,
                label_value,
                label_source,
                utc_now_iso(),
                1 if finalized else 0,
                usable,
                resolution_url,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_label_value(
    station_icao: str,
    valid_local_date: str,
    *,
    metric: str = "high",
    timeout: float = 30.0,
) -> tuple[float | None, str]:
    """Return (value, source). Prefer weather.com historical API."""
    page_url = default_wu_url(station_icao, valid_local_date)
    html = None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(page_url, headers={"User-Agent": "weather-research-bot/0.1"})
            if resp.status_code == 200:
                html = resp.text
    except Exception as exc:  # noqa: BLE001
        log.warning("WU page fetch failed: %s", exc)

    api_key = extract_api_key(html)
    try:
        loc_id = resolve_location_id(station_icao, api_key, timeout=timeout)
        obs = fetch_historical_observations(
            loc_id, valid_local_date, api_key, timeout=timeout
        )
        value = daily_extreme_from_observations(obs, metric=metric)
        if value is not None and sanity_check_temp(value, metric=metric):
            return value, "wunderground_api"
    except Exception as exc:  # noqa: BLE001
        log.warning("WU API fetch failed %s %s: %s", station_icao, valid_local_date, exc)

    if html:
        value = parse_wu_html(html, metric=metric)
        if value is not None and sanity_check_temp(value, metric=metric):
            return value, "wunderground_html"
    return None, "wunderground_failed"


def fetch_and_store(
    station_icao: str,
    valid_local_date: str,
    *,
    metric: str = "high",
    resolution_url: str | None = None,
    timeout: float = 30.0,
) -> dict:
    url = resolution_url or default_wu_url(station_icao, valid_local_date)
    finalized = is_finalized(valid_local_date)
    label_value = None
    source = "wunderground_pending"
    if finalized:
        label_value, source = fetch_label_value(
            station_icao, valid_local_date, metric=metric, timeout=timeout
        )
    upsert_label(
        station_icao=station_icao,
        valid_local_date=valid_local_date,
        metric=metric,
        label_value=label_value,
        resolution_url=url,
        finalized=finalized,
        label_source=source,
    )
    return {
        "station_icao": station_icao,
        "valid_local_date": valid_local_date,
        "metric": metric,
        "label_value": label_value,
        "label_source": source,
        "finalized": finalized,
        "usable_for_training": usable_for_training(valid_local_date, finalized)
        and label_value is not None
        and sanity_check_temp(label_value, metric=metric),
        "resolution_url": url,
    }


def collect_pending_from_db() -> list[dict]:
    path = init_weather_db()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT station_icao, valid_local_date, metric, resolution_url
              FROM weather_markets
             WHERE platform = 'global'
            """
        ).fetchall()
    finally:
        conn.close()
    results = []
    for icao, valid, metric, url in rows:
        results.append(
            fetch_and_store(icao, valid, metric=metric or "high", resolution_url=url)
        )
    return results


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Collect Wunderground resolution labels")
    parser.add_argument("--station")
    parser.add_argument("--date", help="YYYY-MM-DD")
    parser.add_argument("--metric", default="high", choices=["high", "low"])
    parser.add_argument("--url")
    args = parser.parse_args(argv)
    if args.station and args.date:
        print(fetch_and_store(args.station.upper(), args.date, metric=args.metric, resolution_url=args.url))
    else:
        for r in collect_pending_from_db():
            log.info("%s", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
