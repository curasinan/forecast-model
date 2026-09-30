"""Aggregate hourly ensemble temperatures to local-calendar daily max/min."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


def hourly_to_daily_max_min(
    times: list[str],
    values: list[float | None],
    timezone: str,
) -> pd.DataFrame:
    """Return DataFrame with columns valid_local_date, temp_max, temp_min."""
    tz = ZoneInfo(timezone)
    rows: list[dict[str, Any]] = []
    for t, v in zip(times, values):
        if v is None:
            continue
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        local = dt.astimezone(tz)
        rows.append(
            {
                "valid_local_date": local.date().isoformat(),
                "temp": float(v),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["valid_local_date", "temp_max", "temp_min"])
    df = pd.DataFrame(rows)
    out = (
        df.groupby("valid_local_date", as_index=False)
        .agg(temp_max=("temp", "max"), temp_min=("temp", "min"))
    )
    return out


def ensemble_daily_from_open_meteo(
    payload: dict[str, Any],
    timezone: str,
    temperature_unit: str = "fahrenheit",
) -> pd.DataFrame:
    """Parse Open-Meteo ensemble JSON into long daily member rows.

    Handles:
      - daily.temperature_2m_max / _min arrays (preferred)
      - hourly.temperature_2m* member columns as fallback
    """
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    rows: list[dict[str, Any]] = []

    max_keys = [k for k in daily if k.startswith("temperature_2m_max")]

    if dates and max_keys:
        # Control / member suffixes: temperature_2m_max, temperature_2m_max_member01, ...
        for i, d in enumerate(dates):
            member_maxes: list[float] = []
            member_mins: list[float] = []
            for mk in max_keys:
                arr = daily.get(mk) or []
                if i < len(arr) and arr[i] is not None:
                    member_maxes.append(float(arr[i]))
                    member_id = "control" if mk == "temperature_2m_max" else mk.replace(
                        "temperature_2m_max_", ""
                    )
                    mn_key = mk.replace("temperature_2m_max", "temperature_2m_min")
                    mn_arr = daily.get(mn_key) or []
                    mn_val = float(mn_arr[i]) if i < len(mn_arr) and mn_arr[i] is not None else None
                    rows.append(
                        {
                            "valid_local_date": d,
                            "member_id": member_id,
                            "temp_max": float(arr[i]),
                            "temp_min": mn_val,
                            "temperature_unit": temperature_unit,
                        }
                    )
                    if mn_val is not None:
                        member_mins.append(mn_val)
            if member_maxes:
                # Summary row
                import statistics as stats

                rows.append(
                    {
                        "valid_local_date": d,
                        "member_id": "__ensemble_stats__",
                        "temp_max": stats.mean(member_maxes),
                        "temp_min": stats.mean(member_mins) if member_mins else None,
                        "ensemble_mean_max": stats.mean(member_maxes),
                        "ensemble_std_max": stats.pstdev(member_maxes) if len(member_maxes) > 1 else 0.0,
                        "ensemble_mean_min": stats.mean(member_mins) if member_mins else None,
                        "ensemble_std_min": stats.pstdev(member_mins) if len(member_mins) > 1 else 0.0,
                        "temperature_unit": temperature_unit,
                    }
                )
        return pd.DataFrame(rows)

    # Hourly fallback: look for temperature_2m and member columns
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temp_keys = [k for k in hourly if k.startswith("temperature_2m")]
    for key in temp_keys:
        member_id = "control" if key == "temperature_2m" else key.replace("temperature_2m_", "")
        vals = hourly.get(key) or []
        daily_df = hourly_to_daily_max_min(times, vals, timezone)
        for _, r in daily_df.iterrows():
            rows.append(
                {
                    "valid_local_date": r["valid_local_date"],
                    "member_id": member_id,
                    "temp_max": r["temp_max"],
                    "temp_min": r["temp_min"],
                    "temperature_unit": temperature_unit,
                }
            )
    return pd.DataFrame(rows)


def lead_hours(forecast_init_utc: str, valid_local_date: str, timezone: str) -> float:
    """Approximate lead: hours from init to local noon on valid date."""
    init = datetime.fromisoformat(forecast_init_utc.replace("Z", "+00:00"))
    if init.tzinfo is None:
        from datetime import timezone as tz

        init = init.replace(tzinfo=tz.utc)
    local_noon = datetime.fromisoformat(f"{valid_local_date}T12:00:00").replace(
        tzinfo=ZoneInfo(timezone)
    )
    return (local_noon.astimezone(init.tzinfo) - init).total_seconds() / 3600.0
