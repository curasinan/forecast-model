"""NOAA label fetchers for climate contract oracles.

Two source families (docs/venue_semantics.md, 'Climate contract
resolution oracles', verified 2026-08-18):

- GTM / GT: NCEI Climate at a Glance global land+ocean average
  temperature departures, degC vs the 1901-2000 base period. GTM is
  the monthly series, GT the January-December annual value.
- ACD: NOAA GML Mauna Loa CO2 in ppm. The annual mean file settles
  ACD; the monthly file is the (currently unlisted) MACD sibling.

Endpoint shapes were discovered empirically (2026-08-18 probe):

- Climate at a Glance data.json:
    {"description": {"title": ..., "units": "Degrees Celsius",
                     "base_period": "1901-2000"},
     "data": {"185001": {"departure": -0.43}, ...}}
  Monthly keys are 'YYYYMM'; the 12/12 annual variant uses plain 'YYYY'.
- GML txt files: '#'-prefixed header lines, then whitespace columns
  (annual: year mean unc; monthly: year month decimal-date average
  deseasonalized ndays stdev unc).

Fetchers return tidy lists of LabelObservation(series, period, value,
source_url). They raise OracleDriftError when source metadata no longer
matches the contract's oracle definition (base period or units change):
fail loudly rather than record mislabeled settlement data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple

import httpx

TIMEOUT_S = 30.0

CAG_BASE = (
    "https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/"
    "global/time-series/globe/tavg/land_ocean"
)
GML_CO2_ANNUAL_URL = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.txt"
GML_CO2_MONTHLY_URL = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_mm_mlo.txt"

EXPECTED_BASE_PERIOD = "1901-2000"
EXPECTED_UNITS = "Degrees Celsius"

# GML missing-data sentinels are negative (e.g. -9.99 / -99.99).
GML_MISSING_THRESHOLD = 0.0


class OracleDriftError(RuntimeError):
    """Source metadata no longer matches the contract oracle definition."""


class LabelObservation(NamedTuple):
    series: str
    period: str
    value: float
    source_url: str


def gtm_url(end_year: int | None = None) -> str:
    end_year = end_year or datetime.now(timezone.utc).year
    return f"{CAG_BASE}/1/0/1850-{end_year}/data.json"


def gt_url(end_year: int | None = None) -> str:
    end_year = end_year or datetime.now(timezone.utc).year
    return f"{CAG_BASE}/12/12/1850-{end_year}/data.json"


def _check_cag_metadata(payload: dict[str, Any], source_url: str) -> None:
    """Oracle drift detection: fail loudly if base period or units moved."""
    desc = payload.get("description")
    if not isinstance(desc, dict):
        raise OracleDriftError(f"No 'description' metadata in {source_url}")
    base = desc.get("base_period")
    if base != EXPECTED_BASE_PERIOD:
        raise OracleDriftError(
            f"Base period drift at {source_url}: expected "
            f"{EXPECTED_BASE_PERIOD!r}, got {base!r}. Contracts settle on the "
            "1901-2000 baseline; do NOT record values from a different base."
        )
    units = desc.get("units")
    if units != EXPECTED_UNITS:
        raise OracleDriftError(
            f"Units drift at {source_url}: expected {EXPECTED_UNITS!r}, got {units!r}."
        )


def _extract_value(raw: Any) -> float:
    if isinstance(raw, dict):
        if "departure" not in raw:
            raise ValueError(f"Unexpected value payload (no 'departure'): {raw!r}")
        raw = raw["departure"]
    return float(raw)


def _normalize_period(key: str, kind: str) -> str:
    key = key.strip()
    if kind == "monthly":
        if len(key) == 6 and key.isdigit():
            return f"{key[:4]}-{key[4:]}"
        raise ValueError(f"Unexpected monthly period key: {key!r}")
    if kind == "annual":
        if len(key) == 4 and key.isdigit():
            return key
        # Defensive: some CaG configurations key annual values as YYYY12.
        if len(key) == 6 and key.isdigit() and key.endswith("12"):
            return key[:4]
        raise ValueError(f"Unexpected annual period key: {key!r}")
    raise ValueError(f"Unknown period kind: {kind!r}")


def parse_cag_json(
    payload: dict[str, Any], series: str, kind: str, source_url: str
) -> list[LabelObservation]:
    """Parse a Climate at a Glance data.json payload into tidy rows."""
    _check_cag_metadata(payload, source_url)
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise ValueError(f"No 'data' section in {source_url}")
    rows = [
        LabelObservation(series, _normalize_period(k, kind), _extract_value(v), source_url)
        for k, v in data.items()
    ]
    rows.sort(key=lambda r: r.period)
    return rows


def parse_gml_annual(text: str, source_url: str) -> list[LabelObservation]:
    """Parse co2_annmean_mlo.txt (columns: year mean unc) -> acd_annual rows."""
    rows: list[LabelObservation] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            raise ValueError(f"Malformed annual CO2 line: {line!r}")
        year, mean = parts[0], float(parts[1])
        if mean < GML_MISSING_THRESHOLD:
            continue  # missing-data sentinel
        rows.append(LabelObservation("acd_annual", year, mean, source_url))
    if not rows:
        raise ValueError(f"No data rows parsed from {source_url}")
    rows.sort(key=lambda r: r.period)
    return rows


def parse_gml_monthly(text: str, source_url: str) -> list[LabelObservation]:
    """Parse co2_mm_mlo.txt (year month ... average ...) -> acd_monthly rows.

    Uses the 'average' column (4th), which is what the oracle publishes.
    """
    rows: list[LabelObservation] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            raise ValueError(f"Malformed monthly CO2 line: {line!r}")
        year, month, average = parts[0], int(parts[1]), float(parts[3])
        if average < GML_MISSING_THRESHOLD:
            continue  # missing-data sentinel (-9.99 / -99.99)
        rows.append(LabelObservation("acd_monthly", f"{year}-{month:02d}", average, source_url))
    if not rows:
        raise ValueError(f"No data rows parsed from {source_url}")
    rows.sort(key=lambda r: r.period)
    return rows


def _get(url: str) -> httpx.Response:
    resp = httpx.get(url, timeout=TIMEOUT_S, follow_redirects=True)
    resp.raise_for_status()
    return resp


def fetch_gtm(end_year: int | None = None) -> list[LabelObservation]:
    """NCEI monthly global land+ocean anomaly (degC, base 1901-2000)."""
    url = gtm_url(end_year)
    return parse_cag_json(_get(url).json(), "gtm", "monthly", url)


def fetch_gt(end_year: int | None = None) -> list[LabelObservation]:
    """NCEI annual (Jan-Dec) global land+ocean anomaly (degC, base 1901-2000)."""
    url = gt_url(end_year)
    return parse_cag_json(_get(url).json(), "gt", "annual", url)


def fetch_acd_annual() -> list[LabelObservation]:
    """NOAA GML Mauna Loa annual mean CO2 (ppm) -- the ACD settlement series."""
    return parse_gml_annual(_get(GML_CO2_ANNUAL_URL).text, GML_CO2_ANNUAL_URL)


def fetch_acd_monthly() -> list[LabelObservation]:
    """NOAA GML Mauna Loa monthly mean CO2 (ppm) -- MACD sibling series."""
    return parse_gml_monthly(_get(GML_CO2_MONTHLY_URL).text, GML_CO2_MONTHLY_URL)
