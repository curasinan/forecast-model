"""Hourly-bar backfill for gaps in the snapshot-sweep record (Stage 5 follow-up).

The sweep recorder (recorder.py) only captures what it is running for: any
window in which the Gateway was logged out is simply missing from
``quotes/date=YYYY-MM-DD/``. The Stage 5 probe
(docs/stage5_probe_findings.md) established that ForecastEx serves NO daily
bars (error 162, EODChart) but DOES serve 1-hour BID_ASK / MIDPOINT bars back
to 2026-01-17, so those gaps are recoverable after the fact.

Provenance is kept explicit rather than merged into the sweep store:

* backfilled bars land under ``quotes_hourly/date=YYYY-MM-DD/`` -- a
  SEPARATE tree from ``quotes/``. They are hourly OHLC aggregates, not
  point-in-time snapshots, and must never be read as if they were sweeps.
* every row carries ``what_to_show`` and ``source='reqHistoricalData'``.
* for BID_ASK bars IB packs the fields as open=time-average bid,
  high=max ask, low=min bid, close=time-average ask (NOT a price OHLC).
  The column names stay IB's; ``bid_ask_columns()`` documents the mapping.

Pacing is mandatory: violations can lock the session out, so every request
goes through HistoricalPacer, which blocks rather than dropping.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from src.venues.ibkr.pacing import HistoricalPacer, RequestKey

logger = logging.getLogger(__name__)

EXCHANGE = "FORECASTX"
BAR_SIZE = "1 hour"
# Earliest hourly history observed on FORECASTX (docs/stage5_probe_findings.md).
HISTORY_STARTS = date(2026, 1, 17)
# IB caps a single 1-hour-bar request; one month per request is the documented
# ceiling and also the largest gap worth recovering in one pass.
MAX_DURATION_DAYS = 30

BACKFILL_SCHEMA = pa.schema([
    ("ts_utc", pa.string()),        # bar END timestamp, UTC ISO-8601
    ("con_id", pa.int64()),
    ("symbol", pa.string()),
    ("expiry", pa.string()),
    ("strike", pa.float64()),
    ("right", pa.string()),
    ("what_to_show", pa.string()),  # BID_ASK | MIDPOINT | TRADES
    ("bar_size", pa.string()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("source", pa.string()),
])


def bid_ask_columns() -> dict[str, str]:
    """IB's field packing for BID_ASK bars (not an OHLC price series)."""
    return {"open": "time-average bid", "high": "max ask",
            "low": "min bid", "close": "time-average ask"}


@dataclass(frozen=True)
class BackfillRequest:
    """One reqHistoricalData call: whole gap for one contract and tick type."""

    con_id: int
    what_to_show: str
    end_dt: datetime          # UTC, exclusive right edge of the window
    duration_days: int

    @property
    def duration_str(self) -> str:
        return f"{self.duration_days} D"

    @property
    def end_date_time(self) -> str:
        """IB endDateTime in explicit UTC form ('YYYYMMDD-HH:MM:SS')."""
        return self.end_dt.astimezone(timezone.utc).strftime("%Y%m%d-%H:%M:%S")

    def pacing_key(self) -> RequestKey:
        fingerprint = (f"{self.con_id}|{EXCHANGE}|{self.what_to_show}|"
                       f"{self.duration_str}|{BAR_SIZE}|{self.end_date_time}")
        return RequestKey(contract_key=str(self.con_id), exchange=EXCHANGE,
                          tick_type=self.what_to_show, request_fingerprint=fingerprint)


@dataclass
class BackfillResult:
    """What one run actually did -- reported, never silently summarised away."""

    requested: int = 0
    bars: int = 0
    contracts_with_bars: int = 0
    empty_con_ids: list[int] = field(default_factory=list)
    failed: list[tuple[int, str, str]] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    skipped_con_ids: int = 0          # dropped by --max-requests, reported loudly
    dates_written: dict[str, int] = field(default_factory=dict)


