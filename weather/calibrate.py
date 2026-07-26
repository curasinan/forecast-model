"""Calibration diagnostics for weather probabilities."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.evaluation.metrics import (
    brier_score,
    calibration_curve,
    expected_calibration_error,
)
from weather.emos import crps_gaussian


@dataclass
class CalibrationReport:
    n: int
    brier: float
    ece: float
    mean_crps: float | None
    reliability_slope: float | None
    market_brier: float | None = None
    beats_market: bool | None = None

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "brier": self.brier,
            "ece": self.ece,
            "mean_crps": self.mean_crps,
            "reliability_slope": self.reliability_slope,
            "market_brier": self.market_brier,
            "beats_market": self.beats_market,
        }


def reliability_slope(predictions: list[float], outcomes: list[int]) -> float | None:
    if len(predictions) < 10:
        return None
    x = np.asarray(predictions, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if np.allclose(x.var(), 0):
        return None
    # Simple OLS slope of outcome ~ prediction
    x_mean = x.mean()
    y_mean = y.mean()
    num = ((x - x_mean) * (y - y_mean)).sum()
    den = ((x - x_mean) ** 2).sum()
    return float(num / den) if den else None


def evaluate_binary_probs(
    model_ps: list[float],
    outcomes: list[int],
    *,
    market_ps: list[float] | None = None,
    mus: list[float] | None = None,
    sigmas: list[float] | None = None,
    obs_temps: list[float] | None = None,
) -> CalibrationReport:
    brier = brier_score(model_ps, outcomes)
    bins = calibration_curve(model_ps, outcomes, n_bins=10)
    ece = expected_calibration_error(bins)
    slope = reliability_slope(model_ps, outcomes)
    mean_crps = None
    if mus and sigmas and obs_temps and len(mus) == len(obs_temps):
        mean_crps = float(
            np.mean([crps_gaussian(y, mu, s) for y, mu, s in zip(obs_temps, mus, sigmas)])
        )
    market_brier = None
    beats = None
    if market_ps is not None and len(market_ps) == len(outcomes):
        market_brier = brier_score(market_ps, outcomes)
        beats = brier < market_brier
    return CalibrationReport(
        n=len(outcomes),
        brier=brier,
        ece=ece,
        mean_crps=mean_crps,
        reliability_slope=slope,
        market_brier=market_brier,
        beats_market=beats,
    )
