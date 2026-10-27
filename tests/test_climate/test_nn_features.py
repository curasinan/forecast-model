"""Seasonal features: member quantiles, the seasonal frame and the synthetic inputs."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from climate.bridge import Bridge
from climate.dataset import build_frame_from_inputs, month_of_year, target_year_frac
from climate.nn.features import MIN_MEMBERS, SEASONAL_FEATURES, member_quantiles, seasonal_frame
from tests.test_climate import nn_fixtures

QUANTILE_NAMES = ("q10", "q25", "q50", "q75", "q90")
QUANTILE_K = [f"{name}_K" for name in QUANTILE_NAMES]


def _members(init: str, lead: int, values) -> pd.DataFrame:
    values = np.asarray(values, dtype=float)
    return pd.DataFrame(
        {
            "init_date": pd.Timestamp(init),
            "lead_month": lead,
            "member": np.arange(len(values)),
            "t2m_global_K": values,
        }
    )


def _row(frame: pd.DataFrame, init: str, lead: int) -> pd.Series:
    return frame[(frame["init_date"] == init) & (frame["lead_month"] == lead)].iloc[0]


def _truth(target_year_frac_values) -> pd.Series:
    return 0.4 + 0.025 * (target_year_frac_values - 2010.0)


def test_member_quantiles_match_numpy_on_a_known_ensemble():
    seas5 = _members(init="2020-01-01", lead=1, values=np.arange(1.0, 26.0))  # 25 members 1..25
    q = member_quantiles(seas5).iloc[0]
    assert q["q50_K"] == pytest.approx(13.0)
    assert q["q10_K"] == pytest.approx(np.quantile(np.arange(1.0, 26.0), 0.1))
    assert q["n_finite"] == 25


def test_member_quantiles_ignore_nan_and_require_ten_members():
    seas5 = _members(init="2020-01-01", lead=1, values=[1.0, 2.0, np.nan] * 3)  # 6 finite
    q = member_quantiles(seas5).iloc[0]
    assert np.isnan(q["q50_K"]) and q["n_finite"] == 6


def test_member_quantiles_use_finite_members_only():
    values = [*np.arange(1.0, 13.0), np.nan, np.nan, np.inf]  # 12 finite members 1..12
    q = member_quantiles(_members(init="2020-01-01", lead=1, values=values)).iloc[0]
    assert q["q50_K"] == pytest.approx(6.5) and q["n_finite"] == 12


def test_nine_finite_members_are_dropped_and_ten_are_kept():
    assert MIN_MEMBERS == 10
    nine = member_quantiles(_members("2020-01-01", 1, [*np.arange(1.0, 10.0), np.nan])).iloc[0]
    ten = member_quantiles(_members("2020-01-01", 1, np.arange(1.0, 11.0))).iloc[0]
    assert nine["n_finite"] == 9 and nine[QUANTILE_K].isna().all()
    assert ten["n_finite"] == 10 and ten[QUANTILE_K].notna().all()


def test_the_seasonal_features_are_the_agreed_list_in_order():
    assert SEASONAL_FEATURES == [
        "fc_mean_C",
        "fc_sd_C",
        "q10_C",
        "q25_C",
        "q50_C",
        "q75_C",
        "q90_C",
        "target_year_frac",
        "moy_sin",
        "moy_cos",
        "lead_1",
        "lead_2",
        "lead_3",
        "lead_4",
        "lead_5",
        "lead_6",
    ]


@pytest.mark.parametrize("lead", [0, 7])
def test_a_lead_outside_one_to_six_is_refused_and_named(lead, synthetic_inputs):
    seas5, era5_K, noaa_C = synthetic_inputs
    odd = seas5.copy()
    cell = (odd["init_date"] == "2010-03-01") & (odd["lead_month"] == 1)
    odd.loc[cell, "lead_month"] = lead
    bridge = Bridge().fit(era5_K, noaa_C, end="2015-12")
    with pytest.raises(ValueError, match=f"lead_month {lead} is outside 1..6"):
        seasonal_frame(odd, era5_K, noaa_C, bridge=bridge)


def test_seasonal_frame_carries_features_availability_and_the_bridge(synthetic_inputs):
    seas5, era5_K, noaa_C = synthetic_inputs
    bridge = Bridge().fit(era5_K, noaa_C, end="2015-12")
    frame = seasonal_frame(seas5, era5_K, noaa_C, bridge=bridge)
    assert set(SEASONAL_FEATURES) <= set(frame.columns)
    row = frame[(frame["init_date"] == "2010-03-01") & (frame["lead_month"] == 2)].iloc[0]
    assert row["decision_utc"] == datetime(2010, 3, 13, 12, tzinfo=timezone.utc)
    assert row["avail_label_utc"] == datetime(2010, 5, 12, tzinfo=timezone.utc)  # target 2010-04
    assert row["lead_2"] == 1.0 and row[["lead_1", "lead_3"]].sum() == 0.0
    q50_K = (
        member_quantiles(seas5)
        .set_index(["init_date", "lead_month"])
        .loc[(pd.Timestamp("2010-03-01"), 2), "q50_K"]
    )
    assert row["q50_C"] == pytest.approx(bridge.predict_mean(q50_K, 4))
    assert frame.attrs["bridge"] is bridge


def test_seasonal_frame_bridges_every_quantile_and_encodes_month_lead_and_times(synthetic_inputs):
    seas5, era5_K, noaa_C = synthetic_inputs
    bridge = Bridge().fit(era5_K, noaa_C, end="2015-12")
    frame = seasonal_frame(seas5, era5_K, noaa_C, bridge=bridge)
    quantiles = member_quantiles(seas5).set_index(["init_date", "lead_month"])
    keys = pd.MultiIndex.from_frame(frame[["init_date", "lead_month"]])
    for name in QUANTILE_NAMES:
        q_K = quantiles.loc[keys, f"{name}_K"].to_numpy()
        expected = bridge.predict_mean(q_K, frame["month_of_year"].to_numpy())
        np.testing.assert_allclose(frame[f"{name}_C"], expected)
    row = _row(frame, "2010-03-01", 2)  # target 2010-04
    assert row["moy_sin"] == pytest.approx(math.sqrt(3) / 2)
    assert row["moy_cos"] == pytest.approx(-0.5)
    assert row["init_month"] == "2010-03"
    lead_columns = [f"lead_{lead}" for lead in range(1, 7)]
    decoded = frame[lead_columns].to_numpy() @ np.arange(1, 7)
    np.testing.assert_array_equal(decoded, frame["lead_month"])
    assert frame["decision_utc"].equals(frame["avail_seas5_utc"])
    assert str(frame["decision_utc"].dt.tz) == "UTC"
    assert str(frame["avail_label_utc"].dt.tz) == "UTC"
    assert (frame["avail_label_utc"] > frame["decision_utc"]).all()


def test_a_thin_ensemble_keeps_its_row_but_has_no_quantile_features(synthetic_inputs):
    seas5, era5_K, noaa_C = synthetic_inputs
    in_cell = (seas5["init_date"] == "2010-03-01") & (seas5["lead_month"] == 2)
    thin = seas5[~(in_cell & (seas5["member"] >= 5))]
    frame = seasonal_frame(thin, era5_K, noaa_C, bridge=Bridge().fit(era5_K, noaa_C, end="2015-12"))
    row = _row(frame, "2010-03-01", 2)
    assert row[[f"{name}_C" for name in QUANTILE_NAMES]].isna().all()
    assert np.isfinite(row["fc_mean_C"]) and frame["q50_C"].isna().sum() == 1


def test_synthetic_inputs_are_deterministic_per_seed():
    first, again, other = (nn_fixtures.synthetic_inputs(seed=seed) for seed in (3, 3, 4))
    pd.testing.assert_frame_equal(first[0], again[0])
    pd.testing.assert_series_equal(first[1], again[1])
    pd.testing.assert_series_equal(first[2], again[2])
    assert not first[0]["t2m_global_K"].equals(other[0]["t2m_global_K"])


def test_synthetic_inputs_follow_the_documented_truth_bias_and_spread():
    seas5, era5_K, noaa_C = nn_fixtures.synthetic_inputs()
    assert list(seas5.columns) == ["init_date", "lead_month", "member", "t2m_global_K"]
    assert len(seas5) == 240 * 6 * 25
    assert seas5["init_date"].min() == pd.Timestamp("1996-01-01")
    assert seas5["init_date"].max() == pd.Timestamp("2015-12-01")
    frame = build_frame_from_inputs(seas5, era5_K, noaa_C)
    assert frame["era5_K"].notna().all() and frame["y_C"].notna().all()
    assert (frame["n_members"] == 25).all() and set(frame["lead_month"]) == set(range(1, 7))
    assert (frame["y_C"] - _truth(frame["target_year_frac"])).std() == pytest.approx(0.07, abs=0.01)
    assert (frame["ens_mean_K"] - frame["era5_K"]).mean() == pytest.approx(0.12, abs=0.01)
    assert frame["ens_sd_K"].mean() == pytest.approx(0.2, abs=0.01)
    cycle = era5_K.to_numpy() - 287.0 - _truth(target_year_frac(era5_K.index))
    assert cycle.groupby(month_of_year(era5_K.index)).std().max() < 1e-9
