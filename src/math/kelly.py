"""Kelly Criterion — optimal position sizing for maximum long-run growth."""

from __future__ import annotations

from src.domain.carry import CarryModel
from src.domain.fees import FeeModel


def kelly_fraction(
    model_prob: float,
    market_price: float,
    *,
    fee_model: FeeModel | None = None,
    carry_model: CarryModel | None = None,
    holding_days: float = 0.0,
) -> float:
    """Calculate Kelly fraction for buying YES tokens."""
    if market_price <= 0.01 or market_price >= 0.99:
        return 0.0

    fee = fee_model.execution_cost(market_price, 1.0) if fee_model is not None else 0.0
    r = carry_model.accrual(market_price, holding_days) if carry_model is not None else 0.0
    c = market_price + fee
    if c >= 0.99 or c <= 0.01:
        return 0.0

    p = model_prob
    q = 1.0 - p

    if fee == 0.0 and r == 0.0:
        # Frictionless path, kept bit-identical to the original formula.
        b = (1.0 / market_price) - 1.0
        f = (b * p - q) / b
        return max(0.0, f)

    if r >= c:
        return 1.0

    f = p * c / (c - r) - q * c / (1.0 + r - c)

    # Negative Kelly means no edge (or negative edge) — don't bet
    return max(0.0, f)


def half_kelly(
    model_prob: float,
    market_price: float,
    *,
    fee_model: FeeModel | None = None,
    carry_model: CarryModel | None = None,
    holding_days: float = 0.0,
) -> float:
    """Conservative Kelly: bet half the optimal fraction."""
    return (
        kelly_fraction(
            model_prob,
            market_price,
            fee_model=fee_model,
            carry_model=carry_model,
            holding_days=holding_days,
        )
        * 0.5
    )


def kelly_for_no(
    model_prob: float,
    market_price_yes: float,
    *,
    fee_model: FeeModel | None = None,
    carry_model: CarryModel | None = None,
    holding_days: float = 0.0,
) -> float:
    """Kelly fraction for buying NO tokens."""
    if fee_model is not None:
        no_price = fee_model.no_price_from_yes(market_price_yes)
    else:
        no_price = 1.0 - market_price_yes
    prob_no = 1.0 - model_prob
    return kelly_fraction(
        prob_no,
        no_price,
        fee_model=fee_model,
        carry_model=carry_model,
        holding_days=holding_days,
    )


def empirical_kelly(
    model_prob: float,
    market_price: float,
    confidence: float = 0.5,
    n_simulations: int = 10_000,
    *,
    fee_model: FeeModel | None = None,
    carry_model: CarryModel | None = None,
    holding_days: float = 0.0,
) -> float:
    """Empirical Kelly: Monte Carlo-adjusted position sizing."""
    from src.math.monte_carlo import simulate_edge

    edge_dist = simulate_edge(
        model_probability=model_prob,
        model_confidence=confidence,
        market_price=market_price,
        n_simulations=n_simulations,
    )

    # Base Kelly from point estimate
    base_kelly = half_kelly(
        model_prob,
        market_price,
        fee_model=fee_model,
        carry_model=carry_model,
        holding_days=holding_days,
    )

    if base_kelly <= 0:
        return 0.0

    cv = min(1.0, edge_dist.cv_edge)
    empirical_f = base_kelly * (1.0 - cv)

    # Floor at 0, ceiling at half-Kelly
    return max(0.0, min(empirical_f, base_kelly))


def position_size(
    bankroll: float,
    model_prob: float,
    market_price: float,
    confidence: float = 0.5,
    max_fraction: float = 0.25,
    use_empirical: bool = True,
    *,
    fee_model: FeeModel | None = None,
    carry_model: CarryModel | None = None,
    holding_days: float = 0.0,
) -> float:
    """Calculate actual dollar amount to allocate."""
    if use_empirical:
        f = empirical_kelly(
            model_prob,
            market_price,
            confidence,
            fee_model=fee_model,
            carry_model=carry_model,
            holding_days=holding_days,
        )
    else:
        f = half_kelly(
            model_prob,
            market_price,
            fee_model=fee_model,
            carry_model=carry_model,
            holding_days=holding_days,
        )

    f = min(f, max_fraction)  # Never exceed max per-trade allocation
    return bankroll * f
