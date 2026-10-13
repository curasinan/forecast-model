"""Order manager -- converts edge signals into venue-neutral orders."""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import Enum

from src.domain.fees import FeeModel
from src.domain.ids import POLYMARKET
from src.domain.models import Market, OrderRequest
from src.models.prediction import EdgeSignal
from src.venues.base import VenueAdapter, VenueError

logger = logging.getLogger(__name__)

_NO_LEG_IS_MARKET_ID_VENUES = frozenset({POLYMARKET})


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PLACED = "placed"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    SIMULATED = "simulated"  # Simulated fill (no venue involved)


@dataclass
class TradeOrder:
    """A single trade order, venue-neutral."""

    order_id: str
    question_id: str
    market_id: str
    side: OrderSide
    outcome: str  # "YES" or "NO"
    price: float  # Limit price (0-1)
    size: float  # Number of contracts / tokens
    cost_usd: float  # price * size
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    filled_at: datetime | None = None
    venue: str = ""
    venue_order_id: str = ""  # The venue's native order id once placed
    filled_size: float = 0.0
    reason: str = ""
    perm_id: str = ""

    @property
    def clob_order_id(self) -> str:  # backwards-compatible alias
        return self.venue_order_id


def snap_to_tick(price: float, tick: float) -> float:
    """Largest multiple of ``tick`` at or below ``price``, floored at one tick."""
    d_price, d_tick = Decimal(str(price)), Decimal(str(tick))
    steps = (d_price / d_tick).to_integral_value(rounding=ROUND_FLOOR)
    snapped = steps * d_tick
    return float(snapped if snapped >= d_tick else d_tick)


def edge_to_order(
    edge: EdgeSignal,
    size_usd: float,
    market: Market | None = None,
    fee_model: FeeModel | None = None,
    no_price: float | None = None,
) -> TradeOrder:
    """Convert an EdgeSignal + approved size into a TradeOrder."""
    yes_price = edge.market_prob
    if edge.edge_pct > 0:
        price = yes_price
        outcome = "YES"
        leg_id = str(market.market_id) if market is not None else ""
        reason = (
            f"Model={edge.model_prob:.0%} > Market={yes_price:.0%}, "
            f"edge={edge.edge_pct:+.1%}, EV=${edge.expected_value:+.3f}"
        )
    else:
        if no_price is not None:
            price = no_price
        elif fee_model is not None:
            price = fee_model.no_price_from_yes(yes_price)
        else:
            price = 1.0 - yes_price
        outcome = "NO"
        if market is not None and market.no_id is not None:
            leg_id = str(market.no_id)
        elif market is not None and market.market_id.venue in _NO_LEG_IS_MARKET_ID_VENUES:
            leg_id = str(market.market_id)  # single-contract venues (Polymarket)
        else:
            leg_id = ""
        reason = (
            f"Model={edge.model_prob:.0%} < Market={yes_price:.0%}, "
            f"edge={edge.edge_pct:+.1%}, EV=${edge.expected_value:+.3f}"
        )

    tick = market.tick_size if market is not None else None
    if tick and tick > 0:
        price = snap_to_tick(price, tick)

    size = size_usd / price if price > 0 else 0.0
    inc = market.size_increment if market is not None else None
    if inc:
        size = math.floor(size / inc + 1e-9) * inc
    cost = price * size

    return TradeOrder(
        order_id=str(uuid.uuid4())[:8],
        question_id=edge.question_id,
        market_id=leg_id,
        side=OrderSide.BUY,
        outcome=outcome,
        price=round(price, 4),
        size=round(size, 4),
        cost_usd=round(cost, 2),
        venue=(market.market_id.venue if market is not None else ""),
        reason=reason,
    )


class SimulatedFillEngine:
    """Local, optimistic fill simulation (immediate fill at the limit)."""

    def __init__(self, bankroll: float = 1000.0):
        self.balance = float(bankroll)
        self.positions: dict[str, float] = {}

    def fill(self, order: TradeOrder) -> bool:
        if order.cost_usd > self.balance:
            return False
        self.balance -= order.cost_usd
        self.positions[order.market_id] = self.positions.get(order.market_id, 0.0) + order.size
        return True


