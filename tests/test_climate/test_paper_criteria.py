"""Climate go-live and fee-aware edge gates (re-scoped off weather/paper_criteria)."""

from __future__ import annotations

import pandas as pd
import pytest

from climate.paper_criteria import (
    DEFAULT_MIN_EDGE,
    ClimateEdgeGate,
    ClimateGoLiveCriteria,
    apply_edge_gate,
    evaluate_climate_edge,
    evaluate_hindcast_go_live,
    min_edge_from_spread,
)
from weather.paper_criteria import GoLiveCriteria as WeatherGoLiveCriteria


def test_climate_bar_is_not_the_weather_bar():
    weather = WeatherGoLiveCriteria()
    climate = ClimateGoLiveCriteria()
    assert climate.min_resolved_months < weather.min_resolved_buckets
    assert climate.min_paper_days > weather.min_paper_days
    assert climate.min_resolved_months == 12
    assert climate.min_paper_days == 365


def test_min_edge_clears_commission_plus_half_spread():
    assert min_edge_from_spread(0.01) == pytest.approx(DEFAULT_MIN_EDGE)  # floor 0.02
    # A 4-cent book: 0.01 commission + 0.02 half-spread = 0.03
    assert min_edge_from_spread(0.04) == pytest.approx(0.03)


def test_evaluate_climate_edge_tradeable_and_rejects():
    sig = evaluate_climate_edge(0.70, 0.40, 0.02)
    assert sig.tradeable
    assert sig.edge == pytest.approx(0.30)
    assert sig.ev_after_fee == pytest.approx(0.30 - 0.01)
    assert sig.half_kelly > 0
    assert evaluate_climate_edge(0.51, 0.50, 0.02).reason == "edge_below_threshold"
    assert evaluate_climate_edge(0.80, 0.02, 0.02).reason == "price_extreme"
    assert evaluate_climate_edge(0.80, 0.40, 0.02, suspect_quote=True).reason == "suspect_quote"


def test_hindcast_go_live_fails_cold_and_live_paper_is_optional():
    summary = pd.DataFrame(
        {
            "lead_month": [1, 2, 3],
            "n": [5, 5, 5],
            "skill_vs_trend": [0.1, 0.1, 0.1],
            "cover50_emos": [0.5, 0.5, 0.5],
            "cover90_emos": [0.9, 0.9, 0.9],
        }
    )
    cold = evaluate_hindcast_go_live(summary, require_live_paper=False)
    assert not cold.passed
    assert any("n=" in r for r in cold.reasons)

    good = pd.DataFrame(
        {
            "lead_month": [1, 2, 3],
            "n": [80, 80, 80],
            "skill_vs_trend": [0.05, 0.04, 0.03],
            "cover50_emos": [0.48, 0.51, 0.50],
            "cover90_emos": [0.88, 0.91, 0.90],
        }
    )
    hindcast = evaluate_hindcast_go_live(good, require_live_paper=False)
    assert hindcast.passed
    live = evaluate_hindcast_go_live(
        good, paper_days=10, resolved_months=1, require_live_paper=True
    )
    assert not live.passed
    assert any("paper_days" in r for r in live.reasons)
    assert any("resolved_months" in r for r in live.reasons)


def test_apply_edge_gate_adds_tradeable_column():
    ladder = pd.DataFrame(
        {
            "p_model": [0.7, 0.51],
            "yes_mid": [0.40, 0.50],
            "spread": [0.02, 0.02],
            "suspect_quote": [False, False],
        }
    )
    out = apply_edge_gate(ladder, gate=ClimateEdgeGate())
    assert out["tradeable"].tolist() == [True, False]
