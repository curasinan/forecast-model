"""Evaluation metrics — how good are our predictions?

Core metrics for prediction market performance:

1. Brier Score: Mean squared error of probability estimates.
   Perfect = 0.0, worst = 1.0, always-50% = 0.25.
   This is THE standard metric for probabilistic forecasting.

2. Log Loss: Penalizes confident wrong predictions harder than Brier.
   Good for detecting overconfidence.

3. Calibration: Do our 70% predictions happen 70% of the time?
   Grouped into buckets, compared predicted vs actual rate.

4. Cumulative EV: If we bet on every edge signal, what's our P&L?
   The bottom line — did our edge signals actually make money?

5. Accuracy: Simple win rate (did we predict the right side?).
"""

import math
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CalibrationBin:
    """One bin in a calibration curve."""
    lower: float
    upper: float
    predicted_mean: float
    actual_rate: float
    count: int

    @property
    def calibration_error(self) -> float:
        """Absolute difference between predicted and actual."""
        return abs(self.predicted_mean - self.actual_rate)


@dataclass
class MetricsSummary:
    """Complete evaluation of a predictor or ensemble."""
    predictor_name: str
    n_predictions: int
    brier_score: float
    log_loss: float
    accuracy: float
    avg_edge: float
    cumulative_ev: float
    calibration_bins: list[CalibrationBin] = field(default_factory=list)
    expected_calibration_error: float = 0.0

    def to_dict(self) -> dict:
        return {
            "predictor": self.predictor_name,
            "n": self.n_predictions,
            "brier": round(self.brier_score, 4),
            "log_loss": round(self.log_loss, 4),
            "accuracy": round(self.accuracy, 4),
            "avg_edge": round(self.avg_edge, 4),
            "cumulative_ev": round(self.cumulative_ev, 4),
            "ece": round(self.expected_calibration_error, 4),
        }


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    """Brier score: mean squared error of probability estimates.

    BS = (1/N) * Σ (p_i - o_i)²

    Lower is better. 0 = perfect, 1 = worst, 0.25 = always guessing 50%.

    Args:
        predictions: List of predicted probabilities (0-1)
        outcomes: List of actual outcomes (0 or 1)
    """
    if not predictions:
        return 0.25
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def log_loss(predictions: list[float], outcomes: list[int], eps: float = 1e-7) -> float:
    """Log loss (cross-entropy): penalizes confident wrong predictions.

    LL = -(1/N) * Σ [o_i * log(p_i) + (1-o_i) * log(1-p_i)]

    Unlike Brier, log loss goes to infinity for confident wrong predictions.
    A model that says 99% YES on something that resolves NO gets hammered.

    Args:
        predictions: List of predicted probabilities (0-1)
        outcomes: List of actual outcomes (0 or 1)
        eps: Small value to avoid log(0)
    """
    if not predictions:
        return 0.0

    total = 0.0
    for p, o in zip(predictions, outcomes):
        p_clipped = max(eps, min(1 - eps, p))
        total += o * math.log(p_clipped) + (1 - o) * math.log(1 - p_clipped)
    return -total / len(predictions)


def accuracy(predictions: list[float], outcomes: list[int]) -> float:
    """Simple win rate: did we predict the right side?

    Prediction > 0.5 means we predict YES. Check if outcome matches.
    """
    if not predictions:
        return 0.0
    correct = sum(
        1 for p, o in zip(predictions, outcomes)
        if (p > 0.5 and o == 1) or (p < 0.5 and o == 0)
    )
    return correct / len(predictions)


def calibration_curve(
    predictions: list[float],
    outcomes: list[int],
    n_bins: int = 5,
) -> list[CalibrationBin]:
    """Compute calibration bins.

    Groups predictions by probability range and checks:
    do our 60-70% predictions actually happen 60-70% of the time?

    Perfect calibration = the diagonal line on a reliability diagram.
    """
    bin_size = 1.0 / n_bins
    bins: list[CalibrationBin] = []

    for i in range(n_bins):
        lower = i * bin_size
        upper = (i + 1) * bin_size

        in_bin = [
            (p, o) for p, o in zip(predictions, outcomes)
            if lower <= p < upper or (i == n_bins - 1 and p == upper)
        ]

        if in_bin:
            pred_mean = sum(p for p, _ in in_bin) / len(in_bin)
            actual = sum(o for _, o in in_bin) / len(in_bin)
        else:
            pred_mean = (lower + upper) / 2
            actual = 0.0

        bins.append(CalibrationBin(
            lower=lower,
            upper=upper,
            predicted_mean=pred_mean,
            actual_rate=actual,
            count=len(in_bin),
        ))

    return bins


