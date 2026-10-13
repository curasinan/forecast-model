"""Serve-path features matching historical EMOS ``xmodel_prevruns_v1``."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from collectors.weather.collect_history_forecasts import store_history_rows
from weather.emos import EMOSParams
from weather.history import previous_runs_payload_to_daily
from weather.model_store import load_all_emos_params
from weather.open_meteo import OpenMeteoClient
from weather.stations import Station, StationRegistry, load_station_registry
from weather.training import (
    DEFAULT_MODELS,
    FEATURE_SET,
    decision_time_utc,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class XModelFeatures:
    """Serve-time features for feature_set xmodel_prevruns_v1."""

    ens_mean: float
    ens_var: float
    n_models: int
    lead_day: int
    valid_local_date: str
    station_icao: str
    metric: str
    models_present: tuple[str, ...]
    feature_set: str = FEATURE_SET
    source: str = "forecast_history_daily"  # or previous_runs_live


def lead_day_for_market(
    valid_local_date: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
    decision_hour_local: int = 12,
) -> int:
    """Lead day at the current (or frozen) decision instant."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(timezone_name))
    decision_date = local.date()
    v = date.fromisoformat(valid_local_date)
    return max(0, (v - decision_date).days)


def load_emos_for_feature_set(
    root: Path | None = None,
    *,
    required_feature_set: str = FEATURE_SET,
) -> dict[tuple[str, str, int], tuple[EMOSParams, dict[str, Any]]]:
    """Load persisted EMOS params; drop any with mismatched feature_set."""
    raw = load_all_emos_params(root)
    out: dict[tuple[str, str, int], tuple[EMOSParams, dict[str, Any]]] = {}
    skipped = 0
    for key, (params, meta) in raw.items():
        if meta.get("feature_set") != required_feature_set:
            skipped += 1
            log.warning(
                "Skipping EMOS %s: feature_set=%r != %r",
                key,
                meta.get("feature_set"),
                required_feature_set,
            )
            continue
        out[key] = (params, meta)
    if skipped:
        log.info("Loaded %s EMOS models (%s skipped for feature_set)", len(out), skipped)
    else:
        log.info("Loaded %s EMOS models with feature_set=%s", len(out), required_feature_set)
    return out


