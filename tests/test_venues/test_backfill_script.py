"""scripts/backfill_ibkr_hourly.py run end to end against a temporary data root.

The Gateway is the only thing replaced: ``GatewaySession`` hands out a fake
``ib`` that answers ``reqHistoricalData``. The contract cache, the planner,
the backfiller, the parquet files and the run ledger are the real ones.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

import scripts.backfill_ibkr_hourly as cli

BASE_ARGS = ["--port", "4002", "--mode", "paper"]


def _contract(con_id: int, symbol: str, right: str) -> dict:
    # No expiry on purpose: the cache treats it as unexpired on any date.
    return {"conId": con_id, "symbol": symbol, "secType": "OPT",
            "lastTradeDateOrContractMonth": "", "strike": 1.2, "right": right,
            "localSymbol": f"{symbol}_{right}", "tradingClass": symbol, "multiplier": "1",
            "minTick": 0.01, "currency": "USD", "exchange": "FORECASTX", "longName": symbol}


@pytest.fixture
def ticks_root(tmp_path, monkeypatch):
    """An empty data root, a three-contract universe, no repo ``.env``, no log file."""
    snapshot = tmp_path / "universe.json"
    snapshot.write_text(json.dumps({"contracts": {
        "101": _contract(101, "GTM", "C"),
        "102": _contract(102, "GTM", "P"),
        "201": _contract(201, "CPI", "C"),  # outside the climate lane
    }}), encoding="utf-8")
    root = tmp_path / "ticks"
    monkeypatch.setenv("IBKR_DATA_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "SNAPSHOT", snapshot)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    return root


def _bar(month: int, day: int, hour: int, minute: int = 0):
    return SimpleNamespace(date=datetime(2026, month, day, hour, minute, tzinfo=timezone.utc),
                           open=0.4, high=0.5, low=0.3, close=0.45, volume=0)


class FakeIB:
    def __init__(self, bars_by_con_id: dict):
        self.bars_by_con_id = bars_by_con_id
        self.calls: list[dict] = []
        self.interrupt_on: int | None = None  # conId whose request raises KeyboardInterrupt

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqHistoricalData(self, contract, **kwargs):
        if contract.conId == self.interrupt_on:
            raise KeyboardInterrupt
        self.calls.append({"conId": contract.conId, **kwargs})
        return self.bars_by_con_id.get(contract.conId, [])


def _sweep_days(root, days: list[str]) -> None:
    for day in days:
        d = root / "quotes" / f"date={day}"
        d.mkdir(parents=True)
        (d / "quotes-120000Z.parquet").write_bytes(b"stub")


def _stored_days(root, days: list[str]) -> None:
    for day in days:
        d = root / "quotes_hourly" / f"date={day}"
        d.mkdir(parents=True)
        (d / "hourly-earlier-run.parquet").write_bytes(b"stub")  # exists, is not parquet


@pytest.fixture
def gateway(monkeypatch):
    """Replace the Gateway session; returns the fake ``ib`` the script will use."""
    ib = FakeIB({
        # the lead session (evening of the 2nd) and two bars of the gap day
        101: [_bar(9, 2, 21, 16), _bar(9, 2, 22), _bar(9, 3, 0), _bar(9, 3, 1)],
        102: [],
    })
    session = SimpleNamespace(ensure_connected=lambda: ib, close=lambda: None)
    monkeypatch.setattr(cli, "GatewaySession",
                        lambda settings, role, connect_validator=None: session)
    return ib


def _ledger(root) -> list[dict]:
    lines = (root / "backfill_runs.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _stored(root, day: str) -> list[tuple[int, str]]:
    rows = []
    for path in (root / "quotes_hourly" / f"date={day}").glob("*.parquet"):
        table = pq.read_table(path, columns=["con_id", "ts_utc"])
        rows.extend(zip(table.column("con_id").to_pylist(), table.column("ts_utc").to_pylist()))
    return sorted(rows)


def test_the_default_window_is_the_longest_one_a_single_request_can_fill(ticks_root, capsys):
    # Nothing is recorded in the empty root, so every day of the window is missing.
    rc = cli.main(["--dry-run", *BASE_ARGS])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "Missing dates (29)" in out
    assert "2 contracts x 1 tick type(s) = 2 requests" in out  # the CPI contract is out of scope


def test_a_longer_gap_is_refused_with_the_chunk_size_that_works(ticks_root, capsys):
    rc = cli.main(["--dry-run", *BASE_ARGS, "--start", "2026-08-01", "--end", "2026-08-30"])
    out = capsys.readouterr().out

    assert rc == 4
    assert "at most 29 days" in out and "Scope:" not in out


def test_refetch_puts_a_stored_day_back_into_the_plan(ticks_root, capsys):
    from src.venues.ibkr.backfill import BAR_SIZE, write_backfill_rows

    three_bars_of_a_first_day = {
        "ts_utc": "2026-09-03T21:16:00+00:00", "con_id": 101, "symbol": "GTM",
        "expiry": "", "strike": 1.2, "right": "C", "what_to_show": "BID_ASK",
        "bar_size": BAR_SIZE, "open": 0.4, "high": 0.5, "low": 0.3, "close": 0.45,
        "volume": 0.0, "source": "reqHistoricalData"}
    write_backfill_rows([three_bars_of_a_first_day], ticks_root, "earlier")
    window = ["--start", "2026-08-26", "--end", "2026-09-03"]

    assert cli.main(["--dry-run", *BASE_ARGS, *window]) == 0
    assert "Missing dates (8): 2026-08-26 .. 2026-09-02" in capsys.readouterr().out

    assert cli.main(["--dry-run", *BASE_ARGS, *window, "--refetch", "2026-09-03"]) == 0
    out = capsys.readouterr().out
    assert "Missing dates (9): 2026-08-26 .. 2026-09-03" in out
    assert "Already stored in the window: 1 bars, 1 contracts" in out


def test_a_window_that_ends_today_or_later_is_refused(ticks_root, capsys):
    today = datetime.now(timezone.utc).date().isoformat()
    rc = cli.main(["--dry-run", *BASE_ARGS, "--start", "2026-09-01", "--end", today])
    out = capsys.readouterr().out

    assert rc == 2
    assert today in out and "Scope:" not in out


def test_a_reversed_window_is_refused_as_such(ticks_root, capsys):
    rc = cli.main(["--dry-run", *BASE_ARGS, "--start", "2026-09-05", "--end", "2026-09-01",
                   "--refetch", "2026-09-03"])
    out = capsys.readouterr().out

    assert rc == 2
    assert "before" in out and "outside" not in out


def test_a_refetch_date_before_the_available_history_is_refused(ticks_root, capsys):
    rc = cli.main(["--dry-run", *BASE_ARGS, "--start", "2026-01-10", "--end", "2026-01-20",
                   "--refetch", "2026-01-15"])
    out = capsys.readouterr().out

    assert rc == 2
    assert "2026-01-15" in out and "2026-01-17" in out


def test_the_dry_run_states_the_request_and_the_sweep_days_it_will_fill(ticks_root, capsys):
    _sweep_days(ticks_root, ["2026-08-27", "2026-08-28"])
    rc = cli.main(["--dry-run", *BASE_ARGS, "--start", "2026-08-26", "--end", "2026-09-02"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Missing dates (6): 2026-08-26 .. 2026-09-02" in out
    assert "9 D ending 2026-09-03 00:00 UTC" in out
    assert "2026-08-27, 2026-08-28" in out


def test_a_span_widened_by_refetch_over_the_ceiling_names_the_cause(ticks_root, capsys):
    stored = [f"2026-08-{day:02d}" for day in range(1, 32)] + [f"2026-09-{day:02d}" for day in range(1, 9)]
    # every hourly file is a stub: the plan must refuse before it reads any of them
    _stored_days(ticks_root, stored)
    rc = cli.main(["--dry-run", *BASE_ARGS, "--start", "2026-08-01", "--end", "2026-09-09",
                   "--refetch", "2026-08-01"])
    out = capsys.readouterr().out

    assert rc == 4
    assert "--refetch" in out


def test_an_unreadable_stored_file_is_found_by_the_dry_run(ticks_root, capsys):
    _stored_days(ticks_root, ["2026-09-03"])
    rc = cli.main(["--dry-run", *BASE_ARGS, "--start", "2026-09-03", "--end", "2026-09-03",
                   "--refetch", "2026-09-03"])
    out = capsys.readouterr().out

    assert rc == 5
    assert "hourly-earlier-run.parquet" in out


def test_an_unreadable_stored_file_stops_a_real_run_before_it_connects(ticks_root, gateway, capsys):
    _stored_days(ticks_root, ["2026-09-03"])
    rc = cli.main([*BASE_ARGS, "--start", "2026-09-03", "--end", "2026-09-03",
                   "--refetch", "2026-09-03"])

    assert rc == 5
    assert gateway.calls == []
    assert "hourly-earlier-run.parquet" in capsys.readouterr().out


def test_a_second_run_is_refused_while_another_holds_the_lock(ticks_root, gateway, capsys):
    from src.trading.locks import acquire_db_lock

    (ticks_root / "quotes_hourly").mkdir(parents=True)
    with acquire_db_lock(ticks_root / "quotes_hourly" / "backfill"):
        rc = cli.main([*BASE_ARGS, "--start", "2026-09-03", "--end", "2026-09-03"])
    out = capsys.readouterr().out

    assert rc == 6
    assert "another" in out and gateway.calls == []


def test_an_interrupted_run_records_itself_as_interrupted(ticks_root, gateway):
    gateway.interrupt_on = 102
    rc = cli.main([*BASE_ARGS, "--start", "2026-09-03", "--end", "2026-09-03"])

    assert rc == 130
    (record,) = _ledger(ticks_root)
    assert record["interrupted"] is True
    assert record["bars"] == 2 and record["gap_start"] == "2026-09-03" and record["refetch"] == []


def test_a_refetch_date_outside_the_window_is_refused(ticks_root, capsys):
    rc = cli.main(["--dry-run", *BASE_ARGS, "--start", "2026-08-26", "--end", "2026-09-02",
                   "--refetch", "2026-09-03"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "2026-09-03" in captured.out + captured.err
    assert "Scope:" not in captured.out


def test_a_run_writes_the_gap_day_only_and_records_itself(ticks_root, gateway):
    rc = cli.main([*BASE_ARGS, "--start", "2026-09-03", "--end", "2026-09-03"])

    assert rc == 0
    assert [call["durationStr"] for call in gateway.calls] == ["2 D", "2 D"]
    assert _stored(ticks_root, "2026-09-03") == [
        (101, "2026-09-03T00:00:00+00:00"), (101, "2026-09-03T01:00:00+00:00")]
    assert not (ticks_root / "quotes_hourly" / "date=2026-09-02").exists()
    (record,) = _ledger(ticks_root)
    assert record["requested"] == 2 and record["bars"] == 2
    assert record["contracts_with_bars"] == 1 and record["contracts_without_history"] == 1
    assert record["dates_written"] == {"2026-09-03": 2}
    assert record["skipped_existing"] == 0 and record["refetch"] == []
    assert record["empty_con_ids"] == [102] and record["failed_requests"] == []
    assert record["interrupted"] is False


def test_a_refetch_run_adds_nothing_that_is_already_stored(ticks_root, gateway, capsys):
    day = ["--start", "2026-09-03", "--end", "2026-09-03"]
    assert cli.main([*BASE_ARGS, *day]) == 0
    capsys.readouterr()

    # without --refetch the stored day is done and the venue is not asked again
    calls_before = len(gateway.calls)
    assert cli.main([*BASE_ARGS, *day]) == 0
    assert "nothing to do" in capsys.readouterr().out and len(gateway.calls) == calls_before

    assert cli.main([*BASE_ARGS, *day, "--refetch", "2026-09-03"]) == 0
    assert "Already stored  : 2 bars" in capsys.readouterr().out
    assert len(_stored(ticks_root, "2026-09-03")) == 2  # still two bars, no duplicate
    first, second = _ledger(ticks_root)
    assert second["bars"] == 0 and second["skipped_existing"] == 2
    assert second["refetch"] == ["2026-09-03"]
