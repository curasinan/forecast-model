"""S2 quote replay: fills, latency, settlement, expiry and reprice through the real executor."""

from __future__ import annotations

import sqlite3
from datetime import date, time

import pandas as pd
import pytest

from climate.sim.panel import load_panel
from climate.sim.replay import (
    Policy,
    Replay,
    ReplayConfig,
    d1_verdict,
    decision_times,
    settlements,
    stop_times,
    target_months,
)
from climate.storage import VINTAGE_LIVE
from tests.test_climate.sim_replay_fixtures import (
    DEC_EXPIRY,
    DEC_QUOTES,
    SEP_EXPIRY,
    YES,
    StubForecaster,
    times,
    utc,
    write_ladder,
)

DEC_MODEL = {"2026-12": (1.425, 0.115)}


def _replay(ticks_root, tmp_path, cfg, forecaster, values=None, printed=None, ledger=None,
            start=None):
    panel = load_panel(ticks_root)
    contracts = panel.contracts()
    target_of = target_months(contracts, None)
    settle = settlements(target_of, values or {}, printed or {})
    stops = stop_times(contracts, target_of, ledger)
    replay = Replay(panel, contracts, None, forecaster, settle, stops, cfg, tmp_path / "work")
    return replay, replay.run(decision_times(panel, cadence="daily", at=time(14, 0), start=start))


def _orders(result) -> dict[float, pd.Series]:
    return {row.strike: row for _, row in result.orders.iterrows()}


@pytest.fixture
def dec_root(tmp_path):
    """Three days of a December ladder; 1.3's ask dips to 0.42 once on the 15th at 06:00."""
    root = tmp_path / "ticks"

    def quotes(ts):
        q = dict(DEC_QUOTES)
        if ts == utc(2026, 10, 15, 6, 0):
            q[1.3] = (0.40, 0.42, 50)
        return q

    write_ladder(root, times(utc(2026, 10, 14, 0, 0), utc(2026, 10, 16, 23, 40)), quotes, DEC_EXPIRY)
    return root


def test_ask_orders_fill_at_the_next_snapshot_at_the_ask(dec_root, tmp_path):
    cfg = ReplayConfig(policy=Policy.parse("ask"))
    _, result = _replay(dec_root, tmp_path, cfg, StubForecaster(DEC_MODEL))
    orders = _orders(result)
    assert set(orders) == {1.2, 1.3}  # 1.4 finds no room under the $15 month cap
    o13 = orders[1.3]
    assert o13["first_fill_ts"] == pd.Timestamp(utc(2026, 10, 14, 14, 20))
    assert o13["avg_fill"] == pytest.approx(0.44) and o13["filled"] == o13["qty"]
    assert o13["hours_to_fill"] == pytest.approx(1 / 3)
    assert result.metrics["fill_rate"] == 1.0
    assert result.metrics["reconcile_aborts"] == 0
    cost = sum(r["filled"] * (r["avg_fill"] + 0.01) for r in orders.values())
    assert result.metrics["capital_deployed"] == pytest.approx(cost)
    assert result.metrics["capital_deployed"] <= 15.0 + 0.5  # the cap binds on prices, fees on top
    open_bid = orders[1.3]["filled"] * 0.41 + orders[1.2]["filled"] * 0.62
    assert result.metrics["total_pnl_bid"] == pytest.approx(open_bid - cost)


def test_a_second_snapshot_of_latency_delays_the_fill(dec_root, tmp_path):
    cfg = ReplayConfig(policy=Policy.parse("ask"), latency=2)
    _, result = _replay(dec_root, tmp_path, cfg, StubForecaster(DEC_MODEL))
    assert _orders(result)[1.3]["first_fill_ts"] == pd.Timestamp(utc(2026, 10, 14, 14, 40))


def test_a_mid_bid_waits_for_the_ask_to_come_down(dec_root, tmp_path):
    cfg = ReplayConfig(policy=Policy.parse("mid"))
    _, result = _replay(dec_root, tmp_path, cfg, StubForecaster(DEC_MODEL))
    orders = _orders(result)
    assert orders[1.3]["limit"] == pytest.approx(0.42)
    assert orders[1.3]["first_fill_ts"] == pd.Timestamp(utc(2026, 10, 15, 6, 0))
    assert orders[1.2]["filled"] == 0 and orders[1.2]["status"] == "open"
    assert result.metrics["fill_rate"] < 1.0


