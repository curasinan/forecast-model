"""Parse Open-Meteo Previous Runs payloads into lead-structured daily rows."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from weather.daily_agg import hourly_to_daily_max_min
from weather.storage import FEATURE_SCHEMA_VERSION, utc_now_iso

PREVIOUS_DAY_MIN = 1
PREVIOUS_DAY_MAX = 7

HISTORY_COLUMNS = [
    "station_icao",
    "model_name",
    "source_api",
    "forecast_init_utc",
    "lead_day",
    "valid_local_date",
    "temp_max",
    "temp_min",
    "n_hours",
    "temperature_unit",
    "fetched_at",
    "feature_schema_version",
]


def effective_init_utc(valid_local_date: str, previous_day_n: int, timezone_name: str) -> str:
    """Conservative (latest-possible) init embedded in previous_dayN for day D."""
    d = date.fromisoformat(valid_local_date)
    tz = ZoneInfo(timezone_name)
    next_day_start = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1)
    init = next_day_start.astimezone(timezone.utc) - timedelta(hours=24 * previous_day_n)
    return init.isoformat()


def _non_null_counts_by_local_date(
    times: list[str], values: list[float | None], timezone_name: str
) -> dict[str, int]:
    tz = ZoneInfo(timezone_name)
    counts: dict[str, int] = {}
    for t, v in zip(times, values):
        if v is None:
            continue
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        key = dt.astimezone(tz).date().isoformat()
        counts[key] = counts.get(key, 0) + 1
    return counts


def previous_runs_payload_to_daily(
    payload: dict[str, Any],
    *,
    station_icao: str,
    model_name: str,
    timezone_name: str,
    temperature_unit: str,
) -> pd.DataFrame:
    """One row per (valid_local_date, lead_day), forecast_history_daily-shaped."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    fetched = utc_now_iso()
    rows: list[dict[str, Any]] = []
    for n in range(PREVIOUS_DAY_MIN, PREVIOUS_DAY_MAX + 1):
        values = hourly.get(f"temperature_2m_previous_day{n}")
        if not values or all(v is None for v in values):
            continue
        daily = hourly_to_daily_max_min(times, values, timezone_name)
        counts = _non_null_counts_by_local_date(times, values, timezone_name)
        for _, r in daily.iterrows():
            d = str(r["valid_local_date"])
            rows.append(
                {
                    "station_icao": station_icao,
                    "model_name": model_name,
                    "source_api": "previous_runs",
                    "forecast_init_utc": effective_init_utc(d, n, timezone_name),
                    "lead_day": n - 1,
                    "valid_local_date": d,
                    "temp_max": float(r["temp_max"]),
                    "temp_min": float(r["temp_min"]),
                    "n_hours": int(counts.get(d, 0)),
                    "temperature_unit": temperature_unit,
                    "fetched_at": fetched,
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                }
            )
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)
