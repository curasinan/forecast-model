"""Climate label/target pipeline.

NOAA oracle series for ForecastEx climate contracts (GT/GTM temperature
anomalies, ACD Mauna Loa CO2). See docs/venue_semantics.md, section
'Climate contract resolution oracles'.
"""

from climate.paths import data_root

__all__ = ["data_root"]