def test_live_fills_take_only_the_displayed_size(tmp_path):
    root = tmp_path / "ticks"
    quotes = {**DEC_QUOTES, 1.3: (0.41, 0.44, 5)}
    write_ladder(root, times(utc(2026, 10, 14, 13, 0), utc(2026, 10, 14, 18, 0)),
                 lambda ts: quotes, DEC_EXPIRY)
    cfg = ReplayConfig(policy=Policy.parse("ask"), fill_model="live")
    replay, result = _replay(root, tmp_path, cfg, StubForecaster(DEC_MODEL))
    order = next(o for o in replay.venue.orders.values() if o.con_id == YES[1.3])
    assert [q for _, q, _ in order.fills][:4] == [5, 5, 5, 5]
    assert order.filled == order.request.quantity


def test_a_decision_without_a_fresh_snapshot_is_skipped(tmp_path):
    root = tmp_path / "ticks"
    stamps = [t for t in times(utc(2026, 10, 14, 0, 0), utc(2026, 10, 15, 23, 40))
              if not utc(2026, 10, 15, 13, 0) <= t <= utc(2026, 10, 15, 15, 0)]
    write_ladder(root, stamps, lambda ts: DEC_QUOTES, DEC_EXPIRY)
    _, result = _replay(root, tmp_path, ReplayConfig(policy=Policy.parse("ask")),
                        StubForecaster(DEC_MODEL))
    skipped = result.decisions.set_index("decision_ts")["skipped"]
    assert skipped[pd.Timestamp(utc(2026, 10, 15, 14, 0))] == "no fresh snapshot"


@pytest.fixture
def sep_root(tmp_path):
    """September's ladder from 10-05 to 10-11; 1.3's ask falls to 0.58 after trading stops."""
    root = tmp_path / "ticks"

    def quotes(ts):
        q = {1.2: (0.82, 0.85, 50), 1.3: (0.57, 0.60, 50)}
        if ts >= utc(2026, 10, 10, 6, 0):
            q[1.3] = (0.55, 0.58, 50)
        return q

    write_ladder(root, times(utc(2026, 10, 5, 0, 0), utc(2026, 10, 11, 0, 0)), quotes, SEP_EXPIRY)
    return root


SEP_SETTLE = {"values": {"2026-09": (1.30, VINTAGE_LIVE)},
              "printed": {"2026-09": utc(2026, 10, 10, 15, 0)}}


