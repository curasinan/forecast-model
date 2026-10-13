"""Carry semantics: what a position earns (or costs) per day just by existing."""

from __future__ import annotations

import abc
from datetime import date


class StaleCarryRateError(RuntimeError):
    """The stored coupon rate is too old to trust."""


class CarryModel(abc.ABC):
    """Expected accrual per unit of position value per day."""

    @abc.abstractmethod
    def daily_accrual_rate(self) -> float:
        """Fraction of position market value accrued per calendar day."""

    def accrual(self, position_value: float, holding_days: float) -> float:
        """Total expected accrual over a holding period (simple, not compounded)."""
        return position_value * self.daily_accrual_rate() * max(0.0, holding_days)


class NullCarry(CarryModel):
    """No carry (Polymarket; also the safe default)."""

    def daily_accrual_rate(self) -> float:
        return 0.0


class ForecastExCouponCarry(CarryModel):
    """ForecastEx incentive coupon, from a runtime-verified APY."""

    def __init__(self, apy: float, verified_on: date, max_age_days: int = 30):
        if not 0.0 <= apy < 0.5:
            raise ValueError(f"implausible coupon APY: {apy!r}")
        age = (date.today() - verified_on).days
        if age > max_age_days:
            raise StaleCarryRateError(
                f"coupon APY verified {age} days ago (> {max_age_days}); re-verify from "
                "account statements or accrual observation before sizing with carry"
            )
        self.apy = apy
        self.verified_on = verified_on

    def daily_accrual_rate(self) -> float:
        return self.apy / 365.0
