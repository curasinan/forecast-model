"""Limit prices for BUY orders on one quoted leg."""

from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal

PRICE_POLICIES = ("ask", "mid")
DEFAULT_PRICE_POLICY = "ask"
DEFAULT_TICK = 0.01


def finite_or_none(value) -> float | None:
    """`value` as a float, or None for a missing, non-numeric or non-finite one."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def buy_limit_price(bid, ask, *, policy: str = DEFAULT_PRICE_POLICY,
                    tick: float = DEFAULT_TICK, ticks_below_ask: int = 0) -> float | None:
    """The limit for a BUY on one leg, on the tick grid, or None without a usable quote.

    'ask' bids the ask less `ticks_below_ask` ticks, never below the bid: at 0
    ticks the limit equals the ask, so the order is marketable and fills
    against the resting offer. 'mid' is the earlier passive rule: the mid,
    snapped down to the tick, which fills only if a seller comes down to it.
    """
    if policy not in PRICE_POLICIES:
        raise ValueError(f"unknown price policy {policy!r}; expected one of {PRICE_POLICIES}")
    if ticks_below_ask < 0:
        raise ValueError("ticks_below_ask cannot be negative")
    if tick is None or tick <= 0:
        raise ValueError("tick must be positive")

    b, a = finite_or_none(bid), finite_or_none(ask)
    if a is None or a <= 0:
        return None
    if b is not None and b > a:
        return None  # crossed quote

    d_tick = Decimal(str(tick))
    if policy == "mid":
        if b is None:
            return None
        raw = (Decimal(str(b)) + Decimal(str(a))) / 2
    else:
        raw = Decimal(str(a)) - ticks_below_ask * d_tick
        if b is not None:
            raw = max(raw, Decimal(str(b)))

    price = (raw / d_tick).to_integral_value(rounding=ROUND_FLOOR) * d_tick
    if price < d_tick:
        return None
    return float(price)


__all__ = [
    "DEFAULT_PRICE_POLICY",
    "DEFAULT_TICK",
    "PRICE_POLICIES",
    "buy_limit_price",
    "finite_or_none",
]
