"""gtm_sim_replay.py: the grid, the run directory, the usage errors."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from scripts.research import gtm_sim_replay
from src.trading.gtm_intents import intents_from_payload
from tests.test_climate.sim_replay_fixtures import (
    DEC_EXPIRY,
    DEC_QUOTES,
    StubForecaster,
    times,
    utc,
    write_ladder,
)


@pytest.fixture
def ticks(tmp_path, monkeypatch):
    root = tmp_path / "ticks"
    write_ladder(root, times(utc(2026, 10, 14, 13, 0), utc(2026, 10, 15, 16, 0)),
                 lambda ts: DEC_QUOTES, DEC_EXPIRY)
    monkeypatch.setattr(gtm_sim_replay, "build_forecaster",
                        lambda *a, **k: StubForecaster({"2026-12": (1.425, 0.115)}))
    return root


def _args(tmp_path, ticks, *more):
    return ["--root", str(tmp_path / "climate"), "--ticks-root", str(ticks),
            "--universe-json", str(tmp_path / "none.json"), "--out", str(tmp_path / "run"), *more]


def test_the_grid_writes_one_run_directory(tmp_path, ticks):
    code = gtm_sim_replay.main(_args(tmp_path, ticks, "--policies", "ask,ask-1,mid",
                                     "--fill", "paper,live", "--latency", "1,2"))
    assert code == 0
    run = tmp_path / "run"
    assert {p.name for p in run.iterdir()} == {"config.json", "metrics.json", "grid.parquet",
                                               "report.md", "cells"}
    grid = pd.read_parquet(run / "grid.parquet")
    assert len(grid) == 3 * 2 * 2
    cell = run / "cells" / "ask_lat1_paper"
    assert {p.name for p in cell.iterdir()} == {"decisions.parquet", "orders.parquet",
                                                "months.parquet", "equity.parquet", "paper_book.db"}
    metrics = json.loads((run / "metrics.json").read_text())
    assert metrics["d1"][-1].startswith("D1 choice:")
    assert "D1, the limit rule" in (run / "report.md").read_text()
    assert not list(tmp_path.glob(".run*"))  # no scratch left behind


def test_reprice_and_running_month_switches_widen_the_grid(tmp_path, ticks):
    assert gtm_sim_replay.main(_args(tmp_path, ticks, "--policies", "ask", "--fill", "paper",
                                     "--reprice", "both", "--skip-running-month", "both")) == 0
    cells = set(pd.read_parquet(tmp_path / "run" / "grid.parquet")["cell"])
    assert cells == {"ask_lat1_paper", "ask_lat1_paper_reprice", "ask_lat1_paper_norunning",
                     "ask_lat1_paper_reprice_norunning"}


@pytest.mark.parametrize("bad", [["--policies", "bid"], ["--fill", "maybe"], ["--latency", "0"]])
def test_usage_errors_exit_2(tmp_path, ticks, bad):
    assert gtm_sim_replay.main(_args(tmp_path, ticks, *bad)) == 2
    assert not (tmp_path / "run").exists()


def test_no_quotes_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(gtm_sim_replay, "build_forecaster", lambda *a, **k: None)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert gtm_sim_replay.main(_args(tmp_path, empty)) == 2


def test_an_existing_run_directory_is_refused(tmp_path, ticks):
    (tmp_path / "run").mkdir()
    assert gtm_sim_replay.main(_args(tmp_path, ticks)) == 2


def test_intents_count_days_from_the_replay_clock():
    from tests.test_trading.test_paper_intents_pricing import FakeAdapter, _row

    now = datetime(2026, 10, 1, tzinfo=timezone.utc)

    class Clocked(FakeAdapter):
        def get_market(self, market_id):
            market = super().get_market(market_id)
            return market.model_copy(update={"scheduled_resolution_time": now + timedelta(days=40)})

    (intent,) = intents_from_payload({"signals": [_row()]}, Clocked(), None, 0.05, now=now)
    assert intent.days_to_resolution == 40
    (live,) = intents_from_payload({"signals": [_row()]}, Clocked(), None, 0.05)
    assert live.days_to_resolution is None


def test_the_real_forecaster_and_ledger_settle_a_whole_contract_life(tmp_path, synthetic_root):
    """CLI end to end on the synthetic SEAS5 store and ledger: August 2010, quoted from July."""
    from scipy.stats import norm

    from tests.test_climate.sim_replay_fixtures import write_sweep

    strikes = (0.3, 0.4, 0.5)
    yes = dict(zip(strikes, (811, 812, 813)))
    quotes = {}
    for k in strikes:  # a cool, wide market: N(0.30, 0.20) under a 0.93 ceiling
        mid = 0.93 * norm.sf((k - 0.30) / 0.20)
        quotes[k] = (round(mid - 0.015, 2), round(mid + 0.015, 2), 50)
    ticks = tmp_path / "ticks"
    for ts in times(utc(2010, 7, 20, 0, 0), utc(2010, 9, 13, 18, 0), minutes=360):
        write_sweep(ticks, ts, quotes, "20100910", yes_ids=yes)
    code = gtm_sim_replay.main(["--root", str(synthetic_root), "--ticks-root", str(ticks),
                                "--universe-json", str(tmp_path / "none.json"),
                                "--out", str(tmp_path / "run"), "--policies", "ask",
                                "--fill", "paper", "--at", "12:20"])
    assert code == 0
    cell = tmp_path / "run" / "cells" / "ask_lat1_paper"
    months = pd.read_parquet(cell / "months.parquet").set_index("target_month")
    assert months.loc["2010-08", "settled"]
    assert months.loc["2010-08", "label"] == pytest.approx(0.442279, abs=1e-6)
    decisions = pd.read_parquet(cell / "decisions.parquet")
    assert (decisions["skipped"] == "").any()
    assert decisions["labels_through"].dropna().max() >= "2010-06"
    orders = pd.read_parquet(cell / "orders.parquet")
    assert len(orders) > 0 and orders["settled"].all()
