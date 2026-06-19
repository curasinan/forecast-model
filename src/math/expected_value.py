"""Expected Value calculation — every trade decision reduces to one number.

    EV = (p × profit) - (q × loss)

Where:
    p = our estimated probability of YES
    q = 1 - p = probability of NO
    profit = payout if YES minus cost = (1 - price)
    loss = cost if NO = price

Positive EV = trade. Negative = don't. The entire game is estimating p
more accurately than the market.
"""


def expected_value(model_prob: float, market_price: float) -> float:
    """Calculate EV of buying YES at market_price given our probability estimate.

    In Polymarket: buy YES at $0.35, if it resolves YES you get $1.00.
    Profit = $0.65, Loss = $0.35.

    Args:
        model_prob: Our estimated probability of YES (0-1)
        market_price: Current YES token price on Polymarket (0-1)

    Returns:
        Expected value per dollar risked. Positive = profitable trade.
    """
    profit_if_yes = 1.0 - market_price  # Payout minus cost
    loss_if_no = market_price  # We lose what we paid

    ev = (model_prob * profit_if_yes) - ((1 - model_prob) * loss_if_no)
    return ev


def expected_value_no(model_prob: float, market_price_yes: float) -> float:
    """Calculate EV of buying NO at market price.

    NO price = 1 - YES price (by the P(YES) + P(NO) = $1 invariant).
    """
    no_price = 1.0 - market_price_yes
    prob_no = 1.0 - model_prob

    profit_if_no = 1.0 - no_price
    loss_if_yes = no_price

    ev = (prob_no * profit_if_no) - (model_prob * loss_if_yes)
    return ev


def best_trade(model_prob: float, market_price_yes: float) -> tuple[str, float]:
    """Determine the best trade direction and its EV.

    Returns:
        Tuple of (direction, ev) where direction is "YES", "NO", or "PASS"
    """
    ev_yes = expected_value(model_prob, market_price_yes)
    ev_no = expected_value_no(model_prob, market_price_yes)

    if ev_yes > ev_no and ev_yes > 0:
        return "YES", ev_yes
    elif ev_no > ev_yes and ev_no > 0:
        return "NO", ev_no
    else:
        return "PASS", max(ev_yes, ev_no)
