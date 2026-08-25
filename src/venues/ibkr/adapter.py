"""IBKR ForecastEx adapter behind the venue-neutral VenueAdapter port.

Read-only by design in this build: only role='live_trading' may construct a
writable adapter, and even then order transmission only works on a PAPER account
-- enabling it is a manual, deliberate act by the account holder after paper
validation, never a commit in this migration.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import datetime, timezone

from src.domain.carry import CarryModel, ForecastExCouponCarry, NullCarry
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
from src.venues.base import VenueAdapter, VenueError, VenueReadOnlyError
from src.venues.ibkr.clients import connect_ib
from src.venues.ibkr.contracts import ContractResolver
from src.venues.ibkr.settings import IBKRSettings

logger = logging.getLogger(__name__)

_QUOTE_BATCH_SIZE = 50
_DATA_TYPE_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed"}
_STATUS_MAP = {
    "PendingSubmit": "pending",
    "PendingCancel": "pending",
    "PreSubmitted": "open",
    "Submitted": "open",
    "Filled": "filled",
    "Cancelled": "cancelled",
    "ApiCancelled": "cancelled",
    "Inactive": "rejected",
}


class ModeMismatchError(VenueError):
    """The connected account's mode disagrees with settings.trading_mode."""


def _clean_price(value) -> float | None:
    """IB reports missing quotes as nan or -1; both become None."""
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or value < 0:
        return None
    return value


