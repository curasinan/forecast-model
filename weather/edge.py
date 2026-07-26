"""Weather outcome edge — separate from politics price-change edge_calculator."""

from __future__ import annotations

from dataclasses import dataclass

from src.math.kelly import half_kelly, kelly_fraction


@dataclass(frozen=True)
class WeatherEdgeSignal:
    model_p: float
    market_mid: float
    spread: float
    fee: float
    edge: float
    ask: float
    kelly: float
    half_kelly: float
    tradeable: bool
    reason: str = ""


def weather_edge(
    model_p: float,
    market_mid: float,
    spread: float,
    fee: float = 0.0,
) -> float:
    """Executable edge for buying YES at approx ask = mid + spread/2."""
    return float(model_p - (market_mid + max(spread, 0.0) / 2.0) - fee)


def evaluate_weather_edge(
    model_p: float,
    market_mid: float,
    spread: float,
    *,
    fee: float = 0.0,
    min_edge: float = 0.05,
    min_price: float = 0.02,
    max_price: float = 0.98,
) -> WeatherEdgeSignal:
    ask = market_mid + max(spread, 0.0) / 2.0
    edge = weather_edge(model_p, market_mid, spread, fee=fee)
    reason = ""
    tradeable = True
    if not (0.0 <= model_p <= 1.0):
        tradeable = False
        reason = "invalid_model_p"
    elif ask <= min_price or ask >= max_price:
        tradeable = False
        reason = "price_extreme"
    elif edge < min_edge:
        tradeable = False
        reason = "edge_below_threshold"
    k = kelly_fraction(model_p, ask) if tradeable else 0.0
    hk = half_kelly(model_p, ask) if tradeable else 0.0
    return WeatherEdgeSignal(
        model_p=model_p,
        market_mid=market_mid,
        spread=spread,
        fee=fee,
        edge=edge,
        ask=ask,
        kelly=k,
        half_kelly=hk,
        tradeable=tradeable,
        reason=reason,
    )
