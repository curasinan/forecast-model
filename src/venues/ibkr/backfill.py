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

Two facts about the store that the planner and the writer rely on:

* a request reaches one session further back than the gap (LEAD_SESSIONS),
  and only bars on or after the first gap day are written;
* a bar is identified by (contract, instant, tick type) and is never written
  twice, so a plan can be run again over days that already hold files.
"""

from __future__ import annotations

import logging
import os
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
# IB counts a duration of "N D" in sessions, and a FORECASTX session opens the
# evening before its date (21:16 UTC, observed 2026-08-25 and 2026-10-04): N
# sessions ending at midnight start at that open, so the first calendar day
# comes back three bars long. One more session makes the first gap day whole.
LEAD_SESSIONS = 1
MAX_GAP_DAYS = MAX_DURATION_DAYS - LEAD_SESSIONS

BACKFILL_SCHEMA = pa.schema([
    ("ts_utc", pa.string()),        # bar START timestamp (IB's bar.date), UTC ISO-8601
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
    # First UTC date whose bars are wanted. The request itself reaches one
    # session further back (LEAD_SESSIONS); None keeps every bar that returns.
    first_date: date | None = None

    @property
    def duration_str(self) -> str:
        return f"{self.duration_days} D"

    def covers(self, day: str) -> bool:
        """True when a bar stamped on ``day`` ('YYYY-MM-DD') belongs to the gap."""
        return self.first_date is None or day >= self.first_date.isoformat()

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
    answered: int = 0                 # requests the venue answered (bars or none)
    bars: int = 0
    contracts_with_bars: int = 0
    empty_con_ids: list[int] = field(default_factory=list)
    failed: list[tuple[int, str, str]] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    skipped_con_ids: int = 0          # dropped by --max-requests, reported loudly
    dates_written: dict[str, int] = field(default_factory=dict)
    skipped_existing: int = 0         # bars the venue returned that were already stored


def recorded_dates(data_root: str | Path) -> set[date]:
    """UTC dates for which the sweep recorder wrote at least one file.

    A day is considered recorded when it has at least one parquet file
    under quotes/date=YYYY-MM-DD/. Empty partitions are not counted.
    """
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


def backfilled_dates(data_root: str | Path) -> set[date]:
    """UTC dates for which hourly backfill wrote at least one file.

    A day is considered backfilled when it has at least one parquet file
    under quotes_hourly/date=YYYY-MM-DD/. Empty partitions are not counted.
    Partial backfills (empty directories) do not count as done; the next
    backfill will refetch the day in full.
    """
    quotes_hourly = Path(data_root) / "quotes_hourly"
    found: set[date] = set()
    if not quotes_hourly.is_dir():
        return found
    for child in quotes_hourly.iterdir():
        if not child.name.startswith("date=") or not child.is_dir():
            continue
        if not any(child.glob("*.parquet")):
            continue
        try:
            found.add(date.fromisoformat(child.name[len("date="):]))
        except ValueError:
            logger.warning("ignoring unparseable partition %s", child.name)
    return found


def missing_dates(data_root: str | Path, start: date, end: date,
                  refetch: Iterable[date] = ()) -> list[date]:
    """Dates in [start, end] with no sweep file and not already backfilled, clipped to available history.

    A date is considered complete if it either:
    - Has at least one parquet file in quotes/date=YYYY-MM-DD/ (from sweep recorder), OR
    - Has at least one parquet file in quotes_hourly/date=YYYY-MM-DD/ (from historical backfill)

    Empty partitions (directories with no parquet files) are not considered complete
    and will be re-fetched. The day-granularity contract: a day with ANY hourly parquet
    counts as done; partial days are not re-fetched on their own.

    ``refetch`` is the way around that contract: a date named there counts as
    missing whatever it holds, as long as it lies inside the window. It is for
    a day a cut run left with some contracts only, and for the first day of a
    run made before the lead session was requested (three bars per contract).
    """
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    have = recorded_dates(data_root)
    backfilled = backfilled_dates(data_root)
    have = (have | backfilled) - set(refetch)  # either source counts as done
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
    if span > MAX_GAP_DAYS:
        raise ValueError(
            f"gap of {span} days exceeds the {MAX_GAP_DAYS}-day single-request "
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
    requests = [BackfillRequest(con_id=cid, what_to_show=tick, end_dt=end_dt,
                                duration_days=span + LEAD_SESSIONS, first_date=gap_start)
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
    """Map one ib_async BarData to a BACKFILL_SCHEMA row (UTC bar start time)."""
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


def _bar_key(con_id, ts_text: str, what_to_show: str) -> tuple:
    """Identity of one stored bar: contract, instant, tick type.

    The timestamp is compared as an instant, not as text, so the '+00:00' and
    'Z' spellings of one bar are one key.
    """
    try:
        # a date-only text is not an instant: keep it as text so it cannot
        # collide with the bar that starts at midnight
        ts = datetime.fromisoformat(ts_text) if "T" in ts_text else None
    except (TypeError, ValueError):
        ts = None
    if ts is None:
        return (int(con_id), ts_text, what_to_show)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (int(con_id), ts.astimezone(timezone.utc), what_to_show)


def stored_bar_keys(data_root: str | Path, day: str,
                    con_ids: Iterable[int] | None = None) -> set[tuple]:
    """Keys of the bars already stored under quotes_hourly/date=<day>/.

    ``con_ids`` narrows the read to the contracts a run is about to request;
    None reads every contract. Raises RuntimeError naming the file when one
    cannot be read or holds a row without a contract id: a run that adds to
    a day must know what that day already holds.
    """
    keys: set[tuple] = set()
    day_dir = Path(data_root) / "quotes_hourly" / f"date={day}"
    filters = None if con_ids is None else [("con_id", "in", sorted(set(con_ids)))]
    for path in sorted(day_dir.glob("*.parquet")):
        try:
            table = pq.read_table(path, columns=["con_id", "ts_utc", "what_to_show"],
                                  filters=filters)
            keys.update(_bar_key(con_id, ts, what) for con_id, ts, what in zip(
                table.column("con_id").to_pylist(), table.column("ts_utc").to_pylist(),
                table.column("what_to_show").to_pylist()))
        except Exception as exc:  # noqa: BLE001 -- any reader error is the finding
            raise RuntimeError(f"cannot read the stored hourly file {path}: {exc}") from exc
    return keys


def read_stored_window(data_root: str | Path,
                       requests: Sequence[BackfillRequest]) -> dict[str, set[tuple]]:
    """Stored keys of every day a plan can write to, for the contracts it requests.

    Meant to run before the first request: an unreadable file must stop a run
    while nothing has been asked of the venue, not at a flush with fetched
    rows in hand. Requests without a ``first_date`` have no window to read.
    """
    stored: dict[str, set[tuple]] = {}
    con_ids = {r.con_id for r in requests}
    windows = {(r.first_date, r.end_dt.astimezone(timezone.utc).date())
               for r in requests if r.first_date is not None}
    for first, end in windows:
        for offset in range((end - first).days):
            day = (first + timedelta(days=offset)).isoformat()
            if day not in stored:
                stored[day] = stored_bar_keys(data_root, day, con_ids)
    return stored


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
        # Written beside the final name and renamed in one step: a reader (a
        # later plan, the recorder's mirror push) never sees a torn file.
        partial = path.with_name(path.name + ".tmp")
        pq.write_table(pa.Table.from_pylist(day_rows, schema=BACKFILL_SCHEMA), partial)
        os.replace(partial, path)
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
        self._stored: dict[str, set[tuple]] = {}  # day -> keys on disk plus keys queued
        # (contract, tick type) pairs that already have bars in the window: an
        # empty answer for one of them is a failure (ib_async returns [] on a
        # timeout), never "no history".
        self._stored_series: set[tuple[int, str]] = set()
        # The counts of the last run, kept even when run() leaves by exception,
        # so the caller can still record an interrupted run.
        self.last_result: BackfillResult | None = None

    def _stored_keys(self, day: str) -> set[tuple]:
        if day not in self._stored:
            self._stored[day] = stored_bar_keys(self.data_root, day)
        return self._stored[day]

    def _not_yet_stored(self, kept: list[dict], result: BackfillResult) -> list[dict]:
        """Rows of ``kept`` that no stored or queued bar already covers."""
        fresh: list[dict] = []
        for row in kept:
            seen = self._stored_keys(_row_date(row))
            key = _bar_key(row["con_id"], row["ts_utc"], row["what_to_show"])
            if key in seen:
                result.skipped_existing += 1
                continue
            seen.add(key)  # queued rows count too: a repeated request must not double them
            fresh.append(row)
        return fresh

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
            skipped_con_ids: int = 0, flush_every: int = 25,
            stored: dict[str, set[tuple]] | None = None) -> BackfillResult:
        """Execute the plan, writing parquet per covered UTC date as it goes.

        ``stored`` is the window's stored keys as ``read_stored_window``
        returns them; a caller that read them for its plan passes them in,
        otherwise they are read here, before the first request.

        A full-universe pass is hours long under pacing and the Gateway
        restarts nightly, so rows are flushed every ``flush_every`` answered
        requests (and on any exception on the way out) rather than being held
        until the end -- an interrupted run keeps everything it had fetched.
        Each flush writes its own file suffix, so nothing is overwritten.

        Bars already stored for a day are not written again, so the same plan
        can be run twice: that is how a cut run is finished (``--refetch``)
        and how a day that holds only part of its bars is completed.
        """
        result = BackfillResult(requested=len(requests), skipped_con_ids=skipped_con_ids)
        self.last_result = result
        self._stored = (read_stored_window(self.data_root, requests) if stored is None
                        else stored)
        self._stored_series = {(key[0], key[2]) for keys in self._stored.values() for key in keys}
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
                    is_connected = getattr(self.ib, "isConnected", None)
                    if is_connected is not None and not is_connected():
                        # Every further request would fail the same way, one
                        # pacing wait at a time; stop and let the caller record it.
                        raise ConnectionError(
                            f"Gateway connection lost after {answered} answered "
                            f"request(s); {len(requests) - index} not attempted"
                        ) from exc
                    continue
                answered += 1
                result.answered = answered
                # The lead session's bars fall on the day before the gap; writing
                # them would mark that day as backfilled with three bars in it.
                kept = [row for row in (bar_to_row(bar, request, meta) for bar in bars)
                        if request.covers(_row_date(row))]
                if not kept and (request.con_id, request.what_to_show) in self._stored_series:
                    reason = ("no bars returned for a contract that has stored bars in "
                              "the window (a timed-out request answers this way)")
                    result.failed.append((request.con_id, request.what_to_show, reason))
                    logger.warning("backfill %d/%d conId=%s %s failed: %s",
                                   index, len(requests), request.con_id,
                                   request.what_to_show, reason)
                elif not kept:
                    empty.add(request.con_id)
                    logger.info("backfill %d/%d conId=%s %s: no bars",
                                index, len(requests), request.con_id, request.what_to_show)
                else:
                    with_bars.add(request.con_id)
                    fresh = self._not_yet_stored(kept, result)
                    rows.extend(fresh)
                    logger.info("backfill %d/%d conId=%s %s: %d bars (%d already stored)",
                                index, len(requests), request.con_id,
                                request.what_to_show, len(kept), len(kept) - len(fresh))
                if flush_every and answered % flush_every == 0:
                    flush()
        finally:
            flush()  # keep whatever was fetched, including on KeyboardInterrupt
            result.contracts_with_bars = len(with_bars)
            result.empty_con_ids = sorted(empty - with_bars)
        return result
