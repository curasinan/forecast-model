"""ICAO station registry for Polymarket weather markets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = REPO_ROOT / "config" / "weather_stations.json"


@dataclass(frozen=True)
class Station:
    icao: str
    name: str
    city: str
    lat: float
    lon: float
    timezone: str
    unit_default: str
    coastal: bool
    platforms: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "icao": self.icao,
            "name": self.name,
            "city": self.city,
            "lat": self.lat,
            "lon": self.lon,
            "timezone": self.timezone,
            "unit_default": self.unit_default,
            "coastal": self.coastal,
            "platforms": list(self.platforms),
        }


class StationRegistry:
    def __init__(
        self,
        stations: dict[str, Station],
        model_availability_lag_hours: dict[str, float] | None = None,
    ):
        self._stations = stations
        self.model_availability_lag_hours = model_availability_lag_hours or {"default": 6.0}

    def get(self, icao: str) -> Station | None:
        return self._stations.get(icao.upper())

    def require(self, icao: str) -> Station:
        st = self.get(icao)
        if st is None:
            raise KeyError(f"Unknown station ICAO: {icao}")
        return st

    def all(self) -> list[Station]:
        return list(self._stations.values())

    def availability_lag_hours(self, model_name: str) -> float:
        return float(
            self.model_availability_lag_hours.get(
                model_name,
                self.model_availability_lag_hours.get("default", 6.0),
            )
        )


def load_station_registry(path: Path | str | None = None) -> StationRegistry:
    path = Path(path) if path else DEFAULT_REGISTRY_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))
    stations: dict[str, Station] = {}
    for icao, meta in raw.get("stations", {}).items():
        stations[icao.upper()] = Station(
            icao=icao.upper(),
            name=meta["name"],
            city=meta["city"],
            lat=float(meta["lat"]),
            lon=float(meta["lon"]),
            timezone=meta["timezone"],
            unit_default=meta.get("unit_default", "F"),
            coastal=bool(meta.get("coastal", False)),
            platforms=tuple(meta.get("platforms", ["global"])),
        )
    return StationRegistry(
        stations,
        model_availability_lag_hours=dict(raw.get("model_availability_lag_hours", {})),
    )
