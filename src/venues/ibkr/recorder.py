"""Full-universe snapshot-sweep recorder for ForecastEx delayed quotes.

Design: SNAPSHOT SWEEPS, not streaming. The account has ~100 market data
lines (account-level, shared across client ids) while the tradable
universe is ~2,900 contracts, so v1 sweeps the whole universe with
transient ``reqTickers`` snapshot batches (50 conIds per batch) every
``settings.record_interval_minutes``. Each batch releases its data lines
when the snapshot completes, so a sweep never holds more than one batch
worth of lines and needs no subscription manager. The cost is temporal
resolution: one delayed snapshot per contract per interval, no
intra-interval ticks. Streaming subscriptions for a prioritized subset
plus depth recording (reqMktDepth) is the follow-up when microstructure data
is needed; SubscriptionManager (src/venues/ibkr/subscriptions.py) provides the
line-budget accounting, priority eviction, and subscribe-rate limiter for it.

Layout deviation from the migration spec: parquet files are partitioned
by date ONLY (``quotes/date=YYYY-MM-DD/quotes-HHMMSSZ.parquet``) with
``con_id`` as an ordinary column, not by date+contract. One sweep covers
~2,900 conIds; per-conId partitioning would create thousands of tiny
files per sweep, which is pathological for filesystems and parquet
readers alike. One file per sweep keeps file count equal to sweeps/day,
and readers filter on the ``con_id`` column instead.

The universe comes from the contract cache, seeded from the Stage 0
snapshot (data/ibkr_universe_live.json) whenever the cache is empty:
at startup, after the 24h cache TTL lapses, and after the reconnect
flush (contracts.py policy). Live re-discovery of new listings is a
Stage 5 follow-up; until then the snapshot is the only universe source.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from src.venues.ibkr.contracts import ContractResolver
from src.venues.ibkr.gateway import ConnectionEvent, GatewaySession, append_event_jsonl
from src.venues.ibkr.settings import IBKRSettings

logger = logging.getLogger(__name__)

QUOTE_BATCH_SIZE = 50
_DATA_TYPE_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed"}
_LOGGING_CONFIGURED = False

SWEEP_SCHEMA = pa.schema([
    ("ts_utc", pa.string()),
    ("con_id", pa.int64()),
    ("symbol", pa.string()),
    ("expiry", pa.string()),
    ("strike", pa.float64()),
    ("right", pa.string()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("last", pa.float64()),
    ("close", pa.float64()),
    ("bid_size", pa.float64()),
    ("ask_size", pa.float64()),
    ("data_type", pa.string()),
])


def setup_logging(log_dir: str | Path = "logs") -> None:
    """Log to console AND <log_dir>/ibkr_recorder.log (ASCII only)."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    file_handler = logging.FileHandler(log_dir / "ibkr_recorder.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stream)
    root.addHandler(file_handler)
    _LOGGING_CONFIGURED = True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_event(event: ConnectionEvent) -> None:
    """Logging sink for GatewaySession events (JSONL sink handles structure)."""
    logger.warning("gateway event: kind=%s expected=%s downtime_s=%s detail=%s",
                   event.kind, event.expected, event.downtime_seconds, event.detail)


def _nan_to_none(value) -> float | None:
    """nan -> None; everything else (including IB's -1 sentinel) is recorded raw."""
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) else value


def batched(items: list, size: int = QUOTE_BATCH_SIZE) -> list[list]:
    """Split items into consecutive batches of at most ``size`` (2900 -> 58)."""
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def ticker_to_row(ticker, meta: dict, ts_utc: str, data_type: str) -> dict:
    """Map one ib_async Ticker (plus cached contract meta) to a sweep row."""
    return {
        "ts_utc": ts_utc,
        "con_id": int(ticker.contract.conId),
        "symbol": meta.get("symbol") or "",
        "expiry": meta.get("expiry") or "",
        "strike": meta.get("strike"),
        "right": meta.get("right") or "",
        "bid": _nan_to_none(ticker.bid),
        "ask": _nan_to_none(ticker.ask),
        "last": _nan_to_none(ticker.last),
        "close": _nan_to_none(ticker.close),
        "bid_size": _nan_to_none(ticker.bidSize),
        "ask_size": _nan_to_none(ticker.askSize),
        "data_type": data_type,
    }


def write_sweep_file(rows: list[dict], data_root: str | Path,
                     now: datetime | None = None) -> Path:
    """Write one sweep as a single parquet under quotes/date=YYYY-MM-DD/ (UTC)."""
    now = now or datetime.now(timezone.utc)
    out_dir = Path(data_root) / "quotes" / f"date={now:%Y-%m-%d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"quotes-{now:%H%M%S}Z.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SWEEP_SCHEMA), path)
    return path


def write_state(data_root: str | Path, state: dict) -> Path:
    """Atomically write the heartbeat state file (write-then-replace)."""
    state_dir = Path(data_root) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "recorder_state.json"
    tmp = state_dir / "recorder_state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


