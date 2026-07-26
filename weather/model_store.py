"""Persist fitted EMOS parameters as JSON under <data_root>/models/emos/.

One file per (station, metric, lead_day). Files carry a metadata block
(sample count, train range, feature_set, ...) so consumers can refuse
params fitted on a mismatched feature definition.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from weather.emos import EMOSParams
from weather.paths import resolve_weather_data_root

STORE_SCHEMA_VERSION = "emos_store_v1"


def emos_model_dir(root: Path | None = None) -> Path:
    root = root or resolve_weather_data_root()
    d = root / "models" / "emos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_filename(station_icao: str, metric: str, lead_day: int) -> str:
    return f"{station_icao}_{metric}_lead{lead_day}.json"


def save_emos_params(
    params: EMOSParams, *, metadata: dict[str, Any], root: Path | None = None
) -> Path:
    """Atomic write (tmp + os.replace) so a crash never leaves torn JSON."""
    target = emos_model_dir(root) / model_filename(
        params.station_icao, params.metric, params.lead_day
    )
    doc = {
        "schema_version": STORE_SCHEMA_VERSION,
        "a": float(params.a),
        "b": float(params.b),
        "c": float(params.c),
        "d": float(params.d),
        "station_icao": params.station_icao,
        "lead_day": int(params.lead_day),
        "metric": params.metric,
        "model_version": params.model_version,
        "metadata": metadata,
    }
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def load_emos_params(path: Path | str) -> tuple[EMOSParams, dict[str, Any]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    params = EMOSParams(
        a=float(doc["a"]),
        b=float(doc["b"]),
        c=float(doc["c"]),
        d=float(doc["d"]),
        station_icao=str(doc.get("station_icao", "")),
        lead_day=int(doc.get("lead_day", 0)),
        metric=str(doc.get("metric", "high")),
    )
    return params, dict(doc.get("metadata") or {})


def load_all_emos_params(
    root: Path | None = None,
) -> dict[tuple[str, str, int], tuple[EMOSParams, dict[str, Any]]]:
    out: dict[tuple[str, str, int], tuple[EMOSParams, dict[str, Any]]] = {}
    for path in sorted(emos_model_dir(root).glob("*.json")):
        try:
            params, meta = load_emos_params(path)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        out[(params.station_icao, params.metric, params.lead_day)] = (params, meta)
    return out
