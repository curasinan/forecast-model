"""Parse Polymarket weather market rules → station / buckets / platform."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import httpx

from weather.stations import StationRegistry, load_station_registry
from weather.storage import init_weather_db, utc_now_iso, weather_db_path

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

ICAO_RE = re.compile(r"\b([A-Z]{4})\b")
WU_URL_RE = re.compile(
    r"https?://(?:www\.)?wunderground\.com/history/daily/[^\s\)\"']+/([A-Z]{4})",
    re.IGNORECASE,
)
WU_STATION_RE = re.compile(
    r"wunderground\.com/(?:history/daily/[^/\s]+/)+([A-Z]{4})",
    re.IGNORECASE,
)
STATION_NAME_RE = re.compile(
    r"(?:recorded at|at the|Airport Station|Station)\s+(?:the\s+)?"
    r"([A-Za-z0-9 .'\-]+?(?:Airport|Intl|International|Park|City)?)"
    r"(?:\s+Station)?",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b(\d{1,2})\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+'?(\d{2,4})\b",
    re.IGNORECASE,
)
# Polymarket daily weather often omits the year: "on July 22?"
DATE_MD_RE = re.compile(
    r"\b(\d{1,2})\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b",
    re.IGNORECASE,
)
DATE_MD_RE_ALT = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})\b",
    re.IGNORECASE,
)
DATE_ISO_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
# Station-level daily high/low — exclude climate / anomaly series.
GLOBAL_CLIMATE_RE = re.compile(
    r"\bglobal temperature\b|\btemperature anomaly\b|\bglobal warming\b",
    re.IGNORECASE,
)
# 1–3 digit temps only (ISO years like 2026 are excluded by digit cap + date scrub).
BUCKET_RANGE_RE = re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*(?:–|-|to)\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([FC])?\b",
    re.IGNORECASE,
)
BUCKET_OR_HIGHER_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*°?\s*([FC])?\s*(?:or\s+)?(?:higher|above|more)",
    re.IGNORECASE,
)
BUCKET_OR_LOWER_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*°?\s*([FC])?\s*(?:or\s+)?(?:lower|below|less)",
    re.IGNORECASE,
)

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Heuristic city → preferred Global ICAO when URL missing (always prefer URL).
CITY_HINTS: dict[str, str] = {
    "new york": "KLGA",
    "nyc": "KLGA",
    "chicago": "KORD",
    "los angeles": "KLAX",
    "la ": "KLAX",
    "paris": "LFPB",
    "london": "EGLC",
    "tokyo": "RJTT",
    "hong kong": "VHHH",
    "miami": "KMIA",
    "san francisco": "KSFO",
}

WEATHER_QUESTION_RE = re.compile(
    r"\b(temperature|temp|°[FC]|degrees?\s+[FC]|hottest|coldest|"
    r"highest temperature|lowest temperature|high temp|low temp)\b",
    re.IGNORECASE,
)


@dataclass
class ParsedWeatherMarket:
    market_id: str
    question: str
    platform: str  # global | us
    station_icao: str
    lat: float
    lon: float
    unit: str  # F | C
    valid_date: str  # YYYY-MM-DD
    metric: str  # high | low
    bucket_lo: float | None
    bucket_hi: float | None
    bucket_label: str
    resolution_url: str | None
    event_slug: str | None = None
    clob_token_ids: str | None = None
    raw_description: str = ""
    parse_notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def is_weather_question(text: str) -> bool:
    return bool(WEATHER_QUESTION_RE.search(text or ""))


def is_station_temperature_market(question: str, description: str = "") -> bool:
    """True for city/station daily temp buckets; false for global climate series."""
    blob = f"{question or ''}\n{description or ''}"
    if GLOBAL_CLIMATE_RE.search(blob):
        return False
    return is_weather_question(blob)


def _year_from_end_date(end_date: str | None) -> int | None:
    if not end_date:
        return None
    m = re.match(r"^(20\d{2})", str(end_date).strip())
    if m:
        return int(m.group(1))
    try:
        return datetime.fromisoformat(str(end_date).replace("Z", "+00:00")).year
    except ValueError:
        return None


def _coerce_valid_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def detect_platform(description: str, resolution_source: str | None = None) -> str:
    blob = f"{description or ''}\n{resolution_source or ''}".lower()
    if (
        "climate.weather.gov" in blob
        or "cli product" in blob
        or "nws daily climate" in blob
        or re.search(r"\bnws\s+cli\b", blob)
        or re.search(r"\bcli[a-z]{3}\b", blob)  # CLINYC, CLIMDW, ...
    ):
        return "us"
    if "wunderground.com" in blob:
        return "global"
    # Polymarket US docs use CLI; default global for classic WU series.
    return "global"


def detect_metric(question: str, description: str) -> str:
    blob = f"{question}\n{description}".lower()
    if "lowest" in blob or "minimum" in blob or "low temperature" in blob:
        return "low"
    return "high"


def detect_unit(question: str, description: str, station_default: str = "F") -> str:
    blob = f"{question}\n{description}"
    if re.search(r"\bCelsius\b|\b°C\b|degrees Celsius", blob, re.I):
        return "C"
    if re.search(r"\bFahrenheit\b|\b°F\b|degrees Fahrenheit", blob, re.I):
        return "F"
    return station_default.upper()


def parse_valid_date(
    question: str,
    description: str,
    end_date: str | None = None,
) -> str | None:
    """Parse local valid date from question/rules.

    Live Polymarket titles often omit the year (``on July 22?``). When that
    happens, prefer the year from ``end_date`` (market close), else UTC now.
    """
    for text in (question, description):
        m = DATE_RE.search(text or "")
        if m:
            day = int(m.group(1))
            month = MONTHS[m.group(2)[:3].lower()]
            year = int(m.group(3))
            if year < 100:
                year += 2000
            return _coerce_valid_date(year, month, day)
        m2 = DATE_ISO_RE.search(text or "")
        if m2:
            return _coerce_valid_date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))

    fallback_year = _year_from_end_date(end_date) or datetime.now(tz=None).year
    for text in (question, description):
        if not text:
            continue
        m = DATE_MD_RE.search(text)
        if m:
            day = int(m.group(1))
            month = MONTHS[m.group(2)[:3].lower()]
            # If end_date month is earlier than question month, year may be
            # previous (rare for daily markets); keep end_date year.
            return _coerce_valid_date(fallback_year, month, day)
        m = DATE_MD_RE_ALT.search(text)
        if m:
            month = MONTHS[m.group(1)[:3].lower()]
            day = int(m.group(2))
            return _coerce_valid_date(fallback_year, month, day)
    # Last resort: calendar date of end_date (noon-UTC closes map to event day).
    if end_date:
        try:
            dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
            return dt.date().isoformat()
        except ValueError:
            pass
    return None


def extract_icao(
    description: str,
    question: str,
    registry: StationRegistry,
) -> tuple[str | None, str | None, list[str]]:
    notes: list[str] = []
    wu = WU_URL_RE.search(description or "") or WU_STATION_RE.search(description or "")
    resolution_url = wu.group(0) if wu else None
    if wu:
        icao = wu.group(1).upper()
        notes.append(f"icao_from_wunderground_url:{icao}")
        return icao, resolution_url, notes

    # Prefer known ICAOs appearing in text.
    for match in ICAO_RE.finditer(f"{description or ''} {question or ''}"):
        cand = match.group(1).upper()
        if registry.get(cand):
            notes.append(f"icao_from_text:{cand}")
            return cand, None, notes

    blob = f"{question or ''} {description or ''}".lower()
    for hint, icao in CITY_HINTS.items():
        if hint in blob and registry.get(icao):
            notes.append(f"icao_from_city_hint:{hint}->{icao}")
            return icao, None, notes

    notes.append("icao_unresolved")
    return None, resolution_url, notes


def _parse_bucket_from_text(text: str) -> tuple[float | None, float | None, str] | None:
    if not text:
        return None
    # Strip ISO dates so 2026-07-10 cannot look like a temp range.
    scrubbed = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " ", text)
    m = BUCKET_RANGE_RE.search(scrubbed)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        if abs(lo) <= 150 and abs(hi) <= 150:
            # Polymarket whole-degree bins: treat hi as inclusive whole degree,
            # so continuous upper edge is hi + 1 for truncation space.
            return lo, hi + 1.0, f"{int(lo)}-{int(hi)}"

    m = BUCKET_OR_HIGHER_RE.search(scrubbed)
    if m:
        lo = float(m.group(1))
        return lo, None, f"{int(lo)}+"

    m = BUCKET_OR_LOWER_RE.search(scrubbed)
    if m:
        hi = float(m.group(1))
        return None, hi + 1.0, f"<={int(hi)}"

    # Single number with unit e.g. "64°F" (avoid bare Yes/No titles)
    m = re.search(r"(-?\d+)\s*°\s*[FC]\b", scrubbed, re.I)
    if m:
        v = float(m.group(1))
        return v, v + 1.0, str(int(v))
    m = re.search(r"(-?\d+)\s*°?\s*([FC])\b", scrubbed, re.I)
    if m:
        v = float(m.group(1))
        return v, v + 1.0, str(int(v))
    return None


def parse_bucket_bounds(
    question: str,
    group_item_title: str | None = None,
) -> tuple[float | None, float | None, str]:
    """Parse temperature bucket from question / outcome title.

    Returns (lo, hi_exclusive_or_None_for_open, label).
    Open-ended higher: hi=None; open-ended lower: lo=None.
    """
    for text in (group_item_title, question):
        parsed = _parse_bucket_from_text(text or "")
        if parsed is not None:
            return parsed
    return None, None, (group_item_title or question or "").strip()[:64]


def parse_weather_market(
    market: dict[str, Any],
    registry: StationRegistry | None = None,
) -> ParsedWeatherMarket | None:
    registry = registry or load_station_registry()
    question = market.get("question") or market.get("title") or ""
    description = (
        market.get("description")
        or market.get("rules")
        or market.get("resolutionSource")
        or ""
    )
    if not is_station_temperature_market(question, description):
        return None

    platform = detect_platform(description, market.get("resolutionSource"))
    icao, resolution_url, notes = extract_icao(description, question, registry)
    if icao is None:
        return None

    station = registry.get(icao)
    if station is None:
        notes.append("station_not_in_registry")
        # Still emit with NaN coords so caller can extend registry.
        lat, lon, unit_default = float("nan"), float("nan"), "F"
        timezone = "UTC"
    else:
        lat, lon, unit_default = station.lat, station.lon, station.unit_default
        timezone = station.timezone  # noqa: F841 — kept for future local-date checks

    unit = detect_unit(question, description, unit_default)
    metric = detect_metric(question, description)
    end_date = market.get("endDate") or market.get("end_date")
    valid = parse_valid_date(question, description, end_date=end_date)
    if valid is None:
        notes.append("valid_date_unresolved")
        return None

    group_title = market.get("groupItemTitle") or market.get("outcome") or None
    lo, hi, label = parse_bucket_bounds(question, group_title)

    return ParsedWeatherMarket(
        market_id=str(market.get("id") or market.get("conditionId") or ""),
        question=question,
        platform=platform,
        station_icao=icao,
        lat=lat,
        lon=lon,
        unit=unit,
        valid_date=valid,
        metric=metric,
        bucket_lo=lo,
        bucket_hi=hi,
        bucket_label=label,
        resolution_url=resolution_url,
        event_slug=market.get("slug") or market.get("eventSlug"),
        clob_token_ids=json.dumps(market.get("clobTokenIds"))
        if isinstance(market.get("clobTokenIds"), list)
        else market.get("clobTokenIds") or market.get("clob_token_ids"),
        raw_description=description,
        parse_notes=notes,
    )


def upsert_parsed_markets(
    parsed: Iterable[ParsedWeatherMarket],
    db_path: Path | str | None = None,
) -> int:
    path = init_weather_db(db_path)
    conn = sqlite3.connect(path)
    n = 0
    try:
        for p in parsed:
            conn.execute(
                """
                INSERT INTO weather_markets (
                    market_id, event_slug, question, platform, station_icao,
                    valid_local_date, metric, unit, bucket_lo, bucket_hi,
                    bucket_label, resolution_url, clob_token_ids, parsed_at,
                    raw_description
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    question=excluded.question,
                    platform=excluded.platform,
                    station_icao=excluded.station_icao,
                    valid_local_date=excluded.valid_local_date,
                    metric=excluded.metric,
                    unit=excluded.unit,
                    bucket_lo=excluded.bucket_lo,
                    bucket_hi=excluded.bucket_hi,
                    bucket_label=excluded.bucket_label,
                    resolution_url=excluded.resolution_url,
                    clob_token_ids=excluded.clob_token_ids,
                    parsed_at=excluded.parsed_at,
                    raw_description=excluded.raw_description
                """,
                (
                    p.market_id,
                    p.event_slug,
                    p.question,
                    p.platform,
                    p.station_icao,
                    p.valid_date,
                    p.metric,
                    p.unit,
                    p.bucket_lo,
                    p.bucket_hi,
                    p.bucket_label,
                    p.resolution_url,
                    p.clob_token_ids,
                    utc_now_iso(),
                    p.raw_description,
                ),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def fetch_gamma_weather_events(limit: int = 50, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Best-effort discovery of active weather events from Gamma."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": min(limit, 100),
        "tag_slug": "weather",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{GAMMA_BASE}/events", params=params)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data
    except Exception as exc:  # noqa: BLE001
        log.warning("Gamma weather tag fetch failed: %s", exc)

    # Fallback: search endpoint / markets text filter is unreliable; return [].
    return []


def parse_events(events: list[dict[str, Any]], registry: StationRegistry | None = None) -> list[ParsedWeatherMarket]:
    registry = registry or load_station_registry()
    out: list[ParsedWeatherMarket] = []
    for event in events:
        markets = event.get("markets") or []
        for m in markets:
            # Merge event-level description if market description empty.
            if not m.get("description") and event.get("description"):
                m = {**m, "description": event.get("description")}
            if not m.get("slug") and event.get("slug"):
                m = {**m, "eventSlug": event.get("slug")}
            parsed = parse_weather_market(m, registry)
            if parsed is not None:
                out.append(parsed)
    return out


def run_parser_smoke(fixtures: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Parse fixtures or live Gamma sample; return dict rows for inspection."""
    registry = load_station_registry()
    if fixtures is None:
        events = fetch_gamma_weather_events(limit=30)
        parsed = parse_events(events, registry)
    else:
        parsed = [p for p in (parse_weather_market(m, registry) for m in fixtures) if p]
    return [p.as_dict() for p in parsed]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rows = run_parser_smoke()
    print(json.dumps(rows[:25], indent=2))
    print(f"parsed={len(rows)}")
