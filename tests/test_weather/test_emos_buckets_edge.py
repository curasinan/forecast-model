"""EMOS, buckets, edge, calibration, go-live criteria."""

from __future__ import annotations

import numpy as np

from weather.buckets import Bucket, p_bucket_gaussian, truncate_whole_degree
from weather.calibrate import evaluate_binary_probs
from weather.edge import evaluate_weather_edge, weather_edge
from weather.emos import crps_gaussian, fit_emos, predict_emos
from weather.ml_quantile import default_feature_row, fit_quantile_models
from weather.paper_criteria import GoLiveCriteria, evaluate_go_live
from weather.calibrate import CalibrationReport


def test_truncate_and_bucket():
    assert truncate_whole_degree(70.9) == 70
    b = Bucket(69, 71, "69-70")
    assert b.contains_truncated(69.2)
    assert b.contains_truncated(70.9)
    assert not b.contains_truncated(71.0)


def test_p_bucket_gaussian_sums():
    # Wide normal — middle bucket should have mass
    p = p_bucket_gaussian(70, 2, Bucket(69, 71))
    assert 0.2 < p < 0.6


def test_emos_fit_predict():
    rng = np.random.default_rng(0)
    true_mu = 70 + rng.normal(0, 1, size=40)
    ens_mean = true_mu + 1.0  # biased high
    ens_var = np.full(40, 4.0)
    obs = true_mu + rng.normal(0, 1.5, size=40)
    params = fit_emos(obs, ens_mean, ens_var, station_icao="KLGA", lead_day=1, metric="high")
    pred = predict_emos(params, 72.0, 4.0)
    assert pred.sigma > 0
    assert 60 < pred.mu < 80
    assert crps_gaussian(70, pred.mu, pred.sigma) >= 0


def test_weather_edge_not_politics_path():
    e = weather_edge(0.6, 0.4, 0.04, fee=0.0)
    # ask ≈ 0.42 → edge ≈ 0.18
    assert e > 0.15
    sig = evaluate_weather_edge(0.6, 0.4, 0.04, min_edge=0.05)
    assert sig.tradeable
    assert sig.half_kelly > 0
    sig2 = evaluate_weather_edge(0.51, 0.50, 0.02, min_edge=0.05)
    assert not sig2.tradeable


def test_go_live_fails_cold_start():
    report = CalibrationReport(
        n=10,
        brier=0.3,
        ece=0.2,
        mean_crps=None,
        reliability_slope=0.5,
        market_brier=0.25,
        beats_market=False,
    )
    verdict = evaluate_go_live(report, paper_days=5, criteria=GoLiveCriteria())
    assert not verdict.passed
    assert any("paper_days" in r for r in verdict.reasons)


def test_calibration_beats_market():
    # Perfect model vs bad market
    outcomes = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0] * 5
    model = [0.9 if o else 0.1 for o in outcomes]
    market = [0.5] * len(outcomes)
    report = evaluate_binary_probs(model, outcomes, market_ps=market)
    assert report.beats_market is True


def test_quantile_model_smoke():
    rng = np.random.default_rng(1)
    X = np.column_stack(
        [
            rng.normal(70, 5, 50),
            rng.uniform(1, 4, 50),
            rng.uniform(12, 72, 50),
            rng.integers(1, 12, 50),
            np.zeros(50),
            rng.uniform(0, 2, 50),
        ]
    )
    y = X[:, 0] + rng.normal(0, 1, 50)
    bundle = fit_quantile_models(X, y, n_estimators=20)
    x = default_feature_row(ens_mean=70, ens_std=2, lead_hours=24, month=7)
    p = bundle.p_bucket(x, Bucket(69, 71))
    assert 0.0 <= p <= 1.0
