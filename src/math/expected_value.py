"""Expected Value calculation — every trade decision reduces to one number."""

from __future__ import annotations

from src.domain.carry import CarryModel
from src.domain.fees import FeeModel


def expected_value(
    model_prob: float,
    market_price: float,
    *,
    fee_model: FeeModel | None = None,
    carry_model: CarryModel | None = None,
    holding_days: float = 0.0,
) -> float:
    """Calculate EV of buying YES at market_price given our probability estimate."""
    profit_if_yes = 1.0 - market_price  # Payout minus cost
    loss_if_no = market_price  # We lose what we paid

    ev = (model_prob * profit_if_yes) - ((1 - model_prob) * loss_if_no)
    if fee_model is not None:
        ev -= fee_model.execution_cost(market_price, 1.0)
    if carry_model is not None:
        ev += carry_model.accrual(position_value=market_price, holding_days=holding_days)
    return ev


def expected_value_no(
    model_prob: float,
    market_price_yes: float,
    *,
    fee_model: FeeModel | None = None,
    carry_model: CarryModel | None = None,
    holding_days: float = 0.0,
) -> float:
    """Calculate EV of buying NO at market price."""
    if fee_model is not None:
        no_price = fee_model.no_price_from_yes(market_price_yes)
    else:
        no_price = 1.0 - market_price_yes
    prob_no = 1.0 - model_prob

    profit_if_no = 1.0 - no_price
    loss_if_yes = no_price

    ev = (prob_no * profit_if_no) - (model_prob * loss_if_yes)
    if fee_model is not None:
        ev -= fee_model.execution_cost(no_price, 1.0)
    if carry_model is not None:
        ev += carry_model.accrual(position_value=no_price, holding_days=holding_days)
    return ev


def best_trade(
    model_prob: float,
    market_price_yes: float,
    *,
    fee_model: FeeModel | None = None,
    carry_model: CarryModel | None = None,
    holding_days: float = 0.0,
) -> tuple[str, float]:
    """Determine the best trade direction and its EV."""
    ev_yes = expected_value(
        model_prob,
        market_price_yes,
        fee_model=fee_model,
        carry_model=carry_model,
        holding_days=holding_days,
    )
    ev_no = expected_value_no(
        model_prob,
        market_price_yes,
        fee_model=fee_model,
        carry_model=carry_model,
        holding_days=holding_days,
    )

    if ev_yes > ev_no and ev_yes > 0:
        return "YES", ev_yes
    elif ev_no > ev_yes and ev_no > 0:
        return "NO", ev_no
    else:
        return "PASS", max(ev_yes, ev_no)
