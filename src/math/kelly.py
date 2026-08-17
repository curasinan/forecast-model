"""Kelly Criterion — optimal position sizing for maximum long-run growth.

    f* = (b x p - q) / b

Where:
    f* = optimal fraction of bankroll to bet
    p  = our estimated probability of winning
    b  = net odds received (payout / cost - 1)
    q  = 1 - p (probability of losing)

CRITICAL: Never use full Kelly in practice. Your probability estimates
carry uncertainty. Full Kelly assumes perfect knowledge of your edge,
which leads to systematic overbetting and eventual ruin.

Three levels of Kelly sizing (from most aggressive to most conservative):
    1. Full Kelly  — theoretical optimum, assumes perfect edge knowledge
    2. Half Kelly  — standard institutional approach, ~25% less growth but much safer
    3. Empirical Kelly — Monte Carlo-adjusted, accounts for estimation uncertainty
       f_empirical = f_kelly * (1 - CV_edge)
       where CV_edge = coefficient of variation of edge estimates

Venue frictions (migration spec Stage 4.3): a per-contract fee raises the
effective cost c, and carry (a daily accrual on position value) pays back
r over the holding period. Carry accrues with time while directional edge
does not, so `holding_days` is an explicit sizing input. With win return
a = (1 + r - c) / c and loss return b = (c - r) / c per unit staked:

    f* = p/b - q/a = p*c/(c - r) - q*c/(1 + r - c)

With r = 0 and zero fee this reduces exactly to (b*p - q)/b with
b = (1/price) - 1; defaults reproduce the original math bit-for-bit.
"""

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
    """Calculate Kelly fraction for buying YES tokens.

    In Polymarket:
        Cost = market_price (e.g., $0.35)
        Payout if YES = $1.00
        Net odds b = (1 / market_price) - 1

    With frictions, cost per contract becomes c = price + fee and the
    holding period earns carry r = accrual(price, holding_days); carry
    accrues with time while directional edge does not, which is why
    `holding_days` is an explicit sizing input.

    Args:
        model_prob: Our estimated probability of YES (0-1)
        market_price: Current YES token price (0-1)
        fee_model: Venue fee semantics (per-contract execution cost)
        carry_model: Venue carry semantics (daily accrual on position value)
        holding_days: Expected holding period in days

    Returns:
        Optimal fraction of bankroll to bet. 0 if no edge.
    """
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
        # Carry covers the whole stake: the loss branch is non-negative and
        # Kelly is unbounded — clamp to the full bankroll.
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
    """Conservative Kelly: bet half the optimal fraction.

    This is the standard institutional approach. Sacrifices ~25% of
    long-run growth rate but dramatically reduces drawdown risk and
    accounts for estimation error in your probability model.
    """
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
    """Kelly fraction for buying NO tokens.

    NO price derives from the venue's YES+NO price sum: 1 - YES price by
    default, `fee_model.no_price_from_yes` when a fee model is given.
    Our probability of NO = 1 - model_prob.
    """
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
    """Empirical Kelly: Monte Carlo-adjusted position sizing.

    Instead of trusting our single probability estimate, we simulate
    thousands of possible true probabilities and compute Kelly for each.
    The final sizing accounts for estimation uncertainty.

    Formula: f_empirical = f_kelly * (1 - CV_edge)
    Where CV_edge = std(edge) / |mean(edge)| across simulations.

    When CV is high (uncertain edge), we bet much less.
    When CV is low (confident edge), we approach half-Kelly.

    Fee, carry, and holding period thread through to the base Kelly;
    carry accrues with `holding_days` while directional edge does not.

    Args:
        model_prob: Our model's probability estimate
        market_price: Current market YES price
        confidence: How confident we are in model_prob (0-1)
        n_simulations: Monte Carlo samples
        fee_model: Venue fee semantics
        carry_model: Venue carry semantics
        holding_days: Expected holding period in days

    Returns:
        Empirical Kelly fraction (always <= half_kelly).
    """
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

    # Apply CV haircut: high uncertainty -> aggressive reduction
    # Cap CV at 1.0 so we never go negative
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
    """Calculate actual dollar amount to allocate.

    Carry accrues with time while directional edge does not, so the
    expected holding period is an explicit sizing input here (migration
    spec Stage 4.3).

    Args:
        bankroll: Total available capital
        model_prob: Our estimated probability
        market_price: Current market price
        confidence: Model confidence (0-1), used for empirical Kelly
        max_fraction: Maximum fraction of bankroll per position (safety cap)
        use_empirical: If True, use Monte Carlo-adjusted Kelly
        fee_model: Venue fee semantics
        carry_model: Venue carry semantics
        holding_days: Expected holding period in days

    Returns:
        Dollar amount to allocate to this trade.
    """
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
