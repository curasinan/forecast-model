"""Edge detection — where our model disagrees with the market.

Edge = model_probability - market_implied_probability

The entire game is estimating probability more accurately than the market.
When we find a gap, we quantify it with EV and Kelly sizing.
"""

from src.math.expected_value import best_trade
from src.math.kelly import half_kelly, kelly_for_no
from src.models.prediction import EdgeSignal


def find_edge(
    question_id: str,
    model_prob: float,
    market_price_yes: float,
    confidence: float = 0.5,
    min_edge: float = 0.05,
    vpin_flag: bool = False,
) -> EdgeSignal:
    """Compare our model probability against market odds.

    Args:
        question_id: Question identifier
        model_prob: Our model's estimated probability of YES (0-1)
        market_price_yes: Current YES token price on Polymarket (0-1)
        confidence: Our confidence in the model estimate (0-1)
        min_edge: Minimum edge to consider significant
        vpin_flag: Whether VPIN indicates informed trading

    Returns:
        EdgeSignal with edge quantification.
    """
    edge_pct = model_prob - market_price_yes

    # Calculate EV for the best direction
    direction, ev = best_trade(model_prob, market_price_yes)

    # Kelly sizing (half-Kelly for safety)
    if edge_pct > 0:
        kelly = half_kelly(model_prob, market_price_yes)
    elif edge_pct < 0:
        kelly = kelly_for_no(model_prob, market_price_yes) * 0.5  # half-Kelly for NO
    else:
        kelly = 0.0

    # Reduce Kelly if VPIN is elevated (informed traders may know something we don't)
    if vpin_flag:
        kelly *= 0.5

    # Reduce Kelly by confidence (uncertain model = smaller position)
    kelly *= confidence

    return EdgeSignal(
        question_id=question_id,
        model_prob=round(model_prob, 4),
        market_prob=round(market_price_yes, 4),
        edge_pct=round(edge_pct, 4),
        expected_value=round(ev, 4),
        kelly_fraction=round(kelly, 4),
        confidence=confidence,
        vpin_flag=vpin_flag,
    )
