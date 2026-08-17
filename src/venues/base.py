"""The venue port. Strategy, edge, sizing, and execution code depends on
this interface and never on a concrete adapter."""

from __future__ import annotations

import abc
from collections.abc import Iterator, Sequence

from src.domain.carry import CarryModel
from src.domain.fees import FeeModel
from src.domain.ids import MarketId
from src.domain.models import (
    Balance,
    Market,
    Order,
    OrderAck,
    OrderBook,
    OrderRequest,
    Position,
    Quote,
)


class VenueError(RuntimeError):
    """Base class for adapter failures."""


class VenueReadOnlyError(VenueError):
    """An order-path method was called on a read-only adapter."""


class VenueAdapter(abc.ABC):
    """One trading venue, seen through venue-neutral types.

    Adapters default to read-only. Only a deliberately configured writer
    process (see the client-id map in src/venues/ibkr/clients.py) may
    construct a writable adapter, and no code path in this repository
    enables live order transmission — that is a manual, deliberate act by
    the account holder after paper validation.
    """

    name: str = "abstract"

    def __init__(self, read_only: bool = True):
        self.read_only = read_only

    # -- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def is_healthy(self) -> bool: ...

    # -- market data -------------------------------------------------------
    @abc.abstractmethod
    def list_markets(self) -> list[Market]: ...

    @abc.abstractmethod
    def get_market(self, market_id: MarketId) -> Market: ...

    @abc.abstractmethod
    def get_order_book(self, market_id: MarketId, depth: int = 5) -> OrderBook: ...

    @abc.abstractmethod
    def get_quotes(self, market_ids: Sequence[MarketId]) -> list[Quote]: ...

    def stream_quotes(self, market_ids: Sequence[MarketId]) -> Iterator[Quote]:
        """Default streaming: repeated snapshot sweeps. Adapters with a real
        push feed override this."""
        while True:
            yield from self.get_quotes(market_ids)

    # -- account -----------------------------------------------------------
    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def get_balances(self) -> list[Balance]: ...

    @abc.abstractmethod
    def get_open_orders(self) -> list[Order]: ...

    # -- orders ------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> OrderAck:
        if self.read_only:
            raise VenueReadOnlyError(
                f"{self.name} adapter is read-only; refusing to place {request!r}"
            )
        return self._place_order(request)

    def _place_order(self, request: OrderRequest) -> OrderAck:
        raise VenueReadOnlyError(f"{self.name} adapter does not implement order placement")

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    # -- economics ---------------------------------------------------------
    @abc.abstractmethod
    def fee_model(self) -> FeeModel: ...

    @abc.abstractmethod
    def carry_model(self) -> CarryModel: ...