def expected_calibration_error(bins: list[CalibrationBin]) -> float:
    """ECE: weighted average of calibration errors across bins.

    ECE = Σ (n_bin / n_total) * |predicted_mean - actual_rate|

    Lower is better. 0 = perfectly calibrated.
    """
    total_count = sum(b.count for b in bins)
    if total_count == 0:
        return 0.0
    return sum(
        (b.count / total_count) * b.calibration_error
        for b in bins if b.count > 0
    )


def cumulative_ev(
    predictions: list[float],
    outcomes: list[int],
    market_prices: list[float],
    edge_threshold: float = 0.05,
) -> float:
    """Simulate P&L if we bet $1 on every edge signal.

    For each prediction where |model - market| > threshold:
    - If model > market (YES underpriced): buy YES at market price
      - Win: +$(1 - market_price), Lose: -$market_price
    - If model < market (NO underpriced): buy NO at (1 - market_price)
      - Win: +$market_price, Lose: -$(1 - market_price)

    Returns total P&L across all trades.
    """
    total_pnl = 0.0

    for pred, outcome, market in zip(predictions, outcomes, market_prices):
        edge = pred - market

        if abs(edge) < edge_threshold:
            continue  # No edge, no trade

        if edge > 0:
            # Buy YES
            if outcome == 1:
                total_pnl += (1.0 - market)  # Win
            else:
                total_pnl -= market  # Lose
        else:
            # Buy NO
            if outcome == 0:
                total_pnl += market  # Win
            else:
                total_pnl -= (1.0 - market)  # Lose

    return total_pnl


def evaluate_predictor(
    tracker,
    predictor_name: str,
    n_bins: int = 5,
    edge_threshold: float = 0.05,
) -> MetricsSummary | None:
    """Run full evaluation for one predictor using tracked history.

    Args:
        tracker: EvaluationTracker with stored predictions and resolutions
        predictor_name: Which predictor to evaluate
        n_bins: Number of calibration bins
        edge_threshold: Minimum edge for cumulative EV trades

    Returns:
        MetricsSummary or None if not enough data.
    """
    resolved = tracker.get_resolved_predictions()

    # Filter to this predictor
    pairs = [
        (s, o) for s, o in resolved
        if s.predictor_name == predictor_name
    ]

    if not pairs:
        return None

    preds = [s.probability for s, _ in pairs]
    outcomes = [o for _, o in pairs]
    markets = [s.market_price for s, _ in pairs]
    edges = [s.edge_pct for s, _ in pairs]

    bs = brier_score(preds, outcomes)
    ll = log_loss(preds, outcomes)
    acc = accuracy(preds, outcomes)
    avg_edge = sum(edges) / len(edges)
    cum_ev = cumulative_ev(preds, outcomes, markets, edge_threshold)
    cal_bins = calibration_curve(preds, outcomes, n_bins)
    ece = expected_calibration_error(cal_bins)

    return MetricsSummary(
        predictor_name=predictor_name,
        n_predictions=len(pairs),
        brier_score=bs,
        log_loss=ll,
        accuracy=acc,
        avg_edge=avg_edge,
        cumulative_ev=cum_ev,
        calibration_bins=cal_bins,
        expected_calibration_error=ece,
    )


def compare_predictors(
    tracker,
    predictor_names: list[str] | None = None,
) -> list[MetricsSummary]:
    """Evaluate and compare all predictors.

    Returns list of MetricsSummary sorted by Brier score (best first).
    """
    resolved = tracker.get_resolved_predictions()
    if not resolved:
        return []

    # Get unique predictor names from data
    if predictor_names is None:
        predictor_names = list({s.predictor_name for s, _ in resolved})

    summaries = []
    for name in predictor_names:
        summary = evaluate_predictor(tracker, name)
        if summary:
            summaries.append(summary)

    # Sort by Brier score (lower = better)
    summaries.sort(key=lambda s: s.brier_score)
    return summaries
