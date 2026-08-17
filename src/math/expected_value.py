"""Expected Value calculation — every trade decision reduces to one number.

    EV = (p × profit) - (q × loss)

Where:
    p = our estimated probability of YES
    q = 1 - p = probability of NO
    profit = payout if YES minus cost = (1 - price)
    loss = cost if NO = price

Positive EV = trade. Negative = don't. The entire game is estimating p
more accurately than the market.

Venue frictions extend the base formula (migration spec Stage 4.3):
execution fees subtract a fixed cost per contract, and carry (e.g. the
ForecastEx incentive coupon) adds an accrual that grows with holding
time. Directional edge does not depend on how long you hold, but carry
does — so `holding_days` is an explicit input wherever carry applies.
Defaults (no fee model, no carry model) reproduce the original
frictionless math bit-for-bit.
"""

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
    """Calculate EV of buying YES at market_price given our probability estimate.

    In Polymarket: buy YES at $0.35, if it resolves YES you get $1.00.
    Profit = $0.65, Loss = $0.35.

    Args:
        model_prob: Our estimated probability of YES (0-1)
        market_price: Current YES token price (0-1)
        fee_model: Venue fee semantics; subtracts execution_cost(price, 1.0)
        carry_model: Venue carry semantics; adds accrual on position value
        holding_days: Expected holding period — carry accrues with time
            while directional edge does not, so this is an explicit input

    Returns:
        Expected value per contract. Positive = profitable trade.
    """
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
    """Calculate EV of buying NO at market price.

    NO price derives from the venue's YES+NO price sum: 1 - YES price on
    Polymarket, `fee_model.no_price_from_yes` when a fee model is given.
    Fee and carry apply to the NO contract exactly as in `expected_value`.
    """
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
    """Determine the best trade direction and its EV.

    Fee, carry, and holding period thread through to both legs; carry
    accrues with `holding_days` while the directional edge does not.

    Returns:
        Tuple of (direction, ev) where direction is "YES", "NO", or "PASS"
    """
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