def _parse_expiry(text: str | None) -> datetime | None:
    """Parse the cache expiry string ('YYYYMMDD' or 'YYYYMMDD HH:MM:SS')."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class IBKRAdapter(VenueAdapter):
    """ForecastEx event contracts through the ib_async Gateway API."""

    name = "ibkr"

    def __init__(self, settings: IBKRSettings, role: str = "research", read_only: bool = True):
        if not read_only and role != "live_trading":
            raise ValueError(
                f"role {role!r} may not construct a writable adapter; only 'live_trading' "
                "(client id 1) writes orders -- single-writer rule"
            )
        super().__init__(read_only=read_only)
        self.settings = settings
        self.role = role
        self.ib = None
        self._resolver: ContractResolver | None = None
        self.account: str | None = None
        self.mode: str | None = None  # 'paper' | 'live', set by connect()

    @property
    def resolver(self) -> ContractResolver:
        """Contract cache, created lazily so construction touches no disk."""
        if self._resolver is None:
            self._resolver = ContractResolver(self.settings)
        return self._resolver

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        """Connect (reusing an existing session), then verify the account mode.

        Mode is inferred from the account prefix (DU/DF = paper, the only
        API-visible signal). A disagreement with settings.trading_mode raises
        ModeMismatchError; account id and mode are logged at WARNING so every
        run states which account it touched (spec Stage 7).
        """
        if self.ib is None or not self.ib.isConnected():
            self.ib = connect_ib(self.role, self.settings)
        accounts = list(self.ib.managedAccounts())
        if not accounts:
            raise VenueError("no managed accounts visible; is the Gateway logged in?")
        account = accounts[0]
        mode = "paper" if account.startswith(("DU", "DF")) else "live"
        if mode != self.settings.trading_mode:
            raise ModeMismatchError(
                f"connected account {account} is a {mode} account but IBKR_TRADING_MODE="
                f"{self.settings.trading_mode!r}; refusing to continue on the wrong session"
            )
        self.account = account
        self.mode = mode
        logger.warning(
            "ibkr session: account=%s mode=%s role=%s read_only=%s",
            account, mode, self.role, self.read_only,
        )

    def disconnect(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
            self.ib = None

    def is_healthy(self) -> bool:
        if self.ib is None or not self.ib.isConnected():
            return False
        try:
            self.ib.reqCurrentTime()
        except Exception:
            return False
        return True

    def _require_ib(self):
        if self.ib is None or not self.ib.isConnected():
            raise VenueError("not connected; call connect() first")
        return self.ib

    # -- market data -------------------------------------------------------
    def list_markets(self) -> list[Market]:
        """Markets from the resolver cache (one per YES leg; NO legs are no_id)."""
        markets: list[Market] = []
        for con_id in self.resolver.tradable_con_ids():
            row = self.resolver.get(con_id)
            if row is None or row["right"] == "P":
                continue
            markets.append(self._row_to_market(row))
        return markets

    def get_market(self, market_id: MarketId) -> Market:
        row = self.resolver.get(market_id.con_id)
        if row is None:
            raise VenueError(f"{market_id} is not in the contract cache; seed or resolve first")
        return self._row_to_market(row)

    def get_quotes(self, market_ids: Sequence[MarketId]) -> list[Quote]:
        ib = self._require_ib()
        from ib_async import Contract  # lazy; conId-only construction (spec Stage 3.2)

        ib.reqMarketDataType(self.settings.market_data_type)
        data_type = _DATA_TYPE_NAMES.get(self.settings.market_data_type, "delayed")
        contracts = [Contract(conId=m.con_id, exchange="FORECASTX") for m in market_ids]
        quotes: list[Quote] = []
        for start in range(0, len(contracts), _QUOTE_BATCH_SIZE):
            batch = contracts[start:start + _QUOTE_BATCH_SIZE]
            for ticker in ib.reqTickers(*batch):
                quotes.append(Quote(
                    market_id=MarketId.ibkr(ticker.contract.conId),
                    bid=_clean_price(ticker.bid),
                    ask=_clean_price(ticker.ask),
                    last=_clean_price(ticker.last),
                    ts=datetime.now(timezone.utc),
                    data_type=data_type,
                ))
        return quotes

    def get_order_book(self, market_id: MarketId, depth: int = 5) -> OrderBook:
        """Top-of-book only: a depth feed exists on FORECASTX but reqMktDepth
        wiring is a Stage 5 recorder task. Sizes are unknown at this level."""
        quotes = self.get_quotes([market_id])
        quote = quotes[0] if quotes else None
        bids = [OrderBookLevel(price=quote.bid, size=0.0)] if quote and quote.bid else []
        asks = [OrderBookLevel(price=quote.ask, size=0.0)] if quote and quote.ask else []
        ts = quote.ts if quote else datetime.now(timezone.utc)
        return OrderBook(market_id=market_id, bids=bids, asks=asks, ts=ts)

    # -- account -----------------------------------------------------------
    def get_positions(self) -> list[Position]:
        ib = self._require_ib()
        positions: list[Position] = []
        for pos in ib.positions():
            contract = pos.contract
            positions.append(Position(
                market_id=MarketId.ibkr(contract.conId),
                leg="NO" if getattr(contract, "right", "") == "P" else "YES",
                quantity=float(pos.position),
                avg_price=float(pos.avgCost),
                venue_meta={
                    "account": pos.account,
                    "local_symbol": getattr(contract, "localSymbol", ""),
                },
            ))
        return positions

    def get_balances(self) -> list[Balance]:
        ib = self._require_ib()
        by_currency: dict[str, dict[str, float]] = {}
        for value in ib.accountSummary():
            if value.tag not in ("NetLiquidation", "AvailableFunds"):
                continue
            slot = by_currency.setdefault(value.currency or "USD", {"total": 0.0, "available": 0.0})
            key = "total" if value.tag == "NetLiquidation" else "available"
            slot[key] = float(value.value)
        return [
            Balance(currency=currency, total=slot["total"], available=slot["available"])
            for currency, slot in by_currency.items()
        ]

    def get_open_orders(self) -> list[Order]:
        # openTrades() rather than openOrders(): the bare order list lacks the
        # contract, and the domain Order needs a MarketId.
        ib = self._require_ib()
        return [self._trade_to_order(trade) for trade in ib.openTrades()]

    def get_all_open_orders(self) -> list[Order]:
        """Every resting order visible to this session, own or foreign.

        reqAllOpenOrders() (unlike openTrades()) surfaces GTC orders placed by
        other client ids -- their orderId is meaningless here, so callers must
        join on `venue_meta["perm_id"]` instead.
        """
        ib = self._require_ib()
        return [self._trade_to_order(trade) for trade in ib.reqAllOpenOrders()]

    def _trade_to_order(self, trade) -> Order:
        contract, order, status = trade.contract, trade.order, trade.orderStatus
        is_limit = order.orderType == "LMT"
        request = OrderRequest(
            market_id=MarketId.ibkr(contract.conId),
            side="BUY" if order.action.upper() == "BUY" else "SELL",
            leg="NO" if getattr(contract, "right", "") == "P" else "YES",
            quantity=float(order.totalQuantity),
            order_type="LMT" if is_limit else "MKT",
            limit_price=float(order.lmtPrice) if is_limit else None,
            tif="GTC" if order.tif == "GTC" else "DAY",
        )
        return Order(
            order_id=str(order.orderId),
            request=request,
            status=_STATUS_MAP.get(status.status, "pending"),
            filled_quantity=float(status.filled or 0.0),
            avg_fill_price=float(status.avgFillPrice) if status.avgFillPrice else None,
            venue_meta={"perm_id": int(getattr(order, "permId", 0) or 0)},
        )

    # -- orders ------------------------------------------------------------
    def _place_order(self, request: OrderRequest) -> OrderAck:
        """Transmit an order -- PAPER accounts only.

        Live transmission is enabled manually by the account holder after
        paper validation; no code path in this repository can do it. The
        check is on the verified account mode (DU/DF prefix), not on
        configuration, so a mis-set IBKR_TRADING_MODE cannot open the door
        (connect() already refuses on a mode mismatch).
        """
        if self.mode != "paper":
            raise VenueError(
                "live order transmission is disabled in code: connected account is "
                f"{self.account!r} (mode={self.mode!r}); enable live trading manually, "
                "after paper validation, outside this repository"
            )
        from ib_async import Contract, LimitOrder, MarketOrder  # sanctioned: src/venues/

        ib = self._require_ib()
        contract = Contract(conId=request.market_id.con_id, exchange="FORECASTX")
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return OrderAck(order_id="", accepted=False,
                            detail=f"could not qualify conId {request.market_id.con_id}")
        action = "BUY" if request.side == "BUY" else "SELL"
        qty = float(request.quantity)
        if request.order_type == "LMT":
            if request.limit_price is None:
                return OrderAck(order_id="", accepted=False, detail="LMT order without limit_price")
            order = LimitOrder(action, qty, float(request.limit_price), tif=request.tif)
        else:
            order = MarketOrder(action, qty, tif=request.tif)  # explicit tif: error 10349 otherwise
        trade = ib.placeOrder(contract, order)
        ib.sleep(1.0)  # let the first status round-trip arrive
        status = trade.orderStatus.status
        accepted = status not in ("Inactive", "Cancelled", "ApiCancelled")
        return OrderAck(
            order_id=str(trade.order.orderId),
            accepted=accepted,
            detail=status,
            raw={"status": status, "perm_id": getattr(trade.order, "permId", None),
                 "account": self.account, "mode": self.mode},
        )

    def get_order(self, order_id: str) -> Order | None:
        """Order state from this session's trade history (open or done)."""
        ib = self._require_ib()
        for trade in ib.trades():
            if str(trade.order.orderId) != str(order_id):
                continue
            return self._trade_to_order(trade)
        return None

    def cancel_order(self, order_id: str) -> bool:
        if self.read_only:
            raise VenueReadOnlyError(f"{self.name} adapter is read-only; refusing to cancel")
        ib = self._require_ib()
        for trade in ib.openTrades():
            if str(trade.order.orderId) == str(order_id):
                ib.cancelOrder(trade.order)
                return True
        return False

    # -- economics ---------------------------------------------------------
    def fee_model(self) -> FeeModel:
        s = self.settings
        if s.commission_per_contract is None or s.fees_verified_on is None:
            raise VenueError(
                "fee facts unverified: set IBKR_COMMISSION_PER_CONTRACT and "
                "IBKR_FEES_VERIFIED_ON from a whatIf probe (see docs/venue_semantics.md)"
            )
        # YES+NO mid sums to ~1.00 with commission billed separately (verified
        # 2026-08-16), hence price_sum=1.0.
        return FlatCommissionFeeModel(
            s.commission_per_contract, price_sum=1.0, verified_on=s.fees_verified_on
        )

    def carry_model(self) -> CarryModel:
        s = self.settings
        if s.coupon_apy is not None and s.coupon_verified_on is not None:
            return ForecastExCouponCarry(s.coupon_apy, s.coupon_verified_on)
        return NullCarry()

    # -- internals ---------------------------------------------------------
    def _row_to_market(self, row: dict) -> Market:
        no_id = None
        if row["right"] == "C":
            try:
                _, no_con_id = self.resolver.yes_no_pair(
                    row["symbol"], row["expiry"], row["strike"]
                )
                no_id = MarketId.ibkr(no_con_id)
            except VenueError:
                no_id = None
        return Market(
            market_id=MarketId.ibkr(row["con_id"]),
            symbol=row["symbol"],
            title=row["long_name"] or row["local_symbol"],
            no_id=no_id,
            expiry=_parse_expiry(row["expiry"]),
            # ForecastEx resolution timing is scheduled, not estimated (the
            # venue's structural advantage). The contract's last-trade date is
            # the best venue-native approximation of it; the underlying macro
            # release calendar (BLS/BEA/FOMC timestamps) is finer-grained and
            # is a Stage 6 USER-DECISION data source, not wired yet.
            scheduled_resolution_time=_parse_expiry(row["expiry"]),
            strike=row["strike"],
            tick_size=row["min_tick"],
            multiplier=float(row["multiplier"]) if row["multiplier"] else 1.0,
            size_increment=1.0,  # minSize=sizeIncrement=1 contract (verified 2026-08-18)
            currency=row["currency"] or "USD",
            venue_meta={
                "local_symbol": row["local_symbol"],
                "trading_class": row["trading_class"],
                "right": row["right"],
            },
        )