def _rows_from_history_db(
    db_path: Path | str,
    *,
    station: Station,
    valid_local_date: str,
    lead_day: int,
    metric: str,
    models: tuple[str, ...],
    min_hours_forecast: int,
    decision_hour_local: int,
    registry: StationRegistry,
    now: datetime | None = None,
) -> dict[str, float]:
    """Return model_name -> daily extreme for one (station, date, lead)."""
    del now
    expected_unit = "celsius" if station.unit_default.upper() == "C" else "fahrenheit"
    value_col = "temp_max" if metric == "high" else "temp_min"
    placeholders = ",".join("?" for _ in models)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT model_name, forecast_init_utc, temp_max, temp_min, n_hours
              FROM forecast_history_daily
             WHERE station_icao = ? AND valid_local_date = ? AND lead_day = ?
               AND source_api = 'previous_runs' AND temperature_unit = ?
               AND n_hours >= ? AND model_name IN ({placeholders})
            """,
            (
                station.icao,
                valid_local_date,
                lead_day,
                expected_unit,
                min_hours_forecast,
                *models,
            ),
        ).fetchall()
    finally:
        conn.close()

    decision = decision_time_utc(
        valid_local_date, lead_day, decision_hour_local, station.timezone
    )
    out: dict[str, float] = {}
    for r in rows:
        value = r[value_col]
        if value is None:
            continue
        init = datetime.fromisoformat(str(r["forecast_init_utc"]).replace("Z", "+00:00"))
        if init.tzinfo is None:
            init = init.replace(tzinfo=timezone.utc)
        lag = registry.availability_lag_hours(r["model_name"])
        if init + timedelta(hours=lag) > decision:
            continue
        out[str(r["model_name"])] = float(value)
    return out


def _fetch_live_previous_runs_into_db(
    client: OpenMeteoClient,
    db_path: Path | str,
    *,
    station: Station,
    valid_local_date: str,
    models: tuple[str, ...],
) -> None:
    """Fetch a small Previous Runs window covering valid_local_date; upsert DB."""
    unit = "celsius" if station.unit_default.upper() == "C" else "fahrenheit"
    # Pad ±1 day so local-day aggregation is complete.
    d = date.fromisoformat(valid_local_date)
    start = (d - timedelta(days=1)).isoformat()
    end = (d + timedelta(days=1)).isoformat()
    for model in models:
        try:
            payload = client.fetch_previous_runs(
                station.lat,
                station.lon,
                start_date=start,
                end_date=end,
                model=model,
                temperature_unit=unit,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Live previous-runs fetch failed %s %s: %s", station.icao, model, exc)
            continue
        df = previous_runs_payload_to_daily(
            payload,
            station_icao=station.icao,
            model_name=model,
            timezone_name=station.timezone,
            temperature_unit=unit,
        )
        if df.empty:
            continue
        store_history_rows(db_path, df)
        log.info(
            "Cached %s previous-runs rows for %s/%s around %s",
            len(df),
            station.icao,
            model,
            valid_local_date,
        )


def build_xmodel_features(
    db_path: Path | str,
    *,
    station_icao: str,
    valid_local_date: str,
    metric: str,
    lead_day: int | None = None,
    models: tuple[str, ...] = DEFAULT_MODELS,
    require_full_ensemble: bool = True,
    min_hours_forecast: int = 18,  # slightly looser for live partial days
    decision_hour_local: int = 12,
    registry: StationRegistry | None = None,
    client: OpenMeteoClient | None = None,
    fetch_if_missing: bool = True,
    now: datetime | None = None,
) -> XModelFeatures | None:
    """Build xmodel_prevruns_v1 features for one market decision."""
    if metric not in ("high", "low"):
        raise ValueError(f"metric must be high|low, got {metric!r}")
    registry = registry or load_station_registry()
    station = registry.require(station_icao)
    if lead_day is None:
        lead_day = lead_day_for_market(
            valid_local_date, station.timezone, now=now, decision_hour_local=decision_hour_local
        )
    if lead_day > 6:
        log.debug("lead_day=%s > 6; previous_day7 may be missing", lead_day)

    by_model = _rows_from_history_db(
        db_path,
        station=station,
        valid_local_date=valid_local_date,
        lead_day=lead_day,
        metric=metric,
        models=models,
        min_hours_forecast=min_hours_forecast,
        decision_hour_local=decision_hour_local,
        registry=registry,
        now=now,
    )
    source = "forecast_history_daily"

    need_fetch = (
        fetch_if_missing
        and client is not None
        and (
            (require_full_ensemble and set(by_model) != set(models))
            or (not require_full_ensemble and len(by_model) < 2)
        )
    )
    if need_fetch:
        _fetch_live_previous_runs_into_db(
            client, db_path, station=station, valid_local_date=valid_local_date, models=models
        )
        by_model = _rows_from_history_db(
            db_path,
            station=station,
            valid_local_date=valid_local_date,
            lead_day=lead_day,
            metric=metric,
            models=models,
            min_hours_forecast=min_hours_forecast,
            decision_hour_local=decision_hour_local,
            registry=registry,
            now=now,
        )
        source = "previous_runs_live"

    if require_full_ensemble and set(by_model) != set(models):
        log.debug(
            "Incomplete ensemble for %s %s lead%s: have %s",
            station_icao,
            valid_local_date,
            lead_day,
            sorted(by_model),
        )
        return None
    if len(by_model) < 2:
        return None

    values = np.array([by_model[m] for m in sorted(by_model)], dtype=float)
    return XModelFeatures(
        ens_mean=float(values.mean()),
        ens_var=float(values.var(ddof=0)),
        n_models=int(len(values)),
        lead_day=int(lead_day),
        valid_local_date=valid_local_date,
        station_icao=station.icao,
        metric=metric,
        models_present=tuple(sorted(by_model)),
        feature_set=FEATURE_SET,
        source=source,
    )
