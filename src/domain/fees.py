"""Fee semantics per venue.

Never hardcode a venue's fee facts at call sites. Construct the venue's
FeeModel from runtime-verified values (config with a verification date, or
a live probe) and pass it down. Empirical note (2026-08-16, whatIf probes,
docs/venue_semantics.md): IBKR ForecastEx charges a flat $0.01/contract
commission billed separately, and YES+NO mid prices sum to ~$1.00 — NOT
the $1.01 embedded-commission convention some documentation claims.
"""

from __future__ import annotations

import abc
from datetime import date


class FeeModel(abc.ABC):
    """Execution-cost and price-complement semantics of one venue."""

    @abc.abstractmethod
    def execution_cost(self, price: float, quantity: float) -> float:
        """Total cost (currency units) charged for filling `quantity` contracts at `price`."""

    @abc.abstractmethod
    def yes_no_price_sum(self) -> float:
        """What one YES plus one NO of the same market cost together at fair value."""

    def no_price_from_yes(self, yes_price: float) -> float:
        """Derive the NO price from the YES price under this venue's convention."""
        return self.yes_no_price_sum() - yes_price


class ZeroFeeModel(FeeModel):
    """No execution fees; YES+NO = 1.00 (Polymarket mint/merge invariant)."""

    def execution_cost(self, price: float, quantity: float) -> float:
        return 0.0

    def yes_no_price_sum(self) -> float:
        return 1.0


class FlatCommissionFeeModel(FeeModel):
    """A flat per-contract commission (the verified IBKR ForecastEx shape).

    Values come from runtime verification, never from literals at call
    sites; `verified_on` records when. `max_age_days` guards staleness:
    a fee assumption older than that fails loudly rather than silently
    mispricing every edge calculation.
    """

    def __init__(
        self,
        commission_per_contract: float,
        price_sum: float = 1.0,
        verified_on: date | None = None,
        max_age_days: int = 30,
    ):
        if commission_per_contract < 0:
            raise ValueError("commission cannot be negative")
        if verified_on is not None:
            age = (date.today() - verified_on).days
            if age > max_age_days:
                raise StaleFeeError(
                    f"fee model verified {age} days ago (> {max_age_days}); re-verify "
                    "against a whatIf probe or account statement before trading"
                )
        self.commission_per_contract = commission_per_contract
        self._price_sum = price_sum
        self.verified_on = verified_on

    def execution_cost(self, price: float, quantity: float) -> float:
        return self.commission_per_contract * abs(quantity)

    def yes_no_price_sum(self) -> float:
        return self._price_sum


class StaleFeeError(RuntimeError):
    """Fee facts are too old to trade on."""
