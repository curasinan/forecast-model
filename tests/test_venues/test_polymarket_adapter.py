"""PolymarketAdapter: always read-only, Gamma data mapped to domain types."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.domain.carry import NullCarry
from src.domain.fees import ZeroFeeModel
from src.domain.ids import MarketId
from src.domain.models import OrderRequest
from src.models.question import MarketData
from src.venues.base import VenueError, VenueReadOnlyError
from src.venues.polymarket import (
    CLOB_HOST,
    POLYGON_CHAIN_ID,
    PolymarketAdapter,
    readonly_clob_client,
)

TS = datetime(2026, 8, 16, 12, 0, 0)


class FakeCollector:
    """Stands in for PolymarketCollector; no network."""

    gamma_base = "https://gamma-api.polymarket.com"

    def __init__(self, market_data: MarketData | None = None, markets: list[dict] | None = None):
        self._market_data = market_data
        self._markets = markets or []

    async def get_market_by_id(self, condition_id: str) -> MarketData | None:
        return self._market_data

    async def get_all_tech_markets(self, limit: int = 50) -> list[dict]:
        return self._markets


def _market_data() -> MarketData:
    return MarketData(
        market_id="0xdeadbeef",
        slug="will-x-happen",
        yes_price=0.62,
        no_price=0.38,
        volume_24h=1234.0,
        total_volume=99999.0,
        liquidity=500.0,
        last_updated=TS,
    )


def _order_request() -> OrderRequest:
    return OrderRequest(
        market_id=MarketId.polymarket("0xdeadbeef"),
        side="BUY",
        leg="YES",
        quantity=10.0,
        limit_price=0.5,
    )


def test_place_order_raises_even_after_connect():
    adapter = PolymarketAdapter(collector=FakeCollector())
    adapter.connect()
    with pytest.raises(VenueReadOnlyError):
        adapter.place_order(_order_request())


def test_cancel_order_raises():
    adapter = PolymarketAdapter(collector=FakeCollector())
    with pytest.raises(VenueReadOnlyError):
        adapter.cancel_order("order-1")


def test_adapter_is_always_read_only():
    adapter = PolymarketAdapter(collector=FakeCollector())
    assert adapter.read_only is True
    assert adapter.name == "polymarket"


def test_fee_model_is_zero_fee():
    adapter = PolymarketAdapter(collector=FakeCollector())
    fees = adapter.fee_model()
    assert isinstance(fees, ZeroFeeModel)
    assert fees.execution_cost(0.5, 100.0) == 0.0
    assert fees.yes_no_price_sum() == 1.0


def test_carry_model_is_null():
    adapter = PolymarketAdapter(collector=FakeCollector())
    carry = adapter.carry_model()
    assert isinstance(carry, NullCarry)
    assert carry.daily_accrual_rate() == 0.0


def test_get_quotes_maps_market_data(monkeypatch):
    monkeypatch.setattr(
        "src.venues.polymarket.PolymarketCollector",
        lambda: FakeCollector(market_data=_market_data()),
    )
    adapter = PolymarketAdapter()
    market_id = MarketId.polymarket("0xdeadbeef")
    quotes = adapter.get_quotes([market_id])
    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.market_id == market_id
    assert quote.last == 0.62
    assert quote.bid is None and quote.ask is None
    assert quote.ts == TS
    assert quote.data_type == "snapshot"


def test_get_quotes_skips_unknown_markets():
    adapter = PolymarketAdapter(collector=FakeCollector(market_data=None))
    assert adapter.get_quotes([MarketId.polymarket("0xmissing")]) == []


def test_get_quotes_rejects_foreign_venue_id():
    adapter = PolymarketAdapter(collector=FakeCollector())
    with pytest.raises(VenueError):
        adapter.get_quotes([MarketId.ibkr(12345)])


def test_get_market_maps_venue_meta():
    adapter = PolymarketAdapter(collector=FakeCollector(market_data=_market_data()))
    market = adapter.get_market(MarketId.polymarket("0xdeadbeef"))
    assert market.market_id == MarketId.polymarket("0xdeadbeef")
    assert market.venue_meta["slug"] == "will-x-happen"
    assert market.venue_meta["yes_price"] == 0.62
    assert market.venue_meta["no_price"] == 0.38


def test_get_market_unknown_raises():
    adapter = PolymarketAdapter(collector=FakeCollector(market_data=None))
    with pytest.raises(VenueError):
        adapter.get_market(MarketId.polymarket("0xmissing"))


def test_list_markets_maps_normalized_dicts():
    raw = [
        {
            "condition_id": "0xabc",
            "question": "Will X happen?",
            "slug": "will-x",
            "yes_price": 0.4,
            "no_price": 0.6,
            "volume": 100.0,
            "volume_24h": 10.0,
            "liquidity": 50.0,
            "end_date": "2026-12-31",
        },
        {"condition_id": "", "question": "no id, skipped"},
    ]
    adapter = PolymarketAdapter(collector=FakeCollector(markets=raw))
    markets = adapter.list_markets()
    assert len(markets) == 1
    assert markets[0].market_id == MarketId.polymarket("0xabc")
    assert markets[0].title == "Will X happen?"
    assert markets[0].venue_meta["slug"] == "will-x"


def test_get_order_book_directs_to_clob_factory():
    adapter = PolymarketAdapter(collector=FakeCollector())
    with pytest.raises(VenueError, match="readonly_clob_client"):
        adapter.get_order_book(MarketId.polymarket("0xdeadbeef"))


def test_account_methods_return_empty():
    adapter = PolymarketAdapter(collector=FakeCollector())
    assert adapter.get_positions() == []
    assert adapter.get_balances() == []
    assert adapter.get_open_orders() == []


def test_is_healthy_true_on_200(monkeypatch):
    class _Resp:
        status_code = 200

    monkeypatch.setattr("src.venues.polymarket.httpx.get", lambda *a, **k: _Resp())
    adapter = PolymarketAdapter(collector=FakeCollector())
    assert adapter.is_healthy() is True


def test_is_healthy_false_on_error(monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr("src.venues.polymarket.httpx.get", _boom)
    adapter = PolymarketAdapter(collector=FakeCollector())
    assert adapter.is_healthy() is False


def test_readonly_clob_client_raises_import_error_when_absent(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "py_clob_client", None)
    monkeypatch.setitem(sys.modules, "py_clob_client.client", None)
    with pytest.raises(ImportError, match=r"pip install \.\[trading\]"):
        readonly_clob_client()


def test_readonly_clob_client_constructs_unauthenticated_client():
    pytest.importorskip("py_clob_client")
    client = readonly_clob_client(CLOB_HOST, chain_id=POLYGON_CHAIN_ID)
    assert type(client).__name__ == "ClobClient"
