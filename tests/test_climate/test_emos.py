"""Gaussian EMOS, baselines, and leakage-free expanding-window scores."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from climate.emos import (
    TREND_ORIGIN,
    EmosModel,
    MonthlyClimatology,
    TrendClimatology,
    crps_gaussian,
    crps_skill_score,
    expanding_window_predictions,
    expanding_window_scores,
    interval_coverage,
    pit,
    pit_histogram,
    summarise_scores,
    target_year,
    time_split,
)


def _frame(n_years: int = 16, leads=(1, 2, 3), seed: int = 0) -> pd.DataFrame:
    """Synthetic warming series with a biased, overdispersed ensemble."""
    rng = np.random.default_rng(seed)
    rows = []
    for year in range(2000, 2000 + n_years):
        for month in range(1, 13):
            tyf = year + (month - 0.5) / 12.0
            true = 0.4 + 0.025 * (tyf - TREND_ORIGIN)
            for lead in leads:
                fc_mean = true + 0.12 + rng.normal(0.0, 0.04)
                fc_sd = 0.18 + 0.02 * (lead - 1) + rng.uniform(0.0, 0.03)
                y = true + rng.normal(0.0, 0.07)
                rows.append(
                    {
                        "fc_mean_C": fc_mean,
                        "fc_sd_C": fc_sd,
                        "y_C": y,
                        "lead_month": int(lead),
                        "target_year_frac": tyf,
                        "month_of_year": month,
                    }
                )
    return pd.DataFrame(rows)


def test_crps_gaussian_known_values_and_floor():
    # Perfect forecast at sigma=1: CRPS = (2 phi(0) - 1/sqrt(pi)) = 0.233...
    assert crps_gaussian(0.0, 0.0, 1.0) == pytest.approx(
        2.0 * (1.0 / np.sqrt(2.0 * np.pi)) - 1.0 / np.sqrt(np.pi), abs=1e-9
    )
    assert crps_gaussian(0.0, 0.0, 0.0) >= 0.0  # sigma floor, not a crash
    assert np.all(crps_gaussian([0.0, 1.0], 0.0, 1.0) >= 0.0)


def test_pit_uniform_when_calibrated():
    rng = np.random.default_rng(1)
    y = rng.normal(0.0, 1.0, 4000)
    p = pit(y, 0.0, 1.0)
    hist = pit_histogram(p, bins=10)
    assert hist["count"].sum() == 4000
    # Chi-square-ish: each bin ~400, allow generous noise
    assert hist["frac"].min() > 0.06
    assert hist["frac"].max() < 0.14


def test_interval_coverage_of_a_calibrated_normal():
    rng = np.random.default_rng(2)
    y = rng.normal(1.0, 0.5, 3000)
    cov = interval_coverage(y, np.full(3000, 1.0), np.full(3000, 0.5), levels=(0.5, 0.9))
    assert cov[0.5] == pytest.approx(0.5, abs=0.04)
    assert cov[0.9] == pytest.approx(0.9, abs=0.03)


def test_emos_recovers_identity_mean_on_unbiased_ensemble():
    rng = np.random.default_rng(3)
    n = 240
    true = rng.normal(0.8, 0.2, n)
    frame = pd.DataFrame(
        {
            "fc_mean_C": true + rng.normal(0.0, 0.03, n),
            "fc_sd_C": np.full(n, 0.12),
            "y_C": true + rng.normal(0.0, 0.05, n),
            "lead_month": np.ones(n, dtype=int),
            "target_year_frac": 2010.0 + np.arange(n) / 12.0,
            "month_of_year": (np.arange(n) % 12) + 1,
        }
    )
    model = EmosModel(trend=False).fit(frame, leads=[1])
    p = model.params[1]
    assert p.converged
    assert p.a == pytest.approx(0.0, abs=0.08)
    assert p.b == pytest.approx(1.0, abs=0.15)
    assert p.c > 0.0 and p.d >= 0.0
    pred = model.predict(frame)
    assert np.all(np.isfinite(pred["mu"]))
    assert np.all(pred["sigma"] > 0)
    assert float(np.mean(model.crps(frame))) < 0.2


def test_emos_json_roundtrip_and_missing_lead_is_nan():
    frame = _frame(n_years=10, leads=(1, 2))
    model = EmosModel(trend=True).fit(frame, leads=[1, 2])
    text = model.to_json()
    payload = json.loads(text)
    assert payload["trend"] is True
    restored = EmosModel.from_json(text)
    assert restored.params.keys() == model.params.keys()
    pred_a = model.predict(frame)
    pred_b = restored.predict(frame)
    assert np.allclose(pred_a["mu"], pred_b["mu"])
    assert np.allclose(pred_a["sigma"], pred_b["sigma"])

    other = frame.copy()
    other["lead_month"] = 6
    pred6 = model.predict(other)
    assert pred6["mu"].isna().all() and pred6["sigma"].isna().all()


def test_emos_skips_sparse_leads_and_errors_if_none_fit():
    frame = _frame(n_years=4, leads=(1,))
    model = EmosModel().fit(frame, leads=[1, 9], min_rows=8)
    assert 1 in model.params
    assert 9 in model.skipped
    with pytest.raises(ValueError, match="no lead"):
        EmosModel().fit(frame, leads=[9], min_rows=8)


def test_monthly_climatology_does_not_see_test_year_outlier():
    frame = _frame(n_years=12, leads=(1,))
    train, test = time_split(frame, train_through_year=2008)
    clim = MonthlyClimatology().fit(frame.loc[train])
    mu, sd = clim.predict(frame.loc[test])
    assert np.all(sd > 0)
    # Inject a 100 C spike only in the test slice; climatology was already fit.
    spiked = frame.loc[test].copy()
    spiked["y_C"] = 100.0
    mu2, _ = clim.predict(spiked)
    assert np.allclose(mu, mu2)
    assert float(np.mean(mu)) < 2.0


def test_trend_climatology_captures_warming():
    frame = _frame(n_years=16, leads=(1,))
    train, test = time_split(frame, train_through_year=2010)
    tclim = TrendClimatology().fit(frame.loc[train])
    clim = MonthlyClimatology().fit(frame.loc[train])
    mu_t, _ = tclim.predict(frame.loc[test])
    mu_c, _ = clim.predict(frame.loc[test])
    y = frame.loc[test, "y_C"].to_numpy()
    # Trend clim should be closer to a warming test set than a frozen monthly mean.
    assert float(np.mean((mu_t - y) ** 2)) < float(np.mean((mu_c - y) ** 2))


def test_time_split_uses_target_year_not_init():
    frame = pd.DataFrame(
        {
            "fc_mean_C": [1.0, 1.0],
            "fc_sd_C": [0.1, 0.1],
            "y_C": [1.0, 1.1],
            "lead_month": [6, 1],
            "target_year_frac": [2010.96, 2011.04],  # Dec 2010 / Jan 2011
            "month_of_year": [12, 1],
        }
    )
    train, test = time_split(frame, train_through_year=2010)
    assert train.tolist() == [True, False]
    assert test.tolist() == [False, True]
    assert target_year(frame).tolist() == [2010, 2011]


def test_expanding_window_excludes_test_year_from_training():
    frame = _frame(n_years=14, leads=(1, 2))
    # Plant an impossible label only in 2012 so a leaking fit would chase it.
    years = target_year(frame)
    leaked = frame.copy()
    leaked.loc[years == 2012, "y_C"] = 50.0
    pred = expanding_window_predictions(
        leaked, first_test_year=2012, last_test_year=2012, trend=False, leads=[1, 2]
    )
    assert not pred.empty
    assert set(pred["test_year"].unique()) == {2012}
    # Model trained without 2012 cannot put mu near 50.
    assert float(np.nanmean(np.abs(pred["mu_emos"]))) < 5.0
    assert float(np.nanmean(np.abs(pred["mu_clim"]))) < 5.0
    assert float(np.nanmean(np.abs(pred["mu_trend"]))) < 5.0


def test_expanding_window_scores_and_skill():
    frame = _frame(n_years=16, leads=(1, 2))
    scores = expanding_window_scores(
        frame, first_test_year=2008, last_test_year=2014, trend=False, leads=[1, 2]
    )
    assert set(scores.columns) >= {
        "test_year",
        "lead_month",
        "n",
        "crps_emos",
        "skill_vs_clim",
        "skill_vs_trend",
        "cover50_emos",
        "cover90_emos",
    }
    assert scores["n"].sum() > 0
    summary = summarise_scores(scores)
    assert list(summary["lead_month"]) == [1, 2]
    # Skill vs a frozen climatology should be positive on a warming series.
    assert float(summary["skill_vs_clim"].mean()) > 0.0
    # CRPS skill of a model against itself is 0.
    assert crps_skill_score(scores["crps_emos"], scores["crps_emos"]) == pytest.approx(0.0)


def test_emos_requires_frame_columns():
    with pytest.raises(KeyError, match="missing"):
        EmosModel().fit(pd.DataFrame({"fc_mean_C": [1.0]}), leads=[1])
