"""QuoteRecorder unit tests: row mapping, batching math, parquet and state writing."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pyarrow.parquet as pq

from src.venues.ibkr.recorder import (
    QUOTE_BATCH_SIZE,
    QuoteRecorder,
    batched,
    ticker_to_row,
    write_state,
    write_sweep_file,
)
from src.venues.ibkr.settings import IBKRSettings

FUTURE_EXPIRY = "20991231"
NOW = datetime(2026, 8, 16, 14, 30, 5, tzinfo=timezone.utc)
TS = NOW.isoformat()


def _settings(tmp_path) -> IBKRSettings:
    return IBKRSettings(
        _env_file=None, port=4002, trading_mode="paper", data_root=str(tmp_path / "root")
    )


def _ticker(con_id: int, **overrides) -> SimpleNamespace:
    fields = {
        "bid": 0.03, "ask": 0.06, "last": float("nan"), "close": 0.05,
        "bidSize": 10.0, "askSize": 20.0,
    }
    fields.update(overrides)
    return SimpleNamespace(contract=SimpleNamespace(conId=con_id), **fields)


def _meta(symbol: str = "GDP", right: str = "C", strike: float = 2.5) -> dict:
    return {"symbol": symbol, "expiry": FUTURE_EXPIRY, "strike": strike, "right": right}


def _opt_contract(con_id: int, right: str, symbol: str = "GDP", strike: float = 2.5) -> dict:
    suffix = "YES" if right == "C" else "NO"
    return {
        "conId": con_id,
        "symbol": symbol,
        "localSymbol": f"{symbol}_1226_{strike}_{suffix}",
        "secType": "OPT",
        "exchange": "FORECASTX",
        "currency": "USD",
        "tradingClass": symbol,
        "multiplier": "1",
        "lastTradeDateOrContractMonth": FUTURE_EXPIRY,
        "right": right,
        "strike": strike,
        "minTick": 0.01,
        "longName": "Test Contract",
    }


def _write_snapshot(tmp_path, contracts: dict) -> object:
    path = tmp_path / "universe.json"
    path.write_text(json.dumps({"contracts": contracts}), encoding="utf-8")
    return path


class TestBatching:
    def test_universe_batch_count(self):
        batches = batched(list(range(2900)))
        assert len(batches) == 58
        assert all(len(b) == QUOTE_BATCH_SIZE for b in batches)

    def test_remainder_batch(self):
        assert [len(b) for b in batched(list(range(101)))] == [50, 50, 1]

    def test_empty(self):
        assert batched([]) == []

    def test_order_preserved(self):
        items = list(range(120))
        assert [x for b in batched(items) for x in b] == items


class TestRowMapping:
    def test_maps_fields_and_meta(self):
        row = ticker_to_row(_ticker(1001), _meta(), TS, "delayed")
        assert row["ts_utc"] == TS
        assert row["con_id"] == 1001
        assert row["symbol"] == "GDP"
        assert row["expiry"] == FUTURE_EXPIRY
        assert row["strike"] == 2.5
        assert row["right"] == "C"
        assert row["bid"] == 0.03
        assert row["ask"] == 0.06
        assert row["close"] == 0.05
        assert row["bid_size"] == 10.0
        assert row["ask_size"] == 20.0
        assert row["data_type"] == "delayed"

    def test_nan_becomes_none(self):
        nan = float("nan")
        ticker = _ticker(1, bid=nan, ask=nan, last=nan, close=nan, bidSize=nan, askSize=nan)
        row = ticker_to_row(ticker, _meta(), TS, "delayed")
        for field in ("bid", "ask", "last", "close", "bid_size", "ask_size"):
            assert row[field] is None

    def test_negative_sentinel_recorded_raw(self):
        # Raw-capture layer: IB's -1 no-quote sentinel is preserved, not cleaned.
        row = ticker_to_row(_ticker(1, bid=-1.0), _meta(), TS, "delayed")
        assert row["bid"] == -1.0

    def test_missing_meta_uses_defaults(self):
        row = ticker_to_row(_ticker(7), {}, TS, "delayed")
        assert row["symbol"] == ""
        assert row["expiry"] == ""
        assert row["strike"] is None
        assert row["right"] == ""


class TestSweepFile:
    def test_write_and_read_back(self, tmp_path):
        rows = [
            ticker_to_row(_ticker(1001), _meta(), TS, "delayed"),
            ticker_to_row(_ticker(1002, bid=0.94, ask=0.97), _meta(right="P"), TS, "delayed"),
        ]
        path = write_sweep_file(rows, tmp_path, now=NOW)
        assert path == tmp_path / "quotes" / "date=2026-08-16" / "quotes-143005Z.parquet"
        table = pq.read_table(path)
        assert table.num_rows == 2
        back = table.to_pylist()
        assert back[0]["con_id"] == 1001
        assert back[0]["last"] is None  # nan -> None survives the round trip
        assert back[0]["bid"] == 0.03
        assert back[1]["right"] == "P"
        assert all(r["data_type"] == "delayed" for r in back)

    def test_empty_rows_write_full_schema(self, tmp_path):
        path = write_sweep_file([], tmp_path, now=NOW)
        table = pq.read_table(path)
        assert table.num_rows == 0
        assert "con_id" in table.column_names
        assert "ts_utc" in table.column_names


class TestState:
    def test_write_state_round_trip(self, tmp_path):
        state = {"ts": TS, "rows": 5, "sweep_seconds": 1.5, "universe_size": 10}
        path = write_state(tmp_path, state)
        assert path == tmp_path / "state" / "recorder_state.json"
        assert json.loads(path.read_text(encoding="utf-8")) == state

    def test_overwrites_previous_state(self, tmp_path):
        write_state(tmp_path, {"ts": TS, "rows": 1})
        path = write_state(tmp_path, {"ts": TS, "rows": 2})
        assert json.loads(path.read_text(encoding="utf-8"))["rows"] == 2


class FakeIB:
    """Answers reqTickers from canned tickers; records batches and data types."""

    def __init__(self, tickers_by_con_id: dict):
        self._tickers = tickers_by_con_id
        self.market_data_types: list[int] = []
        self.batches: list[list[int]] = []

    def reqMarketDataType(self, data_type: int) -> None:
        self.market_data_types.append(data_type)

    def reqTickers(self, *contracts):
        batch = [c.conId for c in contracts]
        self.batches.append(batch)
        assert all(c.exchange == "FORECASTX" for c in contracts)
        return [self._tickers[cid] for cid in batch if cid in self._tickers]

    def isConnected(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass


class TestRecordOnce:
    def test_full_cycle_writes_parquet_and_state(self, tmp_path):
        settings = _settings(tmp_path)
        snapshot_path = _write_snapshot(tmp_path, {
            "1001": _opt_contract(1001, "C"),
            "1002": _opt_contract(1002, "P"),
            "900": {"conId": 900, "symbol": "GDP", "secType": "IND",
                    "exchange": "FORECASTX", "currency": "USD"},
        })
        recorder = QuoteRecorder(settings, snapshot_path=snapshot_path)
        recorder.ib = FakeIB({
            1001: _ticker(1001),
            1002: _ticker(1002, bid=0.94, ask=0.97),
        })
        state = recorder.record_once()

        assert state["rows"] == 2
        assert state["universe_size"] == 2  # IND row excluded
        assert state["sweep_seconds"] >= 0.0
        files = list((settings.resolved_data_root / "quotes").rglob("*.parquet"))
        assert len(files) == 1
        back = pq.read_table(files[0]).to_pylist()
        assert sorted(r["con_id"] for r in back) == [1001, 1002]
        assert {r["symbol"] for r in back} == {"GDP"}
        assert {r["data_type"] for r in back} == {"delayed"}  # market_data_type 4
        state_file = settings.resolved_data_root / "state" / "recorder_state.json"
        assert json.loads(state_file.read_text(encoding="utf-8")) == state
        assert 4 in recorder.ib.market_data_types
        recorder.close()

    def test_sweep_respects_batch_size(self, tmp_path):
        settings = _settings(tmp_path)
        con_ids = list(range(2000, 2120))  # 120 contracts -> 50/50/20
        snapshot_path = _write_snapshot(tmp_path, {
            str(cid): _opt_contract(cid, "C", strike=float(cid)) for cid in con_ids
        })
        recorder = QuoteRecorder(settings, snapshot_path=snapshot_path)
        recorder.ib = FakeIB({cid: _ticker(cid) for cid in con_ids})
        rows, universe_size = recorder.sweep()

        assert universe_size == 120
        assert len(rows) == 120
        assert [len(b) for b in recorder.ib.batches] == [50, 50, 20]
        assert not any(math.isnan(r["bid"]) for r in rows if r["bid"] is not None)
        recorder.close()

    def test_empty_universe_writes_state_but_no_parquet(self, tmp_path):
        settings = _settings(tmp_path)
        recorder = QuoteRecorder(settings, snapshot_path=tmp_path / "missing.json")
        recorder.ib = FakeIB({})
        state = recorder.record_once()

        assert state["rows"] == 0
        assert state["universe_size"] == 0
        assert not (settings.resolved_data_root / "quotes").exists()
        assert (settings.resolved_data_root / "state" / "recorder_state.json").exists()
        recorder.close()


class TestUniverseSnapshotAging:
    def test_stale_snapshot_emits_warning_on_seed(self, tmp_path, caplog):
        settings = _settings(tmp_path)
        snapshot_path = _write_snapshot(tmp_path, {"1001": _opt_contract(1001, "C")})
        forty_days_ago = time.time() - 40 * 86400
        os.utime(snapshot_path, (forty_days_ago, forty_days_ago))
        recorder = QuoteRecorder(settings, snapshot_path=snapshot_path)

        with caplog.at_level(logging.WARNING, logger="src.venues.ibkr.recorder"):
            con_ids = recorder.universe()

        assert con_ids == [1001]  # seed behavior itself is unchanged
        msgs = [r.getMessage() for r in caplog.records]
        assert any("snapshot" in m.lower() and "40" in m for m in msgs)
        recorder.close()

    def test_fresh_snapshot_emits_no_staleness_warning(self, tmp_path, caplog):
        settings = _settings(tmp_path)
        snapshot_path = _write_snapshot(tmp_path, {"1001": _opt_contract(1001, "C")})
        recorder = QuoteRecorder(settings, snapshot_path=snapshot_path)

        with caplog.at_level(logging.WARNING, logger="src.venues.ibkr.recorder"):
            con_ids = recorder.universe()

        assert con_ids == [1001]
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("snapshot" in m.lower() for m in msgs)
        recorder.close()
