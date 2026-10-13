"""Venue-neutral market, quote, order, and position models (pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain.ids import MarketId

Side = Literal["BUY", "SELL"]
Leg = Literal["YES", "NO"]
Tif = Literal["DAY", "GTC"]
OrderType = Literal["LMT", "MKT"]


class Market(BaseModel):
    """One binary event market."""

    market_id: MarketId
    symbol: str = ""
    title: str = ""
    no_id: MarketId | None = None
    expiry: datetime | None = None
    scheduled_resolution_time: datetime | None = None
    strike: float | None = None
    tick_size: float | None = None
    size_increment: float | None = None
    multiplier: float = 1.0
    currency: str = "USD"
    venue_meta: dict = Field(default_factory=dict)


class Quote(BaseModel):
    market_id: MarketId
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    ts: datetime
    data_type: Literal["live", "delayed", "frozen", "snapshot"] = "live"

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


class OrderBookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    market_id: MarketId
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    ts: datetime

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None


class Position(BaseModel):
    market_id: MarketId
    leg: Leg
    quantity: float
    avg_price: float
    venue_meta: dict = Field(default_factory=dict)


class Balance(BaseModel):
    currency: str
    total: float
    available: float


class OrderRequest(BaseModel):
    """A venue-neutral order intent. The adapter translates it."""

    model_config = ConfigDict(frozen=True)

    market_id: MarketId
    side: Side
    leg: Leg
    quantity: float
    order_type: OrderType = "LMT"
    limit_price: float | None = None
    tif: Tif = "DAY"


class OrderAck(BaseModel):
    order_id: str
    accepted: bool
    detail: str = ""
    raw: dict = Field(default_factory=dict)


class Order(BaseModel):
    order_id: str
    request: OrderRequest
    status: Literal["pending", "open", "filled", "cancelled", "rejected"]
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    venue_meta: dict = Field(default_factory=dict)
