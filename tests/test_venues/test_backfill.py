"""Hourly-bar backfill: gap detection, request planning, pacing, persistence."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from src.venues.ibkr.backfill import (
    BAR_SIZE,
    HISTORY_STARTS,
    BackfillRequest,
    HistoricalBackfiller,
    backfilled_dates,
    bar_to_row,
    missing_dates,
    plan_requests,
    recorded_dates,
    write_backfill_rows,
)
from src.venues.ibkr.pacing import HistoricalPacer


def make_sweep_dirs(root, days: list[str], empty: list[str] = ()):
    for day in days:
        d = root / "quotes" / f"date={day}"
        d.mkdir(parents=True)
        (d / "quotes-120000Z.parquet").write_bytes(b"stub")
    for day in empty:  # partition exists but holds no sweep file
        (root / "quotes" / f"date={day}").mkdir(parents=True)


def make_backfilled_dirs(root, days: list[str], empty: list[str] = ()):
    """Create quotes_hourly/date=D/ with parquet files (or empty dirs if in empty list)."""
    for day in days:
        d = root / "quotes_hourly" / f"date={day}"
        d.mkdir(parents=True)
        (d / "hourly-run1.parquet").write_bytes(b"stub")
    for day in empty:  # partition exists but holds no backfill file
        (root / "quotes_hourly" / f"date={day}").mkdir(parents=True)


class TestGapDetection:
    def test_recorded_dates_ignores_empty_partitions(self, tmp_path):
        make_sweep_dirs(tmp_path, ["2026-08-16", "2026-08-17"], empty=["2026-08-18"])
        assert recorded_dates(tmp_path) == {date(2026, 8, 16), date(2026, 8, 17)}

    def test_recorded_dates_on_missing_tree(self, tmp_path):
        assert recorded_dates(tmp_path / "nope") == set()

    def test_backfilled_dates_finds_hourly_parquets(self, tmp_path):
        make_backfilled_dirs(tmp_path, ["2026-08-16", "2026-08-17"])
        assert backfilled_dates(tmp_path) == {date(2026, 8, 16), date(2026, 8, 17)}

    def test_backfilled_dates_ignores_empty_partitions(self, tmp_path):
        make_backfilled_dirs(tmp_path, ["2026-08-16"], empty=["2026-08-17"])
        assert backfilled_dates(tmp_path) == {date(2026, 8, 16)}

    def test_backfilled_dates_on_missing_tree(self, tmp_path):
        assert backfilled_dates(tmp_path / "nope") == set()

    def test_missing_dates_finds_the_hole(self, tmp_path):
        make_sweep_dirs(tmp_path, ["2026-08-16", "2026-08-17", "2026-08-23"])
        gaps = missing_dates(tmp_path, date(2026, 8, 16), date(2026, 8, 24))
        assert gaps == [date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20),
                        date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 24)]

    def test_missing_dates_clips_to_available_history(self, tmp_path):
        gaps = missing_dates(tmp_path, date(2025, 12, 1), HISTORY_STARTS)
        assert gaps == [HISTORY_STARTS]  # nothing requested before 2026-01-17

    def test_reversed_window_is_an_error(self, tmp_path):
        with pytest.raises(ValueError):
            missing_dates(tmp_path, date(2026, 8, 20), date(2026, 8, 10))

    def test_missing_dates_excludes_backfilled_dates(self, tmp_path):
        # Day D is in quotes_hourly but NOT in quotes
        # missing_dates should not re-plan D since it's already backfilled
        make_backfilled_dirs(tmp_path, ["2026-08-18"])
        gaps = missing_dates(tmp_path, date(2026, 8, 16), date(2026, 8, 20))
        # All days in range are missing from quotes, but 2026-08-18 is backfilled,
        # so it should not appear in the missing list
        assert date(2026, 8, 18) not in gaps
        assert date(2026, 8, 16) in gaps  # not backfilled
        assert date(2026, 8, 19) in gaps  # not backfilled
        assert date(2026, 8, 20) in gaps  # not backfilled

    def test_missing_dates_ignores_empty_backfill_partitions(self, tmp_path):
        # An empty quotes_hourly/date=D/ (no parquet) doesn't count as backfilled
        make_backfilled_dirs(tmp_path, [], empty=["2026-08-18"])
        gaps = missing_dates(tmp_path, date(2026, 8, 16), date(2026, 8, 20))
        # All days are missing, including 2026-08-18 (empty partition)
        assert date(2026, 8, 18) in gaps

    def test_missing_dates_with_missing_backfill_tree(self, tmp_path):
        # If quotes_hourly/ doesn't exist at all, behavior is unchanged
        gaps = missing_dates(tmp_path, date(2026, 8, 16), date(2026, 8, 20))
        # All days in range should be in gaps
        expected = [date(2026, 8, 16), date(2026, 8, 17), date(2026, 8, 18),
                    date(2026, 8, 19), date(2026, 8, 20)]
        assert gaps == expected


class TestPlanning:
    def test_one_request_per_contract_and_tick_spanning_the_gap(self):
        reqs, skipped = plan_requests([1, 2], date(2026, 8, 18), date(2026, 8, 25),
                                      what_to_show=("BID_ASK", "MIDPOINT"))
        assert skipped == 0 and len(reqs) == 4
        assert {r.duration_str for r in reqs} == {"8 D"}
        # endDateTime is the exclusive right edge: midnight AFTER the last gap day
        assert {r.end_date_time for r in reqs} == {"20260826-00:00:00"}
        assert [r.con_id for r in reqs] == [1, 1, 2, 2]

    def test_max_requests_drops_whole_contracts_and_reports(self):
        reqs, skipped = plan_requests([1, 2, 3], date(2026, 8, 18), date(2026, 8, 19),
                                      what_to_show=("BID_ASK", "MIDPOINT"),
                                      max_requests=4)
        assert skipped == 1
        assert {r.con_id for r in reqs} == {1, 2}  # never a half-covered contract

    def test_gap_over_the_single_request_ceiling_is_refused(self):
        with pytest.raises(ValueError, match="ceiling"):
            plan_requests([1], date(2026, 6, 1), date(2026, 8, 25))

    def test_pacing_key_weights_bid_ask_double(self):
        bid_ask = BackfillRequest(1, "BID_ASK", datetime(2026, 8, 26, tzinfo=timezone.utc), 8)
        mid = BackfillRequest(1, "MIDPOINT", datetime(2026, 8, 26, tzinfo=timezone.utc), 8)
        assert bid_ask.pacing_key().weight == 2
        assert mid.pacing_key().weight == 1
        assert bid_ask.pacing_key().request_fingerprint != mid.pacing_key().request_fingerprint


class TestRowMapping:
    def test_bar_to_row_normalises_to_utc_and_carries_provenance(self):
        req = BackfillRequest(830396294, "BID_ASK",
                              datetime(2026, 8, 26, tzinfo=timezone.utc), 8)
        bar = SimpleNamespace(date=datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc),
                              open=0.51, high=0.55, low=0.50, close=0.54, volume=-1.0)
        row = bar_to_row(bar, req, {"symbol": "GTM", "expiry": "20261231",
                                    "strike": 1.2, "right": "C"})
        assert row["ts_utc"].startswith("2026-08-18T14:00:00")
        assert row["con_id"] == 830396294 and row["symbol"] == "GTM"
        assert row["what_to_show"] == "BID_ASK" and row["bar_size"] == BAR_SIZE
        assert row["source"] == "reqHistoricalData"
        assert row["open"] == 0.51 and row["close"] == 0.54

    def test_naive_bar_timestamps_are_treated_as_utc(self):
        req = BackfillRequest(1, "MIDPOINT", datetime(2026, 8, 26, tzinfo=timezone.utc), 1)
        row = bar_to_row(SimpleNamespace(date=datetime(2026, 8, 18, 9, 0)), req, {})
        assert row["ts_utc"].startswith("2026-08-18T09:00:00+00:00")

    def test_write_splits_by_utc_date(self, tmp_path):
        req = BackfillRequest(7, "BID_ASK", datetime(2026, 8, 20, tzinfo=timezone.utc), 2)
        rows = [bar_to_row(SimpleNamespace(date=datetime(2026, 8, d, h, tzinfo=timezone.utc),
                                           open=0.4, high=0.5, low=0.3, close=0.45, volume=0),
                           req, {"symbol": "GTM"})
                for d in (18, 19) for h in (10, 11)]
        written = write_backfill_rows(rows, tmp_path, "run1")
        assert set(written) == {"2026-08-18", "2026-08-19"}
        table = pq.read_table(written["2026-08-18"])
        assert table.num_rows == 2
        assert table.column("source").to_pylist() == ["reqHistoricalData"] * 2


class FakeIB:
    """Minimal reqHistoricalData surface; ``bars_by`` keys on (conId, tick)."""

    def __init__(self, bars_by: dict, fail: set = frozenset()):
        self.bars_by = bars_by
        self.fail = fail
        self.calls: list[dict] = []

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqHistoricalData(self, contract, **kwargs):
        key = (contract.conId, kwargs["whatToShow"])
        self.calls.append({"conId": contract.conId, **kwargs})
        if key in self.fail:
            raise RuntimeError("HMDS query returned no data")
        return self.bars_by.get(key, [])


class FakeResolver:
    def __init__(self, meta: dict):
        self.meta = meta

    def get(self, con_id: int):
        return self.meta.get(con_id)


def _bars(day: int, count: int = 2):
    return [SimpleNamespace(date=datetime(2026, 8, day, 10 + i, tzinfo=timezone.utc),
                            open=0.4, high=0.5, low=0.3, close=0.45, volume=0)
            for i in range(count)]


class TestRun:
    def test_writes_bars_and_reports_empty_and_failed(self, tmp_path):
        reqs, _ = plan_requests([11, 12, 13], date(2026, 8, 18), date(2026, 8, 18))
        ib = FakeIB({(11, "BID_ASK"): _bars(18)}, fail={(13, "BID_ASK")})
        resolver = FakeResolver({11: {"symbol": "GTM", "right": "C"}})
        result = HistoricalBackfiller(ib, resolver, tmp_path).run(reqs, "runX")

        assert result.requested == 3 and result.bars == 2
        assert result.contracts_with_bars == 1
        assert result.empty_con_ids == [12]
        assert [f[0] for f in result.failed] == [13]
        assert result.dates_written == {"2026-08-18": 2}
        # one flush -> one file, suffixed so later flushes cannot overwrite it
        assert (tmp_path / "quotes_hourly" / "date=2026-08-18"
                / "hourly-runX-000.parquet").exists()

    def test_requests_carry_utc_end_and_no_rth_filter(self, tmp_path):
        reqs, _ = plan_requests([11], date(2026, 8, 18), date(2026, 8, 19))
        ib = FakeIB({})
        HistoricalBackfiller(ib, FakeResolver({}), tmp_path).run(reqs, "runY")
        call = ib.calls[0]
        assert call["endDateTime"] == "20260820-00:00:00"
        assert call["durationStr"] == "2 D" and call["barSizeSetting"] == BAR_SIZE
        assert call["useRTH"] is False and call["formatDate"] == 2

    def test_every_request_goes_through_the_pacer(self, tmp_path):
        reqs, _ = plan_requests([11, 12], date(2026, 8, 18), date(2026, 8, 18),
                                what_to_show=("BID_ASK", "MIDPOINT"))
        acquired: list = []
        pacer = HistoricalPacer()
        original = pacer.acquire
        pacer.acquire = lambda key: (acquired.append(key), original(key))[1]
        HistoricalBackfiller(ib := FakeIB({}), FakeResolver({}), tmp_path, pacer=pacer)\
            .run(reqs, "runZ")
        assert len(acquired) == len(ib.calls) == 4
        assert len({k.request_fingerprint for k in acquired}) == 4

    def test_no_file_written_when_nothing_came_back(self, tmp_path):
        reqs, _ = plan_requests([11], date(2026, 8, 18), date(2026, 8, 18))
        result = HistoricalBackfiller(FakeIB({}), FakeResolver({}), tmp_path).run(reqs, "runN")
        assert result.files == [] and not (tmp_path / "quotes_hourly").exists()

    def test_rows_are_flushed_during_the_run_not_only_at_the_end(self, tmp_path):
        con_ids = list(range(1, 6))
        reqs, _ = plan_requests(con_ids, date(2026, 8, 18), date(2026, 8, 18))
        ib = FakeIB({(cid, "BID_ASK"): _bars(18) for cid in con_ids})
        result = HistoricalBackfiller(ib, FakeResolver({}), tmp_path)\
            .run(reqs, "runF", flush_every=2)
        # 5 answered requests, flush every 2 -> 2 mid-run flushes + the final one
        assert len(result.files) == 3
        assert result.bars == 10 and result.dates_written == {"2026-08-18": 10}
        assert len({p.name for p in result.files}) == 3  # no file overwrites another

    def test_interrupted_run_keeps_what_it_already_fetched(self, tmp_path):
        reqs, _ = plan_requests([1, 2, 3], date(2026, 8, 18), date(2026, 8, 18))

        class Interrupting(FakeIB):
            def reqHistoricalData(self, contract, **kwargs):
                if contract.conId == 3:
                    raise KeyboardInterrupt
                return super().reqHistoricalData(contract, **kwargs)

        ib = Interrupting({(1, "BID_ASK"): _bars(18), (2, "BID_ASK"): _bars(18)})
        backfiller = HistoricalBackfiller(ib, FakeResolver({}), tmp_path)
        with pytest.raises(KeyboardInterrupt):
            backfiller.run(reqs, "runI", flush_every=100)
        written = list((tmp_path / "quotes_hourly" / "date=2026-08-18").glob("*.parquet"))
        assert len(written) == 1 and pq.read_table(written[0]).num_rows == 4