class OrderManager:
    """Order lifecycle on top of a VenueAdapter: create, place, track, cancel."""

    def __init__(
        self,
        venue: VenueAdapter,
        simulate: bool | None = None,
        sim_bankroll: float = 1000.0,
    ):
        self._venue = venue
        self._simulate = True if venue.read_only else bool(simulate)
        self._sim = SimulatedFillEngine(sim_bankroll)
        self._orders: list[TradeOrder] = []

    # -- views -------------------------------------------------------------
    @property
    def venue(self) -> VenueAdapter:
        return self._venue

    @property
    def is_simulated(self) -> bool:
        return self._simulate

    @property
    def simulated_balance(self) -> float:
        return self._sim.balance

    @property
    def orders(self) -> list[TradeOrder]:
        return self._orders

    @property
    def open_orders(self) -> list[TradeOrder]:
        return [o for o in self._orders if o.status in (OrderStatus.PENDING, OrderStatus.PLACED)]

    @property
    def filled_orders(self) -> list[TradeOrder]:
        return [o for o in self._orders if o.status in (OrderStatus.FILLED, OrderStatus.SIMULATED)]

    # -- placement ---------------------------------------------------------
    def place_order(self, order: TradeOrder) -> bool:
        """Place (or simulate) an order. Returns True if accepted/simulated."""
        if not order.market_id:
            order.status = OrderStatus.REJECTED
            order.reason += " | no market id for the leg"
            self._orders.append(order)
            return False
        if order.size <= 0 or order.price <= 0:
            order.status = OrderStatus.REJECTED
            order.reason += " | zero size after rounding"
            self._orders.append(order)
            return False
        if self._simulate:
            return self._simulate_order(order)
        return self._place_venue_order(order)

    def _simulate_order(self, order: TradeOrder) -> bool:
        if self._sim.fill(order):
            order.status = OrderStatus.SIMULATED
            order.filled_at = datetime.now()
            order.filled_size = order.size
            self._orders.append(order)
            logger.info(
                "[SIMULATED] %s: BUY %s @ %.4f x %.2f = $%.2f | %s",
                order.order_id, order.outcome, order.price, order.size, order.cost_usd, order.reason,
            )
            return True
        order.status = OrderStatus.REJECTED
        order.reason += " | insufficient simulated balance"
        self._orders.append(order)
        logger.warning("[SIMULATED] %s REJECTED: insufficient balance", order.order_id)
        return False

    def _place_venue_order(self, order: TradeOrder) -> bool:
        from src.domain.ids import MarketId  # local import keeps module header light

        request = OrderRequest(
            market_id=MarketId.parse(order.market_id),
            side="BUY",
            leg="NO" if order.outcome == "NO" else "YES",
            quantity=order.size,
            order_type="LMT",
            limit_price=order.price,
            tif="GTC",
        )
        try:
            ack = self._venue.place_order(request)
        except VenueError as exc:
            order.status = OrderStatus.REJECTED
            order.reason += f" | venue refused: {exc}"
            self._orders.append(order)
            logger.warning("[%s] %s refused: %s", self._venue.name, order.order_id, exc)
            return False
        order.venue_order_id = ack.order_id
        order.perm_id = str(ack.raw.get("perm_id") or "")
        if ack.accepted:
            order.status = OrderStatus.PLACED
            logger.info(
                "[%s] %s PLACED venue_id=%s BUY %s @ %.4f x %.2f | %s",
                self._venue.name, order.order_id, ack.order_id, order.outcome,
                order.price, order.size, ack.detail,
            )
        else:
            order.status = OrderStatus.REJECTED
            order.reason += f" | venue rejected: {ack.detail}"
            logger.warning("[%s] %s REJECTED: %s", self._venue.name, order.order_id, ack.detail)
        self._orders.append(order)
        return ack.accepted

    # -- tracking ----------------------------------------------------------
    def sync_with_venue(self) -> int:
        """Refresh PLACED orders from the venue. Returns number updated."""
        if self._simulate:
            return 0
        updated = 0
        open_ids = {o.order_id for o in self._venue.get_open_orders()}
        for order in self.open_orders:
            if not order.venue_order_id:
                continue
            state = self._venue.get_order(order.venue_order_id)
            if state is None:
                if order.venue_order_id not in open_ids:
                    logger.warning(
                        "order %s (venue %s) no longer open but venue gives no history; "
                        "left PLACED -- reconcile manually",
                        order.order_id, order.venue_order_id,
                    )
                continue
            order.filled_size = state.filled_quantity
            if state.status == "filled":
                order.status = OrderStatus.FILLED
                order.filled_at = datetime.now()
                if state.avg_fill_price is not None:
                    order.price = round(state.avg_fill_price, 4)
                    order.cost_usd = round(order.price * order.filled_size, 2)
                updated += 1
            elif state.status in ("cancelled", "rejected"):
                order.status = OrderStatus.CANCELLED if state.status == "cancelled" \
                    else OrderStatus.REJECTED
                updated += 1
        return updated

    def backfill_perm_ids(self) -> list[TradeOrder]:
        """Fill in a permId the venue had not yet assigned at ack time."""
        if self._simulate:
            return []
        missing = [o for o in self.open_orders if o.venue_order_id and not o.perm_id]
        if not missing:
            return []
        fetch = getattr(self._venue, "get_all_open_orders", None)
        if fetch is None:
            logger.warning(
                "[%s] %d order(s) lack a permId but the venue offers no open-order "
                "re-read; left empty -- the next reconcile will flag them",
                self._venue.name, len(missing),
            )
            return []
        try:
            venue_orders = fetch()
        except Exception as exc:  # noqa: BLE001 - best-effort; see below
            logger.error(
                "[%s] permId backfill re-read failed (%s); %d order(s) keep an empty "
                "perm_id -- the next reconcile will flag them",
                self._venue.name, type(exc).__name__, len(missing),
            )
            return []
        filled: list[TradeOrder] = []
        for order in missing:
            candidates = {
                str((v.venue_meta or {}).get("perm_id") or "")
                for v in venue_orders
                if str(v.order_id) == order.venue_order_id
                and str(v.request.market_id) == order.market_id
            }
            candidates.discard("")
            if len(candidates) != 1:
                logger.warning(
                    "[%s] %s (venue %s): permId still %s after re-reading open orders; "
                    "trade_log.perm_id stays empty -- the next reconcile will flag it",
                    self._venue.name, order.order_id, order.venue_order_id,
                    "ambiguous" if candidates else "unassigned",
                )
                continue
            order.perm_id = candidates.pop()
            filled.append(order)
            logger.info("[%s] %s (venue %s): permId %s backfilled after ack",
                        self._venue.name, order.order_id, order.venue_order_id, order.perm_id)
        return filled

    def cancel_order(self, order_id: str) -> bool:
        order = next((o for o in self._orders if o.order_id == order_id), None)
        if not order:
            return False
        if self._simulate:
            order.status = OrderStatus.CANCELLED
            return True
        if not order.venue_order_id:
            return False
        ok = self._venue.cancel_order(order.venue_order_id)
        if ok:
            order.status = OrderStatus.CANCELLED
        return ok

    def get_trade_summary(self) -> dict:
        done = (OrderStatus.FILLED, OrderStatus.SIMULATED)
        return {
            "venue": self._venue.name,
            "simulated": self._simulate,
            "total_orders": len(self._orders),
            "filled": len([o for o in self._orders if o.status in done]),
            "placed_open": len(self.open_orders),
            "rejected": len([o for o in self._orders if o.status == OrderStatus.REJECTED]),
            "total_cost_usd": round(sum(o.cost_usd for o in self._orders if o.status in done), 2),
        }