def test_settlement_pays_on_the_first_print_and_a_tie_pays_no(sep_root, tmp_path):
    cfg = ReplayConfig(policy=Policy.parse("ask"))
    replay, result = _replay(sep_root, tmp_path, cfg, StubForecaster({"2026-09": (1.40, 0.08)}),
                             **SEP_SETTLE)
    orders = _orders(result)
    assert orders[1.3]["settled"] and orders[1.2]["settled"]
    assert orders[1.3]["payout"] == 0.0  # the print 1.30 is not above 1.3
    assert orders[1.2]["payout"] == orders[1.2]["filled"]
    expected = sum(r["payout"] - r["filled"] * (r["avg_fill"] + 0.01) for r in orders.values())
    assert result.metrics["settled_pnl"] == pytest.approx(expected)
    month = result.months.set_index("target_month").loc["2026-09"]
    assert month["settled"] and month["pnl"] == pytest.approx(expected)
    conn = sqlite3.connect(replay.db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM positions WHERE closed_at IS NULL").fetchone()[0] == 0
        statuses = {s for (s,) in conn.execute("SELECT status FROM trade_log")}
    finally:
        conn.close()
    assert "placed" not in statuses
    assert result.metrics["reconcile_aborts"] == 0


def test_no_fill_after_trading_stops(sep_root, tmp_path):
    cfg = ReplayConfig(policy=Policy.parse("mid"))
    _, result = _replay(sep_root, tmp_path, cfg, StubForecaster({"2026-09": (1.40, 0.08)}),
                        **SEP_SETTLE)
    o13 = _orders(result)[1.3]
    assert o13["limit"] == pytest.approx(0.58)
    assert o13["filled"] == 0 and o13["close_reason"].startswith("expired")
    assert result.metrics["reconcile_aborts"] == 0


def test_no_order_inside_the_last_three_days(sep_root, tmp_path):
    # from 10-07 14:00 the stop at 10-10 00:00 is two days away: the risk check refuses
    _, result = _replay(sep_root, tmp_path, ReplayConfig(policy=Policy.parse("ask")),
                        StubForecaster({"2026-09": (1.40, 0.08)}), start=date(2026, 10, 7),
                        **SEP_SETTLE)
    assert result.orders.empty
    assert result.decisions["n_rejected"].sum() > 0


@pytest.fixture
def jump_root(tmp_path):
    """1.3's ask jumps from 0.44 to 0.47 right after the first decision's snapshot."""
    root = tmp_path / "ticks"

    def quotes(ts):
        q = dict(DEC_QUOTES)
        if ts > utc(2026, 10, 14, 14, 0):
            q[1.3] = (0.44, 0.47, 50)
        return q

    write_ladder(root, times(utc(2026, 10, 14, 13, 0), utc(2026, 10, 15, 18, 0)), quotes, DEC_EXPIRY)
    return root


def test_reprice_cancels_the_stranded_order_and_buys_at_the_new_ask(jump_root, tmp_path):
    cfg = ReplayConfig(policy=Policy.parse("ask"), reprice=True)
    replay, result = _replay(jump_root, tmp_path, cfg, StubForecaster(DEC_MODEL))
    on13 = [o for o in replay.venue.orders.values() if o.con_id == YES[1.3]]
    assert [o.status for o in on13] == ["cancelled", "filled"]
    assert on13[0].request.limit_price == pytest.approx(0.44) and on13[0].filled == 0
    assert on13[1].avg_price == pytest.approx(0.47)
    assert result.metrics["repriced"] == 1
    conn = sqlite3.connect(replay.db)
    try:
        cancelled = conn.execute("SELECT COUNT(*) FROM trade_log WHERE status = 'cancelled'").fetchone()[0]
    finally:
        conn.close()
    assert cancelled == 1


def test_without_reprice_the_stranded_order_stays_unfilled(jump_root, tmp_path):
    replay, _ = _replay(jump_root, tmp_path, ReplayConfig(policy=Policy.parse("ask")),
                        StubForecaster(DEC_MODEL))
    on13 = [o for o in replay.venue.orders.values() if o.con_id == YES[1.3]]
    assert len(on13) == 1 and on13[0].filled == 0 and on13[0].status == "open"


@pytest.mark.parametrize("skip", [False, True])
def test_skip_running_month_leaves_the_month_in_progress_alone(tmp_path, skip):
    root = tmp_path / "ticks"
    write_ladder(root, times(utc(2026, 10, 14, 13, 0), utc(2026, 10, 15, 18, 0)),
                 lambda ts: DEC_QUOTES, "20261111")  # October's ladder, decided in October
    cfg = ReplayConfig(policy=Policy.parse("ask"), skip_running_month=skip)
    _, result = _replay(root, tmp_path, cfg, StubForecaster({"2026-10": (1.42, 0.09)}))
    if skip:
        assert result.orders.empty
    else:
        assert len(result.orders) > 0 and (result.orders["lead_months"] == 0).all()


def test_policy_parsing():
    assert Policy.parse("ask-2") == Policy("ask-2", "ask", 2)
    assert Policy.parse("MID") == Policy("mid", "mid", 0)
    for bad in ("bid", "ask-", "ask-x", "ask+1"):
        with pytest.raises(ValueError):
            Policy.parse(bad)


def test_d1_verdict_follows_the_rule():
    def cell(policy, filled, pnl):
        return (ReplayConfig(policy=Policy.parse(policy)), {"orders_filled": filled, "total_pnl_bid": pnl})

    cells = dict(a=cell("ask", 10, 5.0), b=cell("ask-1", 8, 6.0), c=cell("ask-2", 5, 9.0))
    lines = d1_verdict(cells)
    assert lines[-1] == "D1 choice: ask-1."  # ask-2 has 5 < 0.8 x 8 fills
    cells["b"] = cell("ask-1", 7, 6.0)
    assert d1_verdict(cells)[-1] == "D1 choice: ask."
    assert "not evaluated" in d1_verdict({"x": cell("mid", 1, 1.0)})[0]


def test_stop_times_use_the_expiry_and_the_ledger(tmp_path):
    root = tmp_path / "ticks"
    write_ladder(root, times(utc(2026, 10, 5, 0, 0), utc(2026, 10, 5, 1, 0)),
                 lambda ts: {1.3: (0.57, 0.60, 50)}, SEP_EXPIRY)
    panel = load_panel(root)
    contracts = panel.contracts()
    target_of = target_months(contracts, None)
    assert stop_times(contracts, target_of, None)[YES[1.3]] == pd.Timestamp("2026-10-10", tz="UTC")
    ledger = pd.DataFrame([{"series": "gtm", "period": "2026-09", "value": 1.3,
                            "first_seen_utc": "2026-10-08T13:30:00+00:00", "vintage": VINTAGE_LIVE,
                            "is_revision": False, "source_url": ""}])
    assert stop_times(contracts, target_of, ledger)[YES[1.3]] == pd.Timestamp("2026-10-07 13:30", tz="UTC")