def recorded_dates(data_root: str | Path) -> set[date]:
    """UTC dates for which the sweep recorder wrote at least one file."""
    quotes = Path(data_root) / "quotes"
    found: set[date] = set()
    if not quotes.is_dir():
        return found
    for child in quotes.iterdir():
        if not child.name.startswith("date=") or not child.is_dir():
            continue
        if not any(child.glob("*.parquet")):
            continue
        try:
            found.add(date.fromisoformat(child.name[len("date="):]))
        except ValueError:
            logger.warning("ignoring unparseable partition %s", child.name)
    return found


def missing_dates(data_root: str | Path, start: date, end: date) -> list[date]:
    """Dates in [start, end] with no sweep file, clipped to available history."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    have = recorded_dates(data_root)
    start = max(start, HISTORY_STARTS)
    days = (end - start).days
    return [d for i in range(days + 1)
            if (d := start + timedelta(days=i)) not in have]


def plan_requests(con_ids: Sequence[int], gap_start: date, gap_end: date,
                  what_to_show: Sequence[str] = ("BID_ASK",),
                  max_requests: int | None = None) -> tuple[list[BackfillRequest], int]:
    """One request per (contract, tick type) spanning the whole gap.

    Returns (requests, skipped_contracts). Trimming happens on whole
    contracts so no contract comes back half-covered; the count of dropped
    contracts is returned for the caller to report.
    """
    if gap_end < gap_start:
        raise ValueError(f"gap_end {gap_end} is before gap_start {gap_start}")
    span = (gap_end - gap_start).days + 1
    if span > MAX_DURATION_DAYS:
        raise ValueError(
            f"gap of {span} days exceeds the {MAX_DURATION_DAYS}-day single-request "
            "ceiling for 1-hour bars; backfill it in chunks"
        )
    # End at the start of the day AFTER gap_end so the last day is included
    # whole; IB treats endDateTime as the exclusive right edge.
    end_dt = datetime.combine(gap_end + timedelta(days=1),
                              datetime.min.time(), tzinfo=timezone.utc)
    ticks = list(what_to_show)
    per_contract = len(ticks)
    keep = list(con_ids)
    skipped = 0
    if max_requests is not None:
        capacity = max_requests // per_contract if per_contract else 0
        if capacity < len(keep):
            skipped = len(keep) - capacity
            keep = keep[:capacity]
    requests = [BackfillRequest(con_id=cid, what_to_show=tick,
                                end_dt=end_dt, duration_days=span)
                for cid in keep for tick in ticks]
    return requests, skipped


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else value  # NaN -> None


def bar_to_row(bar, request: BackfillRequest, meta: dict) -> dict:
    """Map one ib_async BarData to a BACKFILL_SCHEMA row (UTC bar end time)."""
    ts = bar.date
    if isinstance(ts, datetime):
        ts_utc = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        ts_text = ts_utc.isoformat()
    else:  # date, or a string when formatDate leaves it unparsed
        ts_text = str(ts)
    return {
        "ts_utc": ts_text,
        "con_id": int(request.con_id),
        "symbol": meta.get("symbol") or "",
        "expiry": meta.get("expiry") or "",
        "strike": _float_or_none(meta.get("strike")),
        "right": meta.get("right") or "",
        "what_to_show": request.what_to_show,
        "bar_size": BAR_SIZE,
        "open": _float_or_none(getattr(bar, "open", None)),
        "high": _float_or_none(getattr(bar, "high", None)),
        "low": _float_or_none(getattr(bar, "low", None)),
        "close": _float_or_none(getattr(bar, "close", None)),
        "volume": _float_or_none(getattr(bar, "volume", None)),
        "source": "reqHistoricalData",
    }


def _row_date(row: dict) -> str:
    text = row["ts_utc"]
    return text[:10] if len(text) >= 10 else "unknown"


def write_backfill_rows(rows: Iterable[dict], data_root: str | Path,
                        run_id: str) -> dict[str, Path]:
    """Write rows into quotes_hourly/date=YYYY-MM-DD/, one file per date."""
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        by_date.setdefault(_row_date(row), []).append(row)
    written: dict[str, Path] = {}
    for day, day_rows in sorted(by_date.items()):
        out_dir = Path(data_root) / "quotes_hourly" / f"date={day}"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"hourly-{run_id}.parquet"
        pq.write_table(pa.Table.from_pylist(day_rows, schema=BACKFILL_SCHEMA), path)
        written[day] = path
    return written


class HistoricalBackfiller:
    """Runs paced reqHistoricalData calls and persists the bars.

    Read-only by construction: it issues historical-data requests and writes
    parquet, nothing else. ``ib`` is any object exposing ib_async's
    ``qualifyContracts`` / ``reqHistoricalData`` (tests inject a fake).
    """

    def __init__(self, ib, resolver, data_root: str | Path,
                 pacer: HistoricalPacer | None = None):
        self.ib = ib
        self.resolver = resolver
        self.data_root = Path(data_root)
        self.pacer = pacer or HistoricalPacer()

    def fetch(self, request: BackfillRequest) -> list:
        """One paced request; raises whatever the API raises (caller records it)."""
        from ib_async import Contract  # lazy: conId-only construction (spec Stage 3.2)

        self.pacer.acquire(request.pacing_key())
        contract = Contract(conId=request.con_id, exchange=EXCHANGE)
        self.ib.qualifyContracts(contract)
        return list(self.ib.reqHistoricalData(
            contract,
            endDateTime=request.end_date_time,
            durationStr=request.duration_str,
            barSizeSetting=BAR_SIZE,
            whatToShow=request.what_to_show,
            useRTH=False,
            formatDate=2,  # UTC epoch-based; no local-timezone ambiguity
        ) or [])

    def run(self, requests: Sequence[BackfillRequest], run_id: str,
            skipped_con_ids: int = 0, flush_every: int = 25) -> BackfillResult:
        """Execute the plan, writing parquet per covered UTC date as it goes.

        A full-universe pass is hours long under pacing and the Gateway
        restarts nightly, so rows are flushed every ``flush_every`` answered
        requests (and on any exception on the way out) rather than being held
        until the end -- an interrupted run keeps everything it had fetched.
        Each flush writes its own file suffix, so nothing is overwritten.
        """
        result = BackfillResult(requested=len(requests), skipped_con_ids=skipped_con_ids)
        rows: list[dict] = []
        with_bars: set[int] = set()
        empty: set[int] = set()
        answered = 0

        def flush() -> None:
            nonlocal rows
            if not rows:
                return
            suffix = f"{run_id}-{len(result.files):03d}"
            written = write_backfill_rows(rows, self.data_root, suffix)
            for day in written:
                result.dates_written[day] = (result.dates_written.get(day, 0)
                                             + sum(1 for r in rows if _row_date(r) == day))
            result.files.extend(written.values())
            result.bars += len(rows)
            rows = []

        try:
            for index, request in enumerate(requests, start=1):
                meta = self.resolver.get(request.con_id) or {}
                try:
                    bars = self.fetch(request)
                except Exception as exc:  # noqa: BLE001 -- one dead contract must not end the run
                    result.failed.append((request.con_id, request.what_to_show, repr(exc)))
                    logger.warning("backfill %d/%d conId=%s %s failed: %s",
                                   index, len(requests), request.con_id,
                                   request.what_to_show, exc)
                    continue
                answered += 1
                if not bars:
                    empty.add(request.con_id)
                    logger.info("backfill %d/%d conId=%s %s: no bars",
                                index, len(requests), request.con_id, request.what_to_show)
                else:
                    with_bars.add(request.con_id)
                    rows.extend(bar_to_row(bar, request, meta) for bar in bars)
                    logger.info("backfill %d/%d conId=%s %s: %d bars",
                                index, len(requests), request.con_id,
                                request.what_to_show, len(bars))
                if flush_every and answered % flush_every == 0:
                    flush()
        finally:
            flush()  # keep whatever was fetched, including on KeyboardInterrupt
            result.contracts_with_bars = len(with_bars)
            result.empty_con_ids = sorted(empty - with_bars)
        return result
