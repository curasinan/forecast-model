"""Polymarket read-only venue adapter and the sanctioned CLOB client factory."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from src.collectors.polymarket import PolymarketCollector
from src.domain.carry import CarryModel, NullCarry
from src.domain.fees import FeeModel, ZeroFeeModel
from src.domain.ids import POLYMARKET, MarketId
from src.domain.models import (
    Balance,
    Market,
    Order,
    OrderBook,
    Position,
    Quote,
)
from src.venues.base import VenueAdapter, VenueError, VenueReadOnlyError

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
HEALTH_TIMEOUT_SECONDS = 5.0


def readonly_clob_client(host: str = CLOB_HOST, chain_id: int = POLYGON_CHAIN_ID) -> Any:
    """Unauthenticated, read-only Polymarket CLOB client."""
    try:
        from py_clob_client.client import ClobClient
    except ImportError as exc:
        raise ImportError(
            "py_clob_client is not installed; the CLOB client is an optional "
            "dependency -- install with: pip install .[trading]"
        ) from exc
    return ClobClient(host, chain_id=chain_id)


class PolymarketAdapter(VenueAdapter):
    """Polymarket seen through the venue port. ALWAYS read-only."""

    name = POLYMARKET

    def __init__(self, collector: PolymarketCollector | None = None):
        super().__init__(read_only=True)  # forced; there is no writable mode
        self._collector = collector if collector is not None else PolymarketCollector()
        self._connected = False

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        """No-op: the Gamma API is plain HTTPS, there is no session."""
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_healthy(self) -> bool:
        """Cheap Gamma API ping; False on any failure."""
        try:
            resp = httpx.get(
                f"{self._collector.gamma_base}/markets",
                params={"limit": 1},
                timeout=HEALTH_TIMEOUT_SECONDS,
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    # -- market data -------------------------------------------------------
    def list_markets(self) -> list[Market]:
        raw = asyncio.run(self._collector.get_all_tech_markets())
        markets: list[Market] = []
        for m in raw:
            condition_id = m.get("condition_id", "")
            if not condition_id:
                continue
            markets.append(
                Market(
                    market_id=MarketId.polymarket(condition_id),
                    title=m.get("question", ""),
                    venue_meta={
                        "slug": m.get("slug", ""),
                        "yes_price": m.get("yes_price"),
                        "no_price": m.get("no_price"),
                        "volume": m.get("volume"),
                        "volume_24h": m.get("volume_24h"),
                        "liquidity": m.get("liquidity"),
                        "end_date": m.get("end_date", ""),
                    },
                )
            )
        return markets

    def get_market(self, market_id: MarketId) -> Market:
        condition_id = self._native(market_id)
        data = asyncio.run(self._collector.get_market_by_id(condition_id))
        if data is None:
            raise VenueError(f"polymarket market not found: {market_id}")
        return Market(
            market_id=MarketId.polymarket(data.market_id or condition_id),
            title="",  # Gamma MarketData carries no question text
            venue_meta={
                "slug": data.slug,
                "yes_price": data.yes_price,
                "no_price": data.no_price,
                "volume_24h": data.volume_24h,
                "total_volume": data.total_volume,
                "liquidity": data.liquidity,
            },
        )

    def get_order_book(self, market_id: MarketId, depth: int = 5) -> OrderBook:
        raise VenueError(
            "order book requires the CLOB client; use readonly_clob_client for research scripts"
        )

    def get_quotes(self, market_ids: Sequence[MarketId]) -> list[Quote]:
        """Gamma yes_price as a point-in-time snapshot (no bid/ask depth)."""
        quotes: list[Quote] = []
        for market_id in market_ids:
            condition_id = self._native(market_id)
            data = asyncio.run(self._collector.get_market_by_id(condition_id))
            if data is None:
                continue
            quotes.append(
                Quote(
                    market_id=market_id,
                    last=data.yes_price,
                    ts=data.last_updated or datetime.now(),
                    data_type="snapshot",
                )
            )
        return quotes

    def get_positions(self) -> list[Position]:
        return []

    def get_balances(self) -> list[Balance]:
        return []

    def get_open_orders(self) -> list[Order]:
        return []

    # -- orders ------------------------------------------------------------
    def cancel_order(self, order_id: str) -> bool:
        raise VenueReadOnlyError(
            f"{self.name} adapter is read-only; refusing to cancel order {order_id!r}"
        )

    # -- economics ---------------------------------------------------------
    def fee_model(self) -> FeeModel:
        return ZeroFeeModel()

    def carry_model(self) -> CarryModel:
        return NullCarry()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _native(market_id: MarketId) -> str:
        if market_id.venue != POLYMARKET:
            raise VenueError(f"{market_id} is not a polymarket market id")
        return market_id.native_id
