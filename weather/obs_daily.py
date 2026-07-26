"""Aggregate raw station observations to local-calendar daily max/min labels.

Training labels for EMOS live in the ``obs_daily`` table; market resolution
truth stays in ``resolution_labels``. The finalized + 72h rule from
weather.finalization applies before ``usable_for_training=1``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from weather.finalization import is_local_day_complete, usable_for_training
from weather.storage import init_weather_db, utc_now_iso

DEFAULT_MIN_HOURS_COVERED = 18

DAILY_COLUMNS = [
    "valid_local_date",
    "tmax_f",
    "tmin_f",
    "tmax_c",
    "tmin_c",
    "n_obs",
    "n_hours_covered",
    "first_obs_utc",
    "last_obs_utc",
]


def observations_to_daily(df: pd.DataFrame, *, timezone_name: str) -> pd.DataFrame:
    """Group irregular obs (observations-table shaped) into local daily rows.

    n_hours_covered counts distinct elapsed UTC hour buckets with >=1 report,
    so a 25-hour fall-back local day can reach 25 and a 23-hour spring-forward
    day tops out at 23.
    """
    if df.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    tz = ZoneInfo(timezone_name)
    work = df.copy()
    ts = pd.to_datetime(work["valid_utc"], utc=True, format="mixed")
    local = ts.dt.tz_convert(tz)
    work["_ts"] = ts
    work["_utc_hour"] = ts.dt.floor("h")
    work["valid_local_date"] = local.dt.date.astype(str)

    nan_col = pd.Series(float("nan"), index=work.index)
    temp_f = (
        pd.to_numeric(work["temp_f"], errors="coerce") if "temp_f" in work.columns else nan_col
    )
    temp_c = (
        pd.to_numeric(work["temp_c"], errors="coerce") if "temp_c" in work.columns else nan_col
    )
    # Prefer F when present (ASOS native); derive the other unit — max/min
    # commute with the linear conversion, so aggregating either is equivalent.
    temp_f = temp_f.fillna(temp_c * 9.0 / 5.0 + 32.0)
    work["_temp_f"] = temp_f
    work = work.dropna(subset=["_temp_f"])
    if work.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    rows: list[dict[str, Any]] = []
    for d, g in work.groupby("valid_local_date", sort=True):
        tmax_f = float(g["_temp_f"].max())
        tmin_f = float(g["_temp_f"].min())
        rows.append(
            {
                "valid_local_date": str(d),
                "tmax_f": tmax_f,
                "tmin_f": tmin_f,
                "tmax_c": (tmax_f - 32.0) * 5.0 / 9.0,
                "tmin_c": (tmin_f - 32.0) * 5.0 / 9.0,
                "n_obs": int(len(g)),
                "n_hours_covered": int(g["_utc_hour"].nunique()),
                "first_obs_utc": g["_ts"].min().isoformat(),
                "last_obs_utc": g["_ts"].max().isoformat(),
            }
        )
    return pd.DataFrame(rows, columns=DAILY_COLUMNS)


def finalize_obs_daily(
    daily: pd.DataFrame,
    *,
    timezone_name: str,
    min_hours: int = DEFAULT_MIN_HOURS_COVERED,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Stamp finalized / usable_for_training flags onto daily rows."""
    out = daily.copy()
    finalized: list[int] = []
    usable: list[int] = []
    for _, r in out.iterrows():
        d = str(r["valid_local_date"])
        complete = is_local_day_complete(d, timezone_name, now)
        is_final = bool(complete and int(r["n_hours_covered"]) >= min_hours)
        finalized.append(1 if is_final else 0)
        usable.append(1 if usable_for_training(d, is_final, now) else 0)
    out["finalized"] = finalized
    out["usable_for_training"] = usable
    return out


def _none_if_nan(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    return value


def upsert_obs_daily(
    db_path: Path | str | None,
    station_icao: str,
    daily: pd.DataFrame,
    source: str = "iem_asos",
) -> int:
    """Batch upsert finalized daily rows; returns row count written."""
    if daily.empty:
        return 0
    path = init_weather_db(db_path)
    computed = utc_now_iso()
    params = [
        (
            station_icao,
            str(r["valid_local_date"]),
            source,
            _none_if_nan(r["tmax_f"]),
            _none_if_nan(r["tmin_f"]),
            _none_if_nan(r["tmax_c"]),
            _none_if_nan(r["tmin_c"]),
            int(r["n_obs"]),
            int(r["n_hours_covered"]),
            _none_if_nan(r["first_obs_utc"]),
            _none_if_nan(r["last_obs_utc"]),
            int(r["finalized"]),
            int(r["usable_for_training"]),
            computed,
        )
        for _, r in daily.iterrows()
    ]
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            """
            INSERT INTO obs_daily (
                station_icao, valid_local_date, source,
                tmax_f, tmin_f, tmax_c, tmin_c,
                n_obs, n_hours_covered, first_obs_utc, last_obs_utc,
                finalized, usable_for_training, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET
                tmax_f=excluded.tmax_f,
                tmin_f=excluded.tmin_f,
                tmax_c=excluded.tmax_c,
                tmin_c=excluded.tmin_c,
                n_obs=excluded.n_obs,
                n_hours_covered=excluded.n_hours_covered,
                first_obs_utc=excluded.first_obs_utc,
                last_obs_utc=excluded.last_obs_utc,
                finalized=excluded.finalized,
                usable_for_training=excluded.usable_for_training,
                computed_at=excluded.computed_at
            """,
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return len(params)
