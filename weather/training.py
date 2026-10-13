"""Build no-look-ahead EMOS training frames from the historical tables."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from weather.finalization import usable_for_training
from weather.stations import StationRegistry, load_station_registry

log = logging.getLogger(__name__)

DEFAULT_MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")
FEATURE_SET = "xmodel_prevruns_v1"

FRAME_COLUMNS = [
    "valid_local_date",
    "obs_value",
    "ens_mean",
    "ens_var",
    "n_models",
    "label_source",
]

OBS_LABEL_SOURCE = "iem_asos"
DEFAULT_LABEL_SOURCES = ("iem_cli", "nws_cli", "wunderground_api", OBS_LABEL_SOURCE)


@dataclass(frozen=True)
class TrainingFrameSpec:
    station_icao: str
    metric: str  # "high" | "low"
    lead_day: int
    models: tuple[str, ...] = DEFAULT_MODELS
    decision_hour_local: int = 12
    min_hours_forecast: int = 22  # allows 23h DST-spring days, drops stubs
    min_hours_obs: int = 18
    require_full_ensemble: bool = True
    feature_set: str = FEATURE_SET
    label_sources: tuple[str, ...] = DEFAULT_LABEL_SOURCES


def label_priority_rank_sql(
    alias: str, sources: tuple[str, ...] = DEFAULT_LABEL_SOURCES
) -> str:
    """SQL CASE ranking label_source by priority (lower = preferred)."""
    if not all(s.replace("_", "").isalnum() for s in sources):
        raise ValueError(f"label sources must be identifier-like: {sources!r}")
    whens = " ".join(
        f"WHEN '{s}' THEN {i}" for i, s in enumerate(sources) if s != OBS_LABEL_SOURCE
    )
    return f"CASE {alias}.label_source {whens} ELSE {len(sources)} END"


def decision_time_utc(
    valid_local_date: str, lead_day: int, decision_hour: int, timezone_name: str
) -> datetime:
    """Local decision_hour:00 on day (D - lead_day), as aware UTC."""
    d = date.fromisoformat(valid_local_date) - timedelta(days=lead_day)
    local = datetime(d.year, d.month, d.day, decision_hour, tzinfo=ZoneInfo(timezone_name))
    return local.astimezone(timezone.utc)


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_training_frame(
    db_path: Path | str,
    spec: TrainingFrameSpec,
    *,
    registry: StationRegistry | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Columns: valid_local_date, obs_value, ens_mean, ens_var, n_models, label_source."""
    if spec.metric not in ("high", "low"):
        raise ValueError(f"metric must be 'high' or 'low', got {spec.metric!r}")
    registry = registry or load_station_registry()
    station = registry.require(spec.station_icao)

    expected_unit = "celsius" if station.unit_default.upper() == "C" else "fahrenheit"
    placeholders = ",".join("?" for _ in spec.models)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        fc_rows = conn.execute(
            f"""
            SELECT model_name, valid_local_date, forecast_init_utc, temp_max, temp_min
              FROM forecast_history_daily
             WHERE station_icao = ? AND lead_day = ? AND source_api = 'previous_runs'
               AND temperature_unit = ? AND n_hours >= ?
               AND model_name IN ({placeholders})
            """,
            (
                station.icao, spec.lead_day, expected_unit,
                spec.min_hours_forecast, *spec.models,
            ),
        ).fetchall()
        obs_rows: list[sqlite3.Row] = []
        if OBS_LABEL_SOURCE in spec.label_sources:
            obs_rows = conn.execute(
                """
                SELECT valid_local_date, tmax_f, tmin_f, tmax_c, tmin_c, finalized
                  FROM obs_daily
                 WHERE station_icao = ? AND source = ? AND usable_for_training = 1
                   AND n_hours_covered >= ?
                """,
                (station.icao, OBS_LABEL_SOURCE, spec.min_hours_obs),
            ).fetchall()
        settlement_sources = [s for s in spec.label_sources if s != OBS_LABEL_SOURCE]
        label_rows: list[sqlite3.Row] = []
        if settlement_sources:
            src_placeholders = ",".join("?" for _ in settlement_sources)
            label_rows = conn.execute(
                f"""
                SELECT valid_local_date, label_value, label_source, finalized
                  FROM resolution_labels
                 WHERE station_icao = ? AND metric = ? AND label_value IS NOT NULL
                   AND label_source IN ({src_placeholders})
                """,
                (station.icao, spec.metric, *settlement_sources),
            ).fetchall()
    finally:
        conn.close()

    empty = pd.DataFrame(columns=FRAME_COLUMNS)
    if not fc_rows or not (obs_rows or label_rows):
        return empty

    fc_value_col = "temp_max" if spec.metric == "high" else "temp_min"
    forecasts: dict[str, dict[str, float]] = {}
    for r in fc_rows:
        value = r[fc_value_col]
        if value is None:
            continue
        d = r["valid_local_date"]
        lag = registry.availability_lag_hours(r["model_name"])
        available = _parse_utc(r["forecast_init_utc"]) + timedelta(hours=lag)
        decision = decision_time_utc(
            d, spec.lead_day, spec.decision_hour_local, station.timezone
        )
        if available > decision:
            continue
        forecasts.setdefault(d, {})[r["model_name"]] = float(value)

    if station.unit_default.upper() == "C":
        obs_col = "tmax_c" if spec.metric == "high" else "tmin_c"
    else:
        obs_col = "tmax_f" if spec.metric == "high" else "tmin_f"

    candidates: dict[str, dict[str, float]] = {}
    if station.unit_default.upper() != "C":
        for r in label_rows:
            d = r["valid_local_date"]
            if not usable_for_training(d, bool(r["finalized"]), now):
                continue
            candidates.setdefault(d, {})[str(r["label_source"])] = float(r["label_value"])
    elif label_rows:
        log.warning(
            "%s: ignoring %s settlement labels (fahrenheit) for a celsius station",
            station.icao, len(label_rows),
        )
    for r in obs_rows:
        value = r[obs_col]
        if value is None:
            continue
        if not usable_for_training(r["valid_local_date"], bool(r["finalized"]), now):
            continue
        candidates.setdefault(r["valid_local_date"], {})[OBS_LABEL_SOURCE] = float(value)

    labels: dict[str, tuple[float, str]] = {}
    for d, by_source in candidates.items():
        for source in spec.label_sources:
            if source in by_source:
                labels[d] = (by_source[source], source)
                break

    rows = []
    for d in sorted(forecasts):
        if d not in labels:
            continue
        by_model = forecasts[d]
        if spec.require_full_ensemble and set(by_model) != set(spec.models):
            continue
        if len(by_model) < 2:
            continue
        values = np.array([by_model[m] for m in sorted(by_model)], dtype=float)
        label_value, label_source = labels[d]
        rows.append(
            {
                "valid_local_date": d,
                "obs_value": label_value,
                "ens_mean": float(values.mean()),
                "ens_var": float(values.var(ddof=0)),
                "n_models": int(len(values)),
                "label_source": label_source,
            }
        )
    if not rows:
        return empty
    return pd.DataFrame(rows, columns=FRAME_COLUMNS)