class QuoteRecorder:
    """Sweep the tradable universe on an interval and persist quote snapshots."""

    def __init__(self, settings: IBKRSettings,
                 snapshot_path: str | Path = "data/ibkr_universe_live.json"):
        self.settings = settings
        self.snapshot_path = Path(snapshot_path)
        self.resolver = ContractResolver(settings)
        self.session = GatewaySession(
            settings, "data_collector", connect_validator=self._validate_connection
        )
        # Listings may have changed while the session was down (contracts.py
        # reconnect policy); universe() re-seeds from the snapshot next sweep.
        self.session.on_reconnect(self.resolver.flush_all)
        self.session.on_event(_log_event)
        self.session.on_event(append_event_jsonl)  # default: logs/ibkr_events.jsonl
        self.ib = None

    # -- universe ------------------------------------------------------------
    def universe(self) -> list[int]:
        """Tradable conIds; re-seeds the cache from the snapshot when empty."""
        con_ids = self.resolver.tradable_con_ids()
        if not con_ids and self.snapshot_path.exists():
            seeded = self.resolver.seed_from_snapshot(self.snapshot_path)
            logger.info("seeded %d contracts from %s", seeded, self.snapshot_path)
            con_ids = self.resolver.tradable_con_ids()
        if not con_ids:
            logger.warning("universe is empty (snapshot missing, or all rows stale/expired)")
        return con_ids

    # -- connection ----------------------------------------------------------
    def _check_mode(self, ib) -> None:
        """Refuse to record data labeled with the wrong trading mode."""
        accounts = list(ib.managedAccounts())
        account = accounts[0] if accounts else ""
        mode = "paper" if account.startswith(("DU", "DF")) else "live"
        if not account or mode != self.settings.trading_mode:
            ib.disconnect()
            raise RuntimeError(
                f"connected account {account!r} looks like {mode} but IBKR_TRADING_MODE="
                f"{self.settings.trading_mode!r}; refusing to record mislabeled data"
            )
        logger.warning("ibkr session: account=%s mode=%s role=data_collector read_only=True",
                       account, mode)

    def _validate_connection(self, ib) -> None:
        """Runs inside GatewaySession after every successful (re)connect.

        A mode mismatch is a configuration error, not a connectivity blip:
        it raises out of the session's retry loop instead of being retried.
        """
        self._check_mode(ib)
        ib.reqMarketDataType(self.settings.market_data_type)

    # -- sweeping ------------------------------------------------------------
    def sweep(self) -> tuple[list[dict], int]:
        """One full-universe pass of transient snapshot batches (no file I/O)."""
        ib = self.ib
        if ib is None:
            raise RuntimeError("not connected; call run() or session.ensure_connected() first")
        from ib_async import Contract  # lazy: conId-only construction (spec Stage 3.2)

        ib.reqMarketDataType(self.settings.market_data_type)
        data_type = _DATA_TYPE_NAMES.get(self.settings.market_data_type, "delayed")
        con_ids = self.universe()
        meta = {cid: (self.resolver.get(cid) or {}) for cid in con_ids}
        rows: list[dict] = []
        for batch in batched(con_ids, QUOTE_BATCH_SIZE):
            contracts = [Contract(conId=cid, exchange="FORECASTX") for cid in batch]
            try:
                tickers = ib.reqTickers(*contracts)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if not ib.isConnected():
                    raise
                logger.warning("batch of %d failed (%s); continuing", len(batch), exc)
                continue
            ts = _utc_now_iso()
            for ticker in tickers:
                cid = int(ticker.contract.conId)
                rows.append(ticker_to_row(ticker, meta.get(cid, {}), ts, data_type))
        return rows, len(con_ids)

    def record_once(self) -> dict:
        """Sweep, write the parquet file (if any rows), write the heartbeat."""
        started = time.monotonic()
        rows, universe_size = self.sweep()
        path: Path | None = None
        if rows:
            path = write_sweep_file(rows, self.settings.resolved_data_root)
        sweep_seconds = round(time.monotonic() - started, 3)
        state = {
            "ts": _utc_now_iso(),
            "rows": len(rows),
            "sweep_seconds": sweep_seconds,
            "universe_size": universe_size,
        }
        write_state(self.settings.resolved_data_root, state)
        logger.info("sweep done: rows=%d universe=%d seconds=%.1f file=%s",
                    len(rows), universe_size, sweep_seconds, path)
        return state

    # -- lifecycle -----------------------------------------------------------
    def run(self, once: bool = False) -> None:
        """Sweep every record_interval_minutes until interrupted.

        Connection supervision is delegated to GatewaySession: health is
        probed (active heartbeat) before each sweep, and a mid-sweep
        connection exception simply loops back into ensure_connected(),
        which reconnects with backoff and replays the resubscription hooks.
        """
        interval_s = self.settings.record_interval_minutes * 60.0
        try:
            while True:
                started = time.monotonic()
                # Config errors (mode mismatch) propagate out loudly here.
                self.ib = self.session.ensure_connected()
                try:
                    self.record_once()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    logger.warning("sweep failed (%s); routing through gateway session", exc)
                    continue
                if once:
                    return
                remaining = interval_s - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            logger.info("keyboard interrupt: stopping recorder")
        finally:
            self.close()

    def close(self) -> None:
        self.session.close()
        self.ib = None
        self.resolver.close()
