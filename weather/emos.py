"""Ensemble Model Output Statistics (EMOS) with minimum CRPS estimation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm

from weather.buckets import Bucket, p_bucket_gaussian


def crps_gaussian(y: float, mu: float, sigma: float) -> float:
    """Closed-form CRPS for Normal predictive distribution (Gneiting et al.)."""
    sigma = max(sigma, 1e-6)
    z = (y - mu) / sigma
    return float(sigma * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1 / math.sqrt(math.pi)))


@dataclass
class EMOSParams:
    a: float
    b: float  # weight on ensemble mean
    c: float  # intercept variance
    d: float  # spread coefficient
    station_icao: str = ""
    lead_day: int = 0
    metric: str = "high"

    @property
    def model_version(self) -> str:
        return f"emos_v1_{self.station_icao}_{self.metric}_lead{self.lead_day}"


@dataclass
class EMOSPrediction:
    mu: float
    sigma: float
    params: EMOSParams

    def p_bucket(self, bucket: Bucket) -> float:
        return p_bucket_gaussian(self.mu, self.sigma, bucket)


def _predict_mu_sigma(theta: np.ndarray, ens_mean: np.ndarray, ens_var: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, b, c, d = theta
    mu = a + b * ens_mean
    sigma = np.sqrt(np.maximum(c + d * ens_var, 1e-8))
    return mu, sigma


def fit_emos(
    observations: Iterable[float],
    ensemble_means: Iterable[float],
    ensemble_vars: Iterable[float],
    *,
    station_icao: str = "",
    lead_day: int = 0,
    metric: str = "high",
) -> EMOSParams:
    y = np.asarray(list(observations), dtype=float)
    m = np.asarray(list(ensemble_means), dtype=float)
    v = np.asarray(list(ensemble_vars), dtype=float)
    if len(y) < 5:
        # Cold start: identity mean, climatological-ish spread
        spread = float(np.nanstd(y - m)) if len(y) else 3.0
        return EMOSParams(0.0, 1.0, max(spread**2, 1.0), 0.5, station_icao, lead_day, metric)

    def objective(theta: np.ndarray) -> float:
        a, b, raw_c, raw_d = theta
        c = math.exp(raw_c)
        d = math.exp(raw_d)
        mu, sigma = _predict_mu_sigma(np.array([a, b, c, d]), m, v)
        return float(np.mean([crps_gaussian(yi, mui, si) for yi, mui, si in zip(y, mu, sigma)]))

    x0 = np.array([0.0, 1.0, math.log(2.0), math.log(0.5)])
    res = minimize(objective, x0, method="L-BFGS-B")
    a, b, raw_c, raw_d = res.x
    return EMOSParams(
        float(a),
        float(b),
        float(math.exp(raw_c)),
        float(math.exp(raw_d)),
        station_icao=station_icao,
        lead_day=lead_day,
        metric=metric,
    )


def predict_emos(
    params: EMOSParams,
    ensemble_mean: float,
    ensemble_var: float,
) -> EMOSPrediction:
    mu = params.a + params.b * ensemble_mean
    sigma = math.sqrt(max(params.c + params.d * max(ensemble_var, 0.0), 1e-8))
    return EMOSPrediction(mu=mu, sigma=sigma, params=params)


def lead_day_bucket(lead_hours: float) -> int:
    return int(max(0, math.floor(lead_hours / 24.0)))
