"""SimVenue: a VenueAdapter that fills orders against recorded quotes, for the replay."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from climate.sim.panel import SWEEP, Contract, QuotePanel
from src.domain.carry import CarryModel, NullCarry
from src.domain.fees import FeeModel, FlatCommissionFeeModel
from src.domain.ids import MarketId
from src.domain.models import (
    Balance,
    Market,
    Order,
    OrderAck,
    OrderBook,
    OrderBookLevel,
    OrderRequest,
    Position,
    Quote,
)
from src.venues.base import VenueAdapter, VenueError

FILL_MODELS = ("paper", "live")
PRICE_TOL = 1e-9


@dataclass
class SimOrder:
    """One order at the simulated venue and everything that happened to it."""

    order_id: str
    perm_id: int
    request: OrderRequest
    placed_at: pd.Timestamp
    active_from: int  # first panel index the order may fill on
    status: str = "open"  # open | filled | cancelled
    filled: float = 0.0
    cost: float = 0.0  # sum of fill qty x fill price
    fills: list[tuple[pd.Timestamp, float, float]] = field(default_factory=list)
    closed_at: pd.Timestamp | None = None
    close_reason: str = ""

    @property
    def con_id(self) -> int:
        return self.request.market_id.con_id

    @property
    def remaining(self) -> float:
        return self.request.quantity - self.filled

    @property
    def avg_price(self) -> float | None:
        return self.cost / self.filled if self.filled > 0 else None


class SimVenue(VenueAdapter):
    """Fills BUY limits against the panel's later snapshots; nothing leaves the process.

    An order placed at a decision may fill from the `latency`-th snapshot after
    the decision's snapshot on, never on the snapshot the decision saw, and
    never at or after its leg's stop time in `resolution_times`, when it
    expires. A
    snapshot whose ask is at or below the limit fills at that ask. 'paper'
    fills the whole remainder, as an engine without a queue would; 'live'
    fills at most the displayed ask size per snapshot, shared among the
    orders on that leg, and nothing on hourly bars, which carry no size.
    """

    name = "sim"

    def __init__(self, panel: QuotePanel, contracts: dict[int, Contract],
                 resolution_times: dict[int, datetime], *, fill_model: str = "paper",
                 latency: int = 1, fee: float = 0.01):
        if fill_model not in FILL_MODELS:
            raise ValueError(f"fill_model must be one of {FILL_MODELS}")
        if latency < 1:
            raise ValueError("latency must be at least one snapshot")
        super().__init__(read_only=False)
        self.panel = panel
        self.contracts = contracts
        self.resolution_times = resolution_times
        self.fill_model = fill_model
        self.latency = int(latency)
        self.fee = float(fee)
        self.now: pd.Timestamp | None = None
        self.decision_index = -1
        self.orders: dict[str, SimOrder] = {}
        self.blocked_by_size = 0
        self._processed = -1
        self._next_id = 1
        self._next_perm = 7_000_001
        self._settled: set[int] = set()

    # -- clock ---------------------------------------------------------------
    def set_clock(self, now: datetime, decision_ts: pd.Timestamp | None) -> None:
        """Decisions from here on are made at `now`, on the snapshot `decision_ts`."""
        self.now = pd.Timestamp(now)
        self.decision_index = (self.panel.index_at_or_before(decision_ts)
                               if decision_ts is not None else self.panel.index_at_or_before(now))

    def advance(self, to: datetime) -> int:
        """Process every snapshot up to `to`; returns the number of fills."""
        last = self.panel.index_at_or_before(to)
        n = 0
        for i in range(self._processed + 1, last + 1):
            n += self._process(i)
        self._processed = max(self._processed, last)
        return n

    def _process(self, i: int) -> int:
        ts = self.panel.times[i]
        shown: dict[int, float] = {}
        n = 0
        for order in sorted(self.orders.values(), key=lambda o: o.perm_id):
            if order.status != "open" or order.active_from > i:
                continue
            stop = self.resolution_times.get(order.con_id)
            if stop is not None and ts >= stop:
                order.status, order.closed_at, order.close_reason = "cancelled", stop, "expired"
                continue
            q = self.panel.quote(ts, order.con_id)
            if q is None:
                continue
            _bid, ask, ask_size, source = q
            limit = float(order.request.limit_price)
            if not np.isfinite(ask) or ask > limit + PRICE_TOL:
                continue
            qty = math.floor(order.remaining + 1e-9)
            if self.fill_model == "live":
                if source != SWEEP or not np.isfinite(ask_size) or ask_size <= 0:
                    self.blocked_by_size += 1
                    continue
                left = shown.setdefault(order.con_id, float(ask_size))
                qty = min(qty, math.floor(left + 1e-9))
                shown[order.con_id] = left - qty
            if qty <= 0:
                continue
            order.fills.append((ts, float(qty), float(ask)))
            order.filled += qty
            order.cost += qty * float(ask)
            if order.remaining <= 1e-9:
                order.status, order.closed_at, order.close_reason = "filled", ts, "filled"
            n += 1
        return n

    def expire(self, con_ids: Sequence[int], at: datetime, reason: str = "expired") -> int:
        """Close every open order on these legs; returns how many were open."""
        wanted = {int(c) for c in con_ids}
        n = 0
        for order in self.orders.values():
            if order.status == "open" and order.con_id in wanted:
                order.status, order.closed_at, order.close_reason = "cancelled", pd.Timestamp(at), reason
                n += 1
        return n

    def settle(self, con_ids: Sequence[int]) -> None:
        """These legs have resolved: their positions leave the venue."""
        self._settled.update(int(c) for c in con_ids)

    # -- VenueAdapter --------------------------------------------------------
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def is_healthy(self) -> bool:
        return True

    def list_markets(self) -> list[Market]:
        return [self.get_market(MarketId.ibkr(c)) for c in self.contracts]

    def get_market(self, market_id: MarketId) -> Market:
        c = self.contracts.get(market_id.con_id)
        if c is None:
            raise VenueError(f"unknown contract {market_id}")
        partner = MarketId.ibkr(c.partner) if c.partner is not None else None
        return Market(
            market_id=market_id, symbol=c.symbol, title=f"{c.symbol} {c.expiry} > {c.strike:g}",
            no_id=partner if c.right == "C" else None,
            scheduled_resolution_time=self.resolution_times.get(c.con_id),
            strike=c.strike, tick_size=0.01, size_increment=1.0,
            venue_meta={"right": c.right, "expiry": c.expiry},
        )

    def _snapshot(self):
        i = self.panel.index_at_or_before(self.now) if self.now is not None else -1
        return self.panel.times[i] if i >= 0 else None

    def get_quotes(self, market_ids) -> list[Quote]:
        ts = self._snapshot()
        out = []
        for m in market_ids:
            q = self.panel.quote(ts, m.con_id) if ts is not None else None
            if q is not None:
                out.append(Quote(market_id=m, bid=_or_none(q[0]), ask=_or_none(q[1]), ts=ts,
                                 data_type="delayed"))
        return out

    def get_order_book(self, market_id: MarketId, depth: int = 5) -> OrderBook:
        ts = self._snapshot()
        q = self.panel.quote(ts, market_id.con_id) if ts is not None else None
        book = OrderBook(market_id=market_id, ts=ts if ts is not None else pd.Timestamp(0, tz="UTC"))
        if q is not None and np.isfinite(q[0]):
            book.bids.append(OrderBookLevel(price=float(q[0]), size=0.0))
        if q is not None and np.isfinite(q[1]):
            book.asks.append(OrderBookLevel(price=float(q[1]),
                                            size=float(q[2]) if np.isfinite(q[2]) else 0.0))
        return book

    def get_positions(self) -> list[Position]:
        held: dict[int, list[float]] = {}
        for order in self.orders.values():
            if order.filled > 0 and order.con_id not in self._settled:
                qty_cost = held.setdefault(order.con_id, [0.0, 0.0])
                qty_cost[0] += order.filled
                qty_cost[1] += order.cost
        return [
            Position(market_id=MarketId.ibkr(con), leg=self._leg(con), quantity=qty,
                     avg_price=cost / qty)
            for con, (qty, cost) in sorted(held.items()) if qty > 0
        ]

    def get_balances(self) -> list[Balance]:
        return [Balance(currency="USD", total=1e9, available=1e9)]

    def _as_order(self, order: SimOrder) -> Order:
        return Order(
            order_id=order.order_id, request=order.request,
            status="open" if order.status == "open" else order.status,
            filled_quantity=order.filled, avg_fill_price=order.avg_price,
            venue_meta={"perm_id": order.perm_id},
        )

    def get_open_orders(self) -> list[Order]:
        return [self._as_order(o) for o in self.orders.values() if o.status == "open"]

    def get_all_open_orders(self) -> list[Order]:
        return self.get_open_orders()

    def get_order(self, order_id: str) -> Order | None:
        order = self.orders.get(str(order_id))
        return self._as_order(order) if order is not None else None

    def _place_order(self, request: OrderRequest) -> OrderAck:
        con = request.market_id.con_id
        if con not in self.contracts:
            return OrderAck(order_id="", accepted=False, detail=f"unknown conId {con}")
        if request.order_type != "LMT" or request.limit_price is None or request.side != "BUY":
            return OrderAck(order_id="", accepted=False, detail="the replay takes BUY limits only")
        oid, perm = str(self._next_id), self._next_perm
        self._next_id += 1
        self._next_perm += 1
        self.orders[oid] = SimOrder(
            order_id=oid, perm_id=perm, request=request,
            placed_at=self.now if self.now is not None else pd.Timestamp(0, tz="UTC"),
            active_from=self.decision_index + self.latency,
        )
        return OrderAck(order_id=oid, accepted=True, detail="Submitted", raw={"perm_id": perm})

    def cancel_order(self, order_id: str) -> bool:
        order = self.orders.get(str(order_id))
        if order is None or order.status != "open":
            return False
        order.status, order.closed_at, order.close_reason = "cancelled", self.now, "cancelled"
        return True

    def fee_model(self) -> FeeModel:
        return FlatCommissionFeeModel(self.fee)

    def carry_model(self) -> CarryModel:
        return NullCarry()

    def _leg(self, con: int) -> str:
        c = self.contracts.get(con)
        return "NO" if c is not None and c.right == "P" else "YES"


def _or_none(value) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None


__all__ = ["FILL_MODELS", "SimOrder", "SimVenue"]
