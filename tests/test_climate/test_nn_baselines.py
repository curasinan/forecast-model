"""Numpy baselines: raw ensemble, EMOS, trend climatology, bridge plus remainder, persistence."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from climate.bridge import Bridge
from climate.emos import SIGMA_FLOOR, EmosModel, TrendClimatology, target_year
from climate.nn.baselines import (
    NOWCAST_BASELINES,
    SEASONAL_BASELINES,
    bridge_nowcast,
    emos_baseline,
    fit_remainder,
    persistence,
    raw_ensemble,
    trend_climatology,
)
from climate.nn.features import seasonal_frame
from tests.test_climate.nn_fixtures import nowcast_rows

ZERO_ALPHA = {month: 0.0 for month in range(1, 13)}


def _train_test(synthetic_inputs) -> tuple[pd.DataFrame, pd.DataFrame]:
    seas5, era5_K, noaa_C = synthetic_inputs
    bridge = Bridge().fit(era5_K, noaa_C, end="2009-12")
    frame = seasonal_frame(seas5, era5_K, noaa_C, bridge=bridge)
    return frame[target_year(frame) < 2010], frame[target_year(frame) == 2010]


def test_raw_ensemble_returns_the_bridged_mean_and_a_floored_sd():
    frame = pd.DataFrame({"fc_mean_C": [1.0], "fc_sd_C": [0.0]})
    mu, sigma = raw_ensemble(frame)
    assert mu[0] == 1.0
    assert sigma[0] == SIGMA_FLOOR


def test_raw_ensemble_passes_a_larger_sd_through_and_keeps_a_missing_one_missing():
    frame = pd.DataFrame({"fc_mean_C": [0.5, 0.6], "fc_sd_C": [0.25, np.nan]})
    mu, sigma = raw_ensemble(frame)
    np.testing.assert_array_equal(mu, [0.5, 0.6])
    np.testing.assert_array_equal(sigma, [0.25, np.nan])


def test_emos_baseline_equals_a_direct_emos_fit(synthetic_inputs):
    train, test = _train_test(synthetic_inputs)
    mu, sigma = emos_baseline(train, test, trend=True)
    direct = EmosModel(trend=True).fit(train).predict(test)
    np.testing.assert_allclose(mu, direct["mu"])
    np.testing.assert_allclose(sigma, direct["sigma"])


def test_emos_baseline_honours_the_trend_flag(synthetic_inputs):
    train, test = _train_test(synthetic_inputs)
    mu, sigma = emos_baseline(train, test, trend=False)
    direct = EmosModel(trend=False).fit(train).predict(test)
    np.testing.assert_allclose(mu, direct["mu"])
    np.testing.assert_allclose(sigma, direct["sigma"])
    assert not np.allclose(mu, emos_baseline(train, test)[0])


def test_trend_climatology_equals_a_direct_fit(synthetic_inputs):
    train, test = _train_test(synthetic_inputs)
    mu, sigma = trend_climatology(train, test)
    direct_mu, direct_sigma = TrendClimatology().fit(train).predict(test)
    np.testing.assert_allclose(mu, direct_mu)
    np.testing.assert_allclose(sigma, direct_sigma)


def test_seasonal_predictors_return_arrays_aligned_with_the_rows_whatever_the_index(
    synthetic_inputs,
):
    train, test = _train_test(synthetic_inputs)
    flipped = test.iloc[::-1]
    predictors = {
        "raw": raw_ensemble,
        "emos": lambda frame: emos_baseline(train, frame),
        "trend": lambda frame: trend_climatology(train, frame),
    }
    assert set(predictors) == set(SEASONAL_BASELINES)
    for predict in predictors.values():
        mu, sigma = predict(test)
        mu_flipped, sigma_flipped = predict(flipped)
        assert isinstance(mu, np.ndarray) and isinstance(sigma, np.ndarray)
        assert len(mu) == len(sigma) == len(test) == 72  # 12 target months x 6 leads
        np.testing.assert_allclose(mu_flipped, mu[::-1])
        np.testing.assert_allclose(sigma_flipped, sigma[::-1])
        assert [len(a) for a in predict(test.iloc[0:0])] == [0, 0]


def test_remainder_stats_are_zero_at_a_full_month_checkpoint():
    stats = fit_remainder(nowcast_rows(checkpoint="+6", month=3, deltas=[0.0, 0.0, 0.0]))
    assert stats.mean[(3, "+6")] == 0.0
    assert stats.sd[(3, "+6")] == SIGMA_FLOOR


def test_remainder_stats_use_the_sample_sd_and_only_rows_with_a_known_full_month_mean():
    train = pd.concat(
        [
            nowcast_rows("10", month=3, deltas=[0.1, 0.2, 0.3]),
            nowcast_rows("10", month=3, deltas=[np.nan], months=["2005-03"]),  # month still open
            nowcast_rows("10", month=3, deltas=[np.inf], months=["2006-03"]),  # not finite either
        ]
    )
    stats = fit_remainder(train)
    assert stats.mean[(3, "10")] == pytest.approx(0.2)
    assert stats.sd[(3, "10")] == pytest.approx(0.1)  # ddof=1; the population sd is 0.0816
    assert stats.pooled_mean["10"] == pytest.approx(0.2)
    assert stats.pooled_sd["10"] == pytest.approx(0.1)


def test_a_checkpoint_that_sees_no_day_of_the_month_has_no_stats_and_no_prediction():
    blind = nowcast_rows("5", month=3, deltas=[0.1, 0.2, 0.3])
    blind["mtd_mean_K"], blind["days_used"] = np.nan, 0  # the ERA5 lag hides every day
    train = pd.concat([blind, nowcast_rows("10", month=3, deltas=[0.1, 0.2, 0.3])])
    stats = fit_remainder(train)
    assert set(stats.pooled_mean) == {"10"}
    assert set(stats.mean) == {(3, "10")}
    bridge = Bridge(alpha=ZERO_ALPHA, beta=1.0, resid_sd=0.03)
    mu, sigma = bridge_nowcast(train, nowcast_rows("5", month=3, mtd=[np.nan]), bridge=bridge)
    assert np.isnan(mu[0]) and np.isnan(sigma[0])


def test_bridge_nowcast_combines_remainder_and_bridge_residual():
    bridge = Bridge(alpha=ZERO_ALPHA, beta=1.0, resid_sd=0.03)
    train = nowcast_rows(checkpoint="10", month=3, deltas=[0.1, 0.2, 0.3])  # mean 0.2, sd 0.1
    test = nowcast_rows(checkpoint="10", month=3, mtd=[287.0])
    mu, sigma = bridge_nowcast(train, test, bridge=bridge)
    assert mu[0] == pytest.approx(287.2)
    assert sigma[0] == pytest.approx(np.hypot(0.1, 0.03))


def test_bridge_nowcast_maps_the_mean_through_alpha_and_beta_and_scales_the_spread_by_beta():
    bridge = Bridge(alpha={month: float(month) for month in range(1, 13)}, beta=0.5, resid_sd=0.03)
    train = nowcast_rows("10", month=3, deltas=[0.1, 0.2, 0.3])  # mean 0.2, sd 0.1
    mu, sigma = bridge_nowcast(train, nowcast_rows("10", month=3, mtd=[287.0]), bridge=bridge)
    assert mu[0] == pytest.approx(3.0 + 0.5 * 287.2)  # alpha[3] + beta * (mtd + remainder mean)
    assert sigma[0] == pytest.approx(np.sqrt((0.5 * 0.1) ** 2 + 0.03**2))


def test_a_sparse_key_falls_back_to_the_pooled_checkpoint_stats():
    train = pd.concat(
        [
            nowcast_rows("10", month=3, deltas=[0.1, 0.2, 0.3]),
            nowcast_rows("10", month=4, deltas=[0.5]),
        ]
    )
    stats = fit_remainder(train)
    assert stats.mean[(4, "10")] == pytest.approx(stats.pooled_mean["10"])
    assert stats.pooled_mean["10"] == pytest.approx(0.275)


def test_a_key_with_two_rows_is_still_sparse_and_one_with_three_keeps_its_own_stats():
    train = pd.concat(
        [
            nowcast_rows("10", month=3, deltas=[0.1, 0.2, 0.3]),  # three rows
            nowcast_rows("10", month=4, deltas=[0.5, 0.7]),  # two rows
        ]
    )
    stats = fit_remainder(train)
    assert stats.mean[(3, "10")] == pytest.approx(0.2)
    assert stats.sd[(3, "10")] == pytest.approx(0.1)
    assert stats.pooled_mean["10"] == pytest.approx(0.36)
    assert stats.pooled_sd["10"] == pytest.approx(np.std([0.1, 0.2, 0.3, 0.5, 0.7], ddof=1))
    assert stats.mean[(4, "10")] == stats.pooled_mean["10"]
    assert stats.sd[(4, "10")] == stats.pooled_sd["10"]


@pytest.mark.filterwarnings("error")
def test_a_checkpoint_with_one_training_row_has_a_mean_but_no_sd():
    train = nowcast_rows("10", month=3, deltas=[0.4])
    stats = fit_remainder(train)
    assert stats.pooled_mean["10"] == pytest.approx(0.4)
    assert np.isnan(stats.pooled_sd["10"]) and np.isnan(stats.sd[(3, "10")])
    bridge = Bridge(alpha=ZERO_ALPHA, beta=1.0, resid_sd=0.03)
    mu, sigma = bridge_nowcast(train, nowcast_rows("10", month=3, mtd=[287.0]), bridge=bridge)
    assert mu[0] == pytest.approx(287.4)
    assert np.isnan(sigma[0])


def test_an_unseen_key_takes_the_pooled_stats_and_an_unseen_checkpoint_has_no_prediction():
    bridge = Bridge(alpha=ZERO_ALPHA, beta=1.0, resid_sd=0.03)
    train = pd.concat(
        [
            nowcast_rows("10", month=3, deltas=[0.1, 0.2, 0.3]),  # mean 0.2, sd 0.1
            nowcast_rows("10", month=4, deltas=[0.5, 0.7, 0.9]),  # mean 0.7, sd 0.2
        ]
    )
    pooled = [0.1, 0.2, 0.3, 0.5, 0.7, 0.9]
    test = pd.concat(  # every row keeps the index label 0
        [
            nowcast_rows("10", month=7, mtd=[287.0]),  # month never seen in training
            nowcast_rows("10", month=3, mtd=[288.0]),  # a seen key
            nowcast_rows("15", month=3, mtd=[287.0]),  # checkpoint never seen
        ]
    )
    mu, sigma = bridge_nowcast(train, test, bridge=bridge)
    assert mu[0] == pytest.approx(287.0 + np.mean(pooled))
    assert sigma[0] == pytest.approx(np.hypot(np.std(pooled, ddof=1), 0.03))
    assert mu[1] == pytest.approx(288.2)
    assert sigma[1] == pytest.approx(np.hypot(0.1, 0.03))
    assert np.isnan(mu[2]) and np.isnan(sigma[2])


def test_a_complete_month_row_gets_no_remainder_whatever_the_training_stats_say():
    bridge = Bridge(alpha=ZERO_ALPHA, beta=1.0, resid_sd=0.03)
    train = nowcast_rows("+6", month=3, deltas=[0.5, 0.5, 0.5])  # stats claim a 0.5 remainder
    test = pd.concat(
        [
            nowcast_rows("+6", month=3, mtd=[287.0]),  # days_used == days_in_month
            nowcast_rows("+10", month=3, mtd=[287.5]),  # complete, checkpoint never seen
        ]
    )
    mu, sigma = bridge_nowcast(train, test, bridge=bridge)
    np.testing.assert_array_equal(mu, [287.0, 287.5])
    np.testing.assert_array_equal(sigma, [0.03, 0.03])


def test_the_complete_month_rule_reads_the_days_used_not_the_checkpoint_name():
    bridge = Bridge(alpha=ZERO_ALPHA, beta=1.0, resid_sd=0.03)
    train = pd.concat(
        [
            nowcast_rows("+1", month=3, deltas=[0.5, 0.5, 0.5]),
            nowcast_rows("10", month=3, deltas=[0.2, 0.2, 0.2]),
        ]
    )
    short = nowcast_rows("+1", month=3, mtd=[287.0]).assign(days_used=26)  # 26 of 30 days
    whole = nowcast_rows("10", month=3, mtd=[287.5]).assign(days_used=30)  # every day
    mu, sigma = bridge_nowcast(train, pd.concat([short, whole]), bridge=bridge)
    assert mu[0] == pytest.approx(287.5)  # the "+1" remainder is added
    assert sigma[0] == pytest.approx(np.hypot(SIGMA_FLOOR, 0.03))
    np.testing.assert_array_equal([mu[1], sigma[1]], [287.5, 0.03])  # no remainder at all


def test_nowcast_rows_refuses_a_list_shorter_than_the_rows_instead_of_broadcasting_it():
    with pytest.raises(ValueError, match="deltas has 1 values for 3 rows"):
        nowcast_rows("10", month=3, deltas=[0.1], y=[1.0, 1.1, 1.2])
    with pytest.raises(ValueError, match="mtd has 2 values for 3 rows"):
        nowcast_rows("10", month=3, mtd=[287.0, 287.1], deltas=[0.1, 0.2, 0.3])


def test_the_nowcast_baselines_return_empty_arrays_for_an_empty_test_frame():
    bridge = Bridge(alpha=ZERO_ALPHA, beta=1.0, resid_sd=0.03)
    train = nowcast_rows("10", month=3, deltas=[0.1, 0.2, 0.3], y=[1.0, 1.1, 1.2])
    assert [len(a) for a in bridge_nowcast(train, train.iloc[0:0], bridge=bridge)] == [0, 0]
    assert [len(a) for a in persistence(train, train.iloc[0:0])] == [0, 0]


def test_persistence_uses_the_previous_label_and_the_training_change_sd():
    train = nowcast_rows("10", month=1, y=[1.0, 1.2, 1.1], months=["2000-01", "2000-02", "2000-03"])
    test = nowcast_rows("10", month=4, prev_label=[1.1])
    mu, sigma = persistence(train, test)
    assert mu[0] == 1.1
    assert sigma[0] == pytest.approx(np.std(np.diff([1.0, 1.2, 1.1]), ddof=1))


def test_persistence_sd_counts_each_labelled_month_once_in_calendar_order():
    months = ["2000-04", "2000-01", "2000-03", "2000-02", "2000-05"]  # not in calendar order
    y = [1.25, 1.0, 2.0, 1.5, np.nan]  # calendar order 1.0, 1.5, 2.0, 1.25; May is unlabelled
    train = pd.concat([nowcast_rows(cp, month=1, y=y, months=months) for cp in ("10", "15")])
    mu, sigma = persistence(train, nowcast_rows("15", month=6, prev_label=[1.3]))
    assert sigma[0] == pytest.approx(np.std([0.5, 0.5, -0.75], ddof=1))


@pytest.mark.filterwarnings("error")
def test_persistence_sigma_is_missing_below_three_distinct_months():
    two_months = pd.concat(
        [
            nowcast_rows(cp, month=1, y=[1.0, 1.2], months=["2000-01", "2000-02"])
            for cp in ("10", "15")
        ]
    )
    test = pd.concat([nowcast_rows("15", month=3, prev_label=[1.2]), nowcast_rows("10", month=3)])
    mu, sigma = persistence(two_months, test)
    np.testing.assert_array_equal(mu, [1.2, np.nan])
    assert np.isnan(sigma).all() and len(sigma) == 2


def test_persistence_sigma_is_floored_when_the_labels_never_change():
    train = nowcast_rows("10", month=1, y=[0.5, 0.5, 0.5], months=["2000-01", "2000-02", "2000-03"])
    mu, sigma = persistence(train, nowcast_rows("15", month=4, prev_label=[0.5]))
    assert sigma[0] == SIGMA_FLOOR


def test_the_baseline_names_are_the_documented_ones():
    assert SEASONAL_BASELINES == ("raw", "emos", "trend")
    assert NOWCAST_BASELINES == ("bridge", "persistence")


def test_nowcast_rows_carries_the_nowcast_frame_columns_with_documented_defaults():
    rows = nowcast_rows("10", month=3, deltas=[0.1, 0.2])
    assert list(rows.columns) == [
        "target_month",
        "checkpoint",
        "month_of_year",
        "days_in_month",
        "days_used",
        "mtd_mean_K",
        "full_month_mean_K",
        "prev_label_C",
        "y_C",
        "target_year_frac",
    ]
    assert list(rows["target_month"]) == ["2000-03", "2001-03"]
    assert (rows["checkpoint"] == "10").all() and (rows["month_of_year"] == 3).all()
    assert (rows["days_in_month"] == 30).all() and (rows["days_used"] == 10).all()
    np.testing.assert_allclose(rows["mtd_mean_K"], [287.0, 287.0])
    np.testing.assert_allclose(rows["full_month_mean_K"], [287.1, 287.2])
    assert rows["prev_label_C"].isna().all() and rows["y_C"].isna().all()
    np.testing.assert_allclose(rows["target_year_frac"], [2000 + 2.5 / 12, 2001 + 2.5 / 12])
    after_month_end = nowcast_rows("+6", month=3)  # the +n checkpoints see the whole month
    assert after_month_end["days_used"].iloc[0] == 30 and len(after_month_end) == 1
    assert after_month_end["full_month_mean_K"].isna().all()
