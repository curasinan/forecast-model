"""Forecast and observation collection tools."""

from __future__ import annotations

import sqlite3
from typing import Any

from mcp_server.serializers import clamp_limit, err, ok
from weather.storage import init_weather_db


def _allow_noncommercial(flag: bool) -> None:
    if flag:
        from src.config import settings

        settings.open_meteo_allow_noncommercial = True


def wx_collect_forecasts(
    station_icao: str,
    *,
    allow_noncommercial: bool = False,
    include_ai: bool = False,
) -> dict[str, Any]:
    """Collect Open-Meteo ensemble forecasts for one station into weather.db."""
    if not station_icao:
        return err("bad_request", "station_icao is required")
    _allow_noncommercial(allow_noncommercial)
    try:
        from collectors.weather.collect_forecasts import collect_station
        from weather.open_meteo import OpenMeteoClient, OpenMeteoLicenseError
    except Exception as exc:  # noqa: BLE001
        return err("import_failed", str(exc))
    try:
        client = OpenMeteoClient(for_trading=True)
    except OpenMeteoLicenseError as exc:
        return err(
            "open_meteo_license",
            str(exc),
            hint="Set OPEN_METEO_API_KEY or pass allow_noncommercial=true for research.",
        )
    try:
        n = collect_station(
            client,
            station_icao.upper(),
            include_ai_members=include_ai,
        )
        return ok({"station_icao": station_icao.upper(), "rows_written": n})
    except Exception as exc:  # noqa: BLE001
        return err("forecast_failed", str(exc), station_icao=station_icao.upper())


def wx_collect_obs(
    station_icao: str,
    *,
    hours: int = 48,
) -> dict[str, Any]:
    """Collect recent IEM ASOS observations for a US (K*) station."""
    if not station_icao:
        return err("bad_request", "station_icao is required")
    icao = station_icao.upper()
    if not icao.startswith("K"):
        return err(
            "unsupported_station",
            f"IEM ASOS collector supports US K* ICAOs; got {icao}",
        )
    try:
        from collectors.weather.collect_obs_iem import collect_station
    except Exception as exc:  # noqa: BLE001
        return err("import_failed", str(exc))
    try:
        n = collect_station(icao, hours=hours)
        return ok({"station_icao": icao, "rows_written": n, "hours": hours})
    except Exception as exc:  # noqa: BLE001
        return err("obs_failed", str(exc), station_icao=icao)


def wx_list_labels(
    *,
    station_icao: str | None = None,
    min_date: str | None = None,
    limit: int = 50,
    weather_db: str | None = None,
) -> dict[str, Any]:
    """List recent resolution_labels from weather.db (read-only)."""
    path = init_weather_db(weather_db)
    lim = clamp_limit(limit)
    sql = """
        SELECT station_icao, valid_local_date, metric, label_value, label_source,
               usable_for_training
          FROM resolution_labels
         WHERE 1=1
    """
    params: list[Any] = []
    if station_icao:
        sql += " AND station_icao = ?"
        params.append(station_icao.upper())
    if min_date:
        sql += " AND valid_local_date >= ?"
        params.append(min_date)
    sql += " ORDER BY valid_local_date DESC LIMIT ?"
    params.append(lim)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as exc:
        return err("query_failed", str(exc))
    finally:
        conn.close()
    return ok(rows, count=len(rows), weather_db=str(path))
