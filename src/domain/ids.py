"""Opaque market identity."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

IBKR = "ibkr"
POLYMARKET = "polymarket"


class MarketId(BaseModel):
    """Opaque, hashable (venue, native_id) pair."""

    model_config = ConfigDict(frozen=True)

    venue: str
    native_id: str

    def __str__(self) -> str:
        return f"{self.venue}:{self.native_id}"

    @classmethod
    def parse(cls, text: str) -> "MarketId":
        venue, sep, native = text.partition(":")
        if not sep or not venue or not native:
            raise ValueError(f"not a MarketId string: {text!r}")
        return cls(venue=venue, native_id=native)

    @classmethod
    def ibkr(cls, con_id: int) -> "MarketId":
        return cls(venue=IBKR, native_id=str(int(con_id)))

    @classmethod
    def polymarket(cls, condition_id: str) -> "MarketId":
        return cls(venue=POLYMARKET, native_id=condition_id)

    @property
    def con_id(self) -> int:
        """The IBKR conId. Raises if this is not an IBKR id."""
        if self.venue != IBKR:
            raise ValueError(f"{self} is not an IBKR market id")
        return int(self.native_id)
