"""Weather prediction-market probability layer.

Station-level temperature PDFs → bucket probs → weather_edge.
Does not use decision/edge_calculator (politics price-change path).
"""

from weather.paths import resolve_weather_data_root
from weather.stations import StationRegistry, load_station_registry

__all__ = [
    "StationRegistry",
    "load_station_registry",
    "resolve_weather_data_root",
]
