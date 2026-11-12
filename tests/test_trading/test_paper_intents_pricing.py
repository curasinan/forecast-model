"""run_ibkr_paper.load_intents: the limit, edge and EV come from the price actually paid."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.run_ibkr_paper as mod
from src.domain.fees import FlatCommissionFeeModel
from src.domain.ids import MarketId
from src.domain.models import Market

YES_CON, NO_CON = 900001, 900002


class FakeAdapter:
    def __init__(self, tick: float = 0.01):
        self.tick = tick
        self.markets_asked: list[int] = []

    def fee_model(self):
        return FlatCommissionFeeModel(0.01)

    def get_market(self, market_id: MarketId) -> Market:
        self.markets_asked.append(market_id.con_id)
        return Market(
            market_id=market_id, no_id=MarketId.ibkr(NO_CON), symbol="GTM",
            title="GTM Dec-2026 > 1.3", strike=1.3, size_increment=1.0, tick_size=self.tick,
            scheduled_resolution_time=datetime.now(timezone.utc) + timedelta(days=60),
        )


def _row(**over) -> dict:
    row = {
        "con_id": YES_CON, "target_month": "2026-12", "strike": 1.3, "p_model": 0.842,
        "bid": 0.41, "ask": 0.44, "yes_mid": 0.425, "no_bid": 0.55, "no_ask": 0.58,
        "no_mid": 0.565, "edge": 0.417, "half_kelly": 0.3, "suspect_quote": False,
        "tradeable": True,
    }
    row.update(over)
    return row


def _signals(tmp_path, *rows) -> str:
    path = tmp_path / "gtm_signals.json"
    path.write_text(json.dumps({"signals": list(rows)}), encoding="utf-8")
    return path


def test_ask_policy_prices_the_yes_leg_at_the_ask(tmp_path):
    intents = mod.load_intents(_signals(tmp_path, _row()), FakeAdapter(), None, 0.05)
    (intent,) = intents
    assert intent.edge.market_prob == pytest.approx(0.44)
    assert intent.edge.edge_pct == pytest.approx(0.842 - 0.44)
    assert intent.edge.expected_value == pytest.approx(0.842 - 0.44 - 0.01)
    assert intent.quote == {"policy": "ask", "ticks_below_ask": 0, "bid": 0.41, "ask": 0.44,
                            "limit": 0.44}


def test_ticks_below_ask_lowers_the_limit(tmp_path):
    (intent,) = mod.load_intents(_signals(tmp_path, _row()), FakeAdapter(), None, 0.05,
                                 ticks_below_ask=1)
    assert intent.edge.market_prob == pytest.approx(0.43)


def test_mid_policy_keeps_the_passive_bid(tmp_path):
    (intent,) = mod.load_intents(_signals(tmp_path, _row()), FakeAdapter(), None, 0.05,
                                 price_policy="mid")
    assert intent.edge.market_prob == pytest.approx(0.42)  # 0.425 snapped down


def test_edge_is_judged_at_the_ask_not_the_mid(tmp_path):
    # mid edge 0.06 passes a 0.05 floor, but paying the ask leaves 0.04
    row = _row(p_model=0.485, bid=0.41, ask=0.44, yes_mid=0.425, edge=0.06)
    adapter = FakeAdapter()
    assert mod.load_intents(_signals(tmp_path, row), adapter, None, 0.05) == []
    assert adapter.markets_asked == []  # refused before any venue call


def test_no_leg_prices_at_its_own_ask(tmp_path):
    row = _row(p_model=0.2, yes_mid=0.425, edge=-0.225)
    (intent,) = mod.load_intents(_signals(tmp_path, row), FakeAdapter(), None, 0.05)
    assert intent.no_price == pytest.approx(0.58)
    assert intent.edge.edge_pct == pytest.approx(-((1 - 0.2) - 0.58))
    assert intent.edge.market_prob == pytest.approx(1 - 0.58)
    assert intent.quote["ask"] == 0.58


def test_no_leg_without_its_quote_is_skipped(tmp_path):
    row = _row(p_model=0.2, edge=-0.225, no_bid=None, no_ask=None)
    assert mod.load_intents(_signals(tmp_path, row), FakeAdapter(), None, 0.05) == []


def test_suspect_and_untradeable_rows_stay_out(tmp_path):
    rows = [_row(suspect_quote=True), _row(tradeable=False)]
    assert mod.load_intents(_signals(tmp_path, *rows), FakeAdapter(), None, 0.05) == []


def test_a_coarser_market_tick_reprices(tmp_path):
    (intent,) = mod.load_intents(_signals(tmp_path, _row(ask=0.47)), FakeAdapter(tick=0.05),
                                 None, 0.05)
    assert intent.edge.market_prob == pytest.approx(0.45)


def test_stale_fee_facts_refuse_before_any_row_is_priced(tmp_path):
    class StaleFees(FakeAdapter):
        def fee_model(self):
            raise RuntimeError("fee model verified 45 days ago")

    with pytest.raises(RuntimeError, match="verified"):
        mod.load_intents(_signals(tmp_path, _row()), StaleFees(), None, 0.05)


def test_reprice_with_simulate_is_refused_before_anything_connects():
    assert mod.main(["--reprice", "--simulate"]) == 2


def test_negative_ticks_are_refused():
    assert mod.main(["--ticks-below-ask", "-1"]) == 2
