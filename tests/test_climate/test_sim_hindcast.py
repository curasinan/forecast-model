"""S3 hindcast: the strict forecaster, the synthetic markets, the venue and the CLI."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from climate.emos import TrendClimatology
from climate.sim.forecast import print_times
from climate.sim.hindcast import (
    MarketModel,
    MarketSpec,
    StrictForecaster,
    breakeven,
    build_venue,
    expiry_for,
    hindcast_decisions,
    hindcast_metrics,
    settlement_values,
    yearly_table,
)
from climate.sim.replay import Policy, Replay, ReplayConfig, settlements, stop_times, target_months
from scripts.research import gtm_sim_hindcast
from tests.test_climate import nn_fixtures
from tests.test_climate.sim_replay_fixtures import utc


@pytest.fixture(scope="module")
def inputs():
    seas5, era5, labels = nn_fixtures.synthetic_inputs()
    return seas5, era5, labels, StrictForecaster(seas5, era5, labels), print_times(labels.index, None)


def _market(inputs, spec, seed=0):
    _, _, labels, model, printed = inputs
    return MarketModel(spec, labels, printed, model, seed=seed)


T = utc(2008, 6, 14, 14, 0)
TARGETS = ["2008-06", "2008-07", "2008-08", "2008-09"]


def test_the_bridge_is_refitted_each_year_on_the_previous_years(inputs):
    model = inputs[3]
    assert model.year(2008).frame.attrs["bridge"].fit_end == "2007-12"
    assert model.at(T).labels_through == "2008-05"  # printed 2008-06-12


def test_m0_is_the_trend_climatology_of_the_printed_labels(inputs):
    labels = inputs[2]
    got = _market(inputs, MarketSpec("m0")).at(T, TARGETS)
    printed = labels.loc[:"2008-05"]
    frame = pd.DataFrame({"y_C": printed.to_numpy(),
                          "month_of_year": [int(m[5:]) for m in printed.index],
                          "target_year_frac": [int(m[:4]) + (int(m[5:]) - 0.5) / 12 for m in printed.index]})
    mu, sd = TrendClimatology().fit(frame).predict(pd.DataFrame({
        "month_of_year": [7], "target_year_frac": [2008 + 6.5 / 12]}))
    assert got["2008-07"] == pytest.approx((mu[0], max(sd[0], 0.02)))


def test_m1_widens_with_the_horizon(inputs):
    got = _market(inputs, MarketSpec("m1")).at(T, TARGETS)
    sigmas = [got[m][1] for m in TARGETS]
    assert sigmas == sorted(sigmas) and sigmas[0] < sigmas[-1]


def test_m2_is_the_freshest_released_raw_ensemble(inputs):
    model = inputs[3]
    got = _market(inputs, MarketSpec("m2")).at(T, ["2008-08"])
    frame = model.frame_at(T)
    row = frame[frame["target_month"] == "2008-08"].sort_values("lead_month").iloc[0]
    assert pd.Timestamp(row["init_date"]).strftime("%Y-%m") == "2008-06"
    assert got["2008-08"] == pytest.approx((row["fc_mean_C"], max(row["fc_sd_C"], 0.02)))


def test_m3_and_blends_follow_the_model(inputs):
    model = inputs[3].at(T).predictive
    m3 = _market(inputs, MarketSpec("m3", bias=-0.13, ratio=1.5)).at(T, ["2008-08"])
    assert m3["2008-08"] == pytest.approx((model["2008-08"][0] - 0.13, model["2008-08"][1] * 1.5))
    m0 = _market(inputs, MarketSpec("m0")).at(T, ["2008-08"])["2008-08"]
    half = _market(inputs, MarketSpec("m0", weight=0.5)).at(T, ["2008-08"])["2008-08"]
    assert half[0] == pytest.approx((m0[0] + model["2008-08"][0]) / 2)
    same = _market(inputs, MarketSpec("m0", weight=1.0)).at(T, ["2008-08"])["2008-08"]
    assert same[0] == pytest.approx(model["2008-08"][0])


def test_the_informed_market_is_seeded_and_centred_on_the_label(inputs):
    labels = inputs[2]
    a = _market(inputs, MarketSpec("informed", sd=0.05), seed=3).at(T, TARGETS)
    b = _market(inputs, MarketSpec("informed", sd=0.05), seed=3).at(T, TARGETS)
    c = _market(inputs, MarketSpec("informed", sd=0.05), seed=4).at(T, TARGETS)
    assert a == b and a != c
    errors = [a[m][0] - labels[m] for m in TARGETS]
    assert all(abs(e) < 0.3 for e in errors) and a["2008-07"][1] == 0.05


def test_specs_are_validated():
    with pytest.raises(ValueError):
        MarketSpec("m9")
    with pytest.raises(ValueError):
        MarketSpec("m0", weight=1.5)
    with pytest.raises(ValueError):
        MarketSpec("informed")
    assert MarketSpec("m1", weight=0.25).name == "m1_w0.25"
    assert MarketSpec("informed", sd=0.08).name == "informed_sd0.08"


def test_the_venue_lists_fixed_ladders_and_settles_the_last_months(inputs):
    decisions = hindcast_decisions(2008, 2008)
    venue = build_venue(_market(inputs, MarketSpec("m0")), decisions, last_month="2008-12",
                        months_ahead=2)
    frame = venue.panel.frame
    expiries = set(frame["expiry"])
    assert expiry_for("2008-12") in expiries and expiry_for("2009-01") not in expiries
    by_target = frame.assign(target=frame["expiry"]).groupby("target")["strike"]
    assert all(len(set(np.round(s, 2))) == 11 for _, s in by_target)
    yes = frame[frame["right"] == "C"].set_index(["ts", "expiry", "strike"])
    no = frame[frame["right"] == "P"].set_index(["ts", "expiry", "strike"])
    assert np.allclose(no["bid"], 1 - yes.loc[no.index, "ask"])
    assert ((frame["bid"] * 100).round(6) % 1 == 0).all() and (frame["ask"] > frame["bid"]).all()
    assert venue.panel.times[-1] > pd.Timestamp(utc(2009, 1, 12))  # after December's print


def test_settlement_noise_is_seeded(inputs):
    labels = inputs[2]
    clean = settlement_values(labels, ["2008-07"])
    noisy = settlement_values(labels, ["2008-07"], noise=0.03, seed=1)
    again = settlement_values(labels, ["2008-07"], noise=0.03, seed=1)
    assert clean["2008-07"][0] == round(float(labels["2008-07"]), 2)
    assert noisy == again and noisy != clean


def test_breakeven():
    assert breakeven([(0, 10.0), (0.5, 5.0), (1.0, -5.0)]) == pytest.approx(0.75)
    assert breakeven([(0, 10.0), (0.5, 5.0)]) is None
    assert breakeven([(0.04, -6.0), (0.08, 2.0)], rising=True) == pytest.approx(0.07)


def _run(inputs, spec, tmp_path, first=2006, last=2007):
    _, _, labels, model, printed = inputs
    decisions = hindcast_decisions(first, last)
    venue = build_venue(MarketModel(spec, labels, printed, model), decisions, last_month=f"{last}-12")
    contracts = venue.panel.contracts()
    target_of = target_months(contracts, None)
    months = sorted(set(target_of.values()))
    settle = settlements(target_of, settlement_values(labels, months), print_times(months, None))
    result = Replay(venue.panel, contracts, None, model, settle, stop_times(contracts, target_of, None),
                    ReplayConfig(policy=Policy.parse("ask")), tmp_path / spec.name).run(decisions)
    table = yearly_table(result, list(range(first, last + 1)))
    return result, table, hindcast_metrics(table, result)


def test_a_market_that_is_the_model_gives_no_trade(inputs, tmp_path):
    result, _, metrics = _run(inputs, MarketSpec("m0", weight=1.0), tmp_path)
    assert result.orders.empty and metrics["total_pnl"] == 0.0


def test_a_badly_wrong_market_pays_and_every_trade_settles(inputs, tmp_path):
    result, table, metrics = _run(inputs, MarketSpec("informed", sd=0.4), tmp_path)
    assert metrics["total_pnl"] > 0 and metrics["orders_filled"] > 0
    assert result.orders["settled"].all() and metrics["reconcile_aborts"] == 0
    assert table["year"].tolist() == [2006, 2007]
    assert table["pnl"].sum() == pytest.approx(metrics["total_pnl"])
    assert metrics["pnl_before"] + metrics["pnl_from"] == pytest.approx(metrics["total_pnl"])


def test_the_cli_writes_the_grid_and_the_break_evens(tmp_path, synthetic_root):
    out = tmp_path / "run"
    code = gtm_sim_hindcast.main(["--root", str(synthetic_root), "--first-year", "2006",
                                  "--last-year", "2006", "--proxies", "m0,m3", "--blends", "0,1",
                                  "--informed-sd", "0.02,0.4", "--out", str(out)])
    assert code == 0
    grid = pd.read_parquet(out / "grid.parquet")
    assert set(grid["cell"]) == {"m0_w0", "m0_w1", "m3_w0", "m3_w1", "informed_sd0.02",
                                 "informed_sd0.4"}
    assert grid.set_index("cell").loc["m0_w1", "orders_filled"] == 0
    assert (out / "cells" / "m0_w0" / "yearly.parquet").exists()
    assert (out / "cells" / "m0_w0" / "paper_book.db").exists()
    metrics = json.loads((out / "metrics.json").read_text())
    assert "trend climatology" in metrics["breakeven"]
    assert "## Break-even" in (out / "report.md").read_text()


@pytest.mark.parametrize("bad", [["--proxies", "m7"], ["--blends", "0,1.5"], ["--informed-sd", "-1"],
                                 ["--first-year", "2010", "--last-year", "2009"], ["--day", "5"],
                                 ["--proxies", "", "--informed-sd", ""]])
def test_usage_errors_exit_2(tmp_path, synthetic_root, bad):
    args = ["--root", str(synthetic_root), "--first-year", "2006", "--last-year", "2006",
            "--out", str(tmp_path / "run"), *bad]
    assert gtm_sim_hindcast.main(args) == 2
    assert not (tmp_path / "run").exists()
