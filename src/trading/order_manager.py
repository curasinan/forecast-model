"""Order manager — converts edge signals into CLOB orders.

Translates our EdgeSignal + risk-adjusted size into:
- BUY YES (when edge > 0, our prob > market)
- BUY NO  (when edge < 0, our prob < market)

Handles order creation, placement, cancellation, and status tracking.
In dry-run mode, simulates order execution locally. The live Polymarket
CLOB path was retired to legacy/polymarket_onchain (2026-08-16); live
methods raise RuntimeError.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.models.prediction import EdgeSignal
from src.trading.wallet import PolymarketWallet

logger = logging.getLogger(__name__)

_RETIRED_MSG = (
    "live Polymarket execution retired to legacy/polymarket_onchain (2026-08-16); "
    "live trading is migrating to IBKR -- see src/venues/"
)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PLACED = "placed"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    SIMULATED = "simulated"  # Dry-run only


@dataclass
class TradeOrder:
    """A single trade order."""

    order_id: str
    question_id: str
    token_id: str  # Polymarket YES or NO token ID
    side: OrderSide
    outcome: str  # "YES" or "NO"
    price: float  # Limit price (0-1)
    size: float  # Number of tokens (= $ at limit price)
    cost_usd: float  # Total cost = price * size
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    filled_at: datetime | None = None
    clob_order_id: str = ""  # ID from Polymarket API
    reason: str = ""  # Why this trade was made


def edge_to_order(
    edge: EdgeSignal,
    size_usd: float,
    yes_token_id: str = "",
    no_token_id: str = "",
) -> TradeOrder:
    """Convert an EdgeSignal + approved size into a TradeOrder.

    If edge > 0: BUY YES at market price (model thinks YES is underpriced)
    If edge < 0: BUY NO at market price (model thinks NO is underpriced)
    """
    if edge.edge_pct > 0:
        # YES is underpriced — buy YES
        price = edge.market_prob  # Buy at current market price
        outcome = "YES"
        token_id = yes_token_id
        reason = (
            f"Model={edge.model_prob:.0%} > Market={edge.market_prob:.0%}, "
            f"edge={edge.edge_pct:+.1%}, EV=${edge.expected_value:+.3f}"
        )
    else:
        # NO is underpriced — buy NO
        price = 1 - edge.market_prob  # NO price = 1 - YES price
        outcome = "NO"
        token_id = no_token_id
        reason = (
            f"Model={edge.model_prob:.0%} < Market={edge.market_prob:.0%}, "
            f"edge={edge.edge_pct:+.1%}, EV=${edge.expected_value:+.3f}"
        )

    # Size in tokens: $ / price = number of tokens
    token_size = size_usd / price if price > 0 else 0

    return TradeOrder(
        order_id=str(uuid.uuid4())[:8],
        question_id=edge.question_id,
        token_id=token_id,
        side=OrderSide.BUY,
        outcome=outcome,
        price=round(price, 4),
        size=round(token_size, 2),
        cost_usd=round(size_usd, 2),
        reason=reason,
    )


class OrderManager:
    """Manages order lifecycle: create, place, track, cancel."""

    def __init__(self, wallet: PolymarketWallet):
        self._wallet = wallet
        self._orders: list[TradeOrder] = []

    @property
    def orders(self) -> list[TradeOrder]:
        return self._orders

    @property
    def open_orders(self) -> list[TradeOrder]:
        return [o for o in self._orders if o.status in (OrderStatus.PENDING, OrderStatus.PLACED)]

    @property
    def filled_orders(self) -> list[TradeOrder]:
        return [o for o in self._orders if o.status == OrderStatus.FILLED]

    def place_order(self, order: TradeOrder) -> bool:
        """Place an order on Polymarket (or simulate in dry-run).

        Returns True if order was placed/simulated successfully.
        """
        if self._wallet.is_dry_run:
            return self._simulate_order(order)

        return self._place_live_order(order)

    def _simulate_order(self, order: TradeOrder) -> bool:
        """Simulate order execution in dry-run mode.

        Assumes immediate fill at limit price (optimistic).
        """
        success = self._wallet.simulate_trade(
            side=order.side.value,
            price=order.price,
            size=order.size,
        )

        if success:
            order.status = OrderStatus.SIMULATED
            order.filled_at = datetime.now()
            self._orders.append(order)
            logger.info(
                f"[DRY-RUN] Order {order.order_id}: "
                f"BUY {order.outcome} @ ${order.price:.2f} x {order.size:.1f} "
                f"= ${order.cost_usd:.2f} | {order.reason}"
            )
            return True
        else:
            order.status = OrderStatus.REJECTED
            order.reason += " | Insufficient simulated balance"
            self._orders.append(order)
            logger.warning(f"[DRY-RUN] Order {order.order_id} REJECTED: insufficient balance")
            return False

    def _place_live_order(self, order: TradeOrder) -> bool:
        """Retired: live Polymarket order placement moved to legacy/.

        Always raises; see legacy/polymarket_onchain/clob_orders.py.
        """
        raise RuntimeError(_RETIRED_MSG)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order (simulated orders only; live path is retired)."""
        order = next((o for o in self._orders if o.order_id == order_id), None)
        if not order:
            return False

        if self._wallet.is_dry_run:
            order.status = OrderStatus.CANCELLED
            return True

        raise RuntimeError(_RETIRED_MSG)

    def get_trade_summary(self) -> dict:
        """Summary statistics of all orders."""
        total = len(self._orders)
        filled = len([o for o in self._orders if o.status in (OrderStatus.FILLED, OrderStatus.SIMULATED)])
        rejected = len([o for o in self._orders if o.status == OrderStatus.REJECTED])
        total_cost = sum(o.cost_usd for o in self._orders if o.status in (OrderStatus.FILLED, OrderStatus.SIMULATED))

        return {
            "total_orders": total,
            "filled": filled,
            "rejected": rejected,
            "total_cost_usd": round(total_cost, 2),
        }
