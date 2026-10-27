"""The nowcast benchmark: strict windows, the checkpoint table, first prints and the win rule."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from climate.bridge import Bridge
from climate.emos import crps_gaussian, crps_skill_score, interval_coverage, pit, pit_histogram
from climate.nn import benchmark
from climate.nn.availability import genuine_first_print_months
from climate.nn.baselines import bridge_nowcast, persistence
from climate.nn.benchmark import NowcastRun, nowcast_win, run_nowcast
from climate.nn.features import seasonal_frame
from climate.nn.leakage import LeakageError
from climate.nn.nowcast import CHECKPOINTS, WIN_CHECKPOINTS, nowcast_frame
from climate.nn.nowcast_windows import WindowError, monthly_means, nowcast_windows
from climate.storage import LEDGER_COLUMNS, VINTAGE_LIVE
from tests.test_climate import nn_fixtures

BASELINES = ("bridge", "persistence")
ROW_COLUMNS = ["test_year", "target_month", "checkpoint", "decision_utc", "y_C"]
SUMMARY_COLUMNS = ["model", "checkpoint", "n", "crps", "skill_vs_bridge", "cover50", "cover90"]


def _cfg(models, first, last, seeds=(), hidden=32):
    return NowcastRun(tuple(models), first, last, tuple(seeds), hidden, "cpu")


def _windows(inputs, first, last, **kwargs):
    daily_K, noaa_C, seas5, ledger = inputs
    return nowcast_windows(
        daily_K,
        noaa_C,
        seas5,
        ledger,
        first_test_year=first,
        last_test_year=last,
        **kwargs,
    )


@pytest.fixture(scope="module")
def inputs():
    """The synthetic inputs once per module; the tests below only read them."""
    return nn_fixtures.synthetic_nowcast()


@pytest.fixture(scope="module")
def baseline_run(inputs):
    """(rows, summary, extras) of bridge and persistence over the test years 2010 and 2011."""
    return run_nowcast(*inputs, _cfg(BASELINES, 2010, 2011))


# the monthly means of the daily series


def test_a_month_mean_is_the_mean_of_its_days_and_the_index_is_year_month_text(inputs):
    daily_K = inputs[0]
    monthly = monthly_means(daily_K)
    assert monthly.index[0] == "2000-01" and monthly.index[-1] == "2011-12" and len(monthly) == 144
    assert monthly["2008-02"] == pytest.approx(daily_K["2008-02"].mean(), rel=0, abs=1e-12)
    assert monthly["2009-04"] == pytest.approx(daily_K["2009-04"].mean(), rel=0, abs=1e-12)
    assert monthly.notna().all()


def test_a_month_missing_a_day_a_value_or_the_end_of_the_series_has_no_mean(inputs):
    daily_K = inputs[0]
    cut = daily_K.drop(pd.Timestamp("2009-05-04"))
    cut["2009-06-10"] = np.nan
    cut["2009-07-20"] = np.inf
    cut = cut[cut.index <= "2011-10-30"]
    monthly = monthly_means(cut)
    assert monthly[["2009-05", "2009-06", "2009-07", "2011-10"]].isna().all()
    assert monthly["2009-08"] == pytest.approx(daily_K["2009-08"].mean(), rel=0, abs=1e-12)
    assert "2011-11" not in monthly.index


def test_a_leap_february_needs_its_twenty_ninth_day():
    days = pd.date_range("2008-02-01", "2008-02-28")
    daily = pd.Series(np.arange(len(days), dtype=float), index=days)
    assert np.isnan(monthly_means(daily)["2008-02"])
    daily["2008-02-29"] = 28.0
    assert monthly_means(daily)["2008-02"] == pytest.approx(14.0)


# the windows


def test_a_window_trains_on_the_labelled_gapless_rows_before_its_year_and_tests_on_its_year(inputs):
    windows = _windows(inputs, 2009, 2010)
    assert [w.test_year for w in windows] == [2009, 2010]
    for window in windows:
        year = window.test_year
        assert window.train["target_month"].str.slice(0, 4).astype(int).max() == year - 1
        assert (window.train["target_month"] < f"{year}-01").all()
        assert np.isfinite(window.train["y_C"]).all() and (window.train["gap_days"] == 0).all()
        assert len(window.train) == (year - 2000) * 12 * 9
        assert (window.test["target_month"].str.slice(0, 4) == str(year)).all()
        assert len(window.test) == 12 * 9 - 2 and window.dropped_gap_rows == 0
        assert window.excluded == 2  # January's checkpoints 5 and 10 come before the 12th
        assert window.cutoff_utc == datetime(year, 1, 12, tzinfo=timezone.utc)
        assert window.train.index.tolist() == list(range(len(window.train)))
        assert window.test.index.tolist() == list(range(len(window.test)))


def test_the_bridge_of_a_window_is_fitted_on_the_labels_through_the_year_before(inputs):
    daily_K, noaa_C, _, _ = inputs
    for window in _windows(inputs, 2009, 2010):
        expected = Bridge().fit(monthly_means(daily_K), noaa_C, end=f"{window.test_year - 1}-12")
        assert window.bridge.fit_end == f"{window.test_year - 1}-12"
        assert window.bridge.params == expected.params


def test_the_frame_of_a_window_is_the_seasonal_and_nowcast_frames_built_with_its_bridge(inputs):
    daily_K, noaa_C, seas5, ledger = inputs
    (window,) = _windows(inputs, 2010, 2010)
    monthly = monthly_means(daily_K)
    seasonal = seasonal_frame(seas5, monthly, noaa_C, bridge=window.bridge)
    frame = nowcast_frame(daily_K, noaa_C, seasonal, bridge=window.bridge, ledger=ledger)
    in_year = frame[frame["target_month"].str.startswith("2010")]
    decided = in_year[in_year["decision_utc"] >= pd.Timestamp("2010-01-12", tz="UTC")]
    pd.testing.assert_frame_equal(window.test, decided.reset_index(drop=True))


def _in_2009_as_admitted(train):
    """The 2009 rows of a training set without the two January rows 2009's own window excludes."""
    in_2009 = train[train["target_month"].str.startswith("2009")]
    january = (in_2009["target_month"] == "2009-01") & in_2009["checkpoint"].isin(["5", "10"])
    return in_2009[~january].reset_index(drop=True)


def test_every_window_rebuilds_the_bridge_dependent_columns_with_its_own_bridge(inputs):
    early, late = _windows(inputs, 2009, 2010)
    in_2009 = _in_2009_as_admitted(late.train)
    assert len(in_2009) == len(early.test) and in_2009["target_month"].equals(
        early.test["target_month"]
    )
    np.testing.assert_array_equal(in_2009["mtd_mean_K"], early.test["mtd_mean_K"])
    for column in ("mtd_mean_C", "seas_mu", "seas_sigma"):
        assert not np.allclose(in_2009[column], early.test[column], equal_nan=True)
    assert early.bridge.params != late.bridge.params


def test_a_label_of_the_test_year_or_later_does_not_reach_the_bridge_or_the_training_rows(inputs):
    daily_K, noaa_C, seas5, ledger = inputs
    bumped = noaa_C.copy()
    bumped[bumped.index >= "2010-01"] += 5.0
    (clean,) = _windows(inputs, 2010, 2010)
    (changed,) = _windows((daily_K, bumped, seas5, ledger), 2010, 2010)
    assert clean.bridge.params == changed.bridge.params
    pd.testing.assert_frame_equal(clean.train, changed.train)
    for column in ("mtd_mean_C", "seas_mu", "seas_sigma"):
        np.testing.assert_array_equal(clean.test[column], changed.test[column])
    assert not np.allclose(clean.test["y_C"], changed.test["y_C"], equal_nan=True)


def test_a_gap_day_drops_the_rows_that_see_it_from_the_training_set_and_counts_the_test_ones(
    inputs,
):
    daily_K, noaa_C, seas5, ledger = inputs
    gappy = daily_K.drop([pd.Timestamp("2008-05-04"), pd.Timestamp("2010-05-04")])
    (window,) = _windows((gappy, noaa_C, seas5, ledger), 2010, 2010)
    assert window.dropped_gap_rows == 8
    may_2010 = window.test[window.test["target_month"] == "2010-05"]
    assert may_2010["checkpoint"].tolist() == ["5"] and len(window.test) == 12 * 9 - 8 - 2
    may_2008 = window.train[window.train["target_month"] == "2008-05"]
    assert may_2008["checkpoint"].tolist() == ["5"] and (window.train["gap_days"] == 0).all()
    assert window.train.index.tolist() == list(range(len(window.train)))  # rows were dropped


def test_the_monthly_means_the_bridge_is_fitted_on_leave_out_the_months_with_a_gap(inputs):
    daily_K, noaa_C, seas5, ledger = inputs
    gappy = daily_K.drop(pd.Timestamp("2008-05-04"))
    (window,) = _windows((gappy, noaa_C, seas5, ledger), 2010, 2010)
    expected = Bridge().fit(monthly_means(gappy), noaa_C, end="2009-12")
    assert window.bridge.params == expected.params
    assert window.bridge.n_obs == 119  # 2000-01 to 2009-12 without 2008-05


def test_a_test_year_beyond_the_data_has_an_empty_test_set_and_the_labelled_months_as_training(
    inputs,
):
    (window,) = _windows(inputs, 2012, 2012)
    assert len(window.test) == 0 and window.dropped_gap_rows == 0 and window.excluded == 0
    assert window.train["target_month"].max() == "2011-11" and len(window.train) == 143 * 9


def test_a_test_year_without_enough_overlap_for_the_bridge_names_the_year(inputs):
    with pytest.raises(WindowError, match="test year 2000"):
        _windows(inputs, 2000, 2000)
    assert issubclass(WindowError, ValueError)


def test_nowcast_windows_are_compared_by_identity_instead_of_by_their_frames(inputs):
    (first,) = _windows(inputs, 2010, 2010)
    (again,) = _windows(inputs, 2010, 2010)
    assert first == first and first != again
    assert first in [first] and first not in [again]


def test_a_given_bridge_month_fits_one_bridge_for_every_window(inputs):
    early, late = _windows(inputs, 2009, 2010, bridge_through="2011-11")
    assert early.bridge.fit_end == late.bridge.fit_end == "2011-11"
    assert early.bridge.params == late.bridge.params
    pd.testing.assert_frame_equal(_in_2009_as_admitted(late.train), early.test)


@pytest.mark.parametrize("month", ["2011", "2011-13", "2011-1", "11-2011", ""])
def test_a_bridge_month_that_is_not_year_month_is_refused(inputs, month):
    with pytest.raises(ValueError, match="YYYY-MM"):
        _windows(inputs, 2010, 2010, bridge_through=month)


# the run


def test_rows_with_a_gap_are_dropped_from_training_and_scoring(synthetic_nowcast):
    daily, noaa_C, seasonal, ledger = synthetic_nowcast
    daily = daily.drop(pd.Timestamp("2009-05-04"))
    cfg = NowcastRun(("bridge",), 2009, 2009, (), 32, "cpu")
    rows, summary, extras = run_nowcast(daily, noaa_C, seasonal, ledger, cfg)
    assert extras["dropped_gap_rows"] == 8  # every 2009-05 checkpoint that sees May 4; "5" stays
    assert set(rows[rows.target_month == "2009-05"]["checkpoint"]) == {"5"}


def test_the_dropped_gap_rows_are_counted_over_every_window(inputs):
    daily_K, noaa_C, seas5, ledger = inputs
    gappy = daily_K.drop([pd.Timestamp("2009-05-04"), pd.Timestamp("2010-07-04")])
    cfg = _cfg(BASELINES, 2009, 2010)
    rows, summary, extras = run_nowcast(gappy, noaa_C, seas5, ledger, cfg)
    assert extras["dropped_gap_rows"] == 16 and extras["excluded_rows"] == 4
    assert len(rows) == 2 * (12 * 9 - 2) - 16


def test_unlabelled_rows_are_predicted_but_not_scored(synthetic_nowcast):
    pytest.importorskip("torch")
    cfg = NowcastRun(("nn", "bridge"), 2010, 2011, (0,), 16, "cpu")
    rows, summary, extras = run_nowcast(*synthetic_nowcast, cfg)
    running = rows[rows.target_month == "2011-12"].set_index("checkpoint")  # left unlabelled
    assert len(running) == 9 and running["y_C"].isna().all()
    finite = np.isfinite(running["mu_nn"])
    assert finite.drop("5").all() and not finite["5"]  # no ERA5 day is visible at "5"
    nn = summary[summary.model == "nn"].set_index("checkpoint")
    assert nn.at["10", "n"] == 21  # 24 months minus the unlabelled one and two Januaries
    assert nn.at["5", "n"] == 0 and np.isnan(nn.at["5", "crps"])


def test_bridge_and_persistence_are_scored_per_checkpoint(synthetic_nowcast):
    cfg = NowcastRun(("bridge", "persistence"), 2010, 2011, (), 32, "cpu")
    rows, summary, extras = run_nowcast(*synthetic_nowcast, cfg)
    assert len(summary) == 18 and set(summary["checkpoint"]) == set(CHECKPOINTS)
    full = summary[(summary.model == "bridge") & (summary.checkpoint == "+6")]["crps"].iloc[0]
    early = summary[(summary.model == "bridge") & (summary.checkpoint == "10")]["crps"].iloc[0]
    assert full < early


def test_first_print_summary_is_restricted_to_genuine_months(synthetic_nowcast):
    daily, noaa_C, seasonal, ledger = synthetic_nowcast  # the ledger has two live first prints
    cfg = NowcastRun(("bridge",), 2011, 2011, (), 32, "cpu")
    rows, summary, extras = run_nowcast(daily, noaa_C, seasonal, ledger, cfg)
    assert extras["first_print_summary"]["n"].max() == 2


def test_the_rows_carry_the_documented_columns_in_the_order_of_the_models(baseline_run):
    rows, summary, extras = baseline_run
    prediction_columns = [f"{kind}_{model}" for model in BASELINES for kind in ("mu", "sigma")]
    assert list(rows.columns) == [*ROW_COLUMNS, *prediction_columns]
    assert len(rows) == 2 * (12 * 9 - 2) and sorted(rows["test_year"].unique()) == [2010, 2011]
    assert str(rows["decision_utc"].dtype).endswith("UTC]")
    assert rows["target_month"].str.slice(0, 4).astype(int).eq(rows["test_year"]).all()
    assert rows["y_C"].isna().sum() == 9  # 2011-12 stays in the rows, unlabelled


def test_each_baseline_row_is_the_direct_prediction_on_its_window(inputs, baseline_run):
    rows = baseline_run[0]
    for window in _windows(inputs, 2010, 2011):
        part = rows[rows["test_year"] == window.test_year]
        direct = {
            "bridge": bridge_nowcast(window.train, window.test, bridge=window.bridge),
            "persistence": persistence(window.train, window.test),
        }
        for name, (mu, sigma) in direct.items():
            np.testing.assert_array_equal(part[f"mu_{name}"], mu)
            np.testing.assert_array_equal(part[f"sigma_{name}"], sigma)
        np.testing.assert_array_equal(part["target_month"], window.test["target_month"])
        np.testing.assert_array_equal(part["checkpoint"], window.test["checkpoint"])


def test_the_summary_is_the_mean_crps_the_paired_skill_and_the_coverage_of_the_scorable_rows(
    baseline_run,
):
    rows, summary, extras = baseline_run
    assert list(summary.columns) == SUMMARY_COLUMNS and len(summary) == 2 * len(CHECKPOINTS)
    assert summary["model"].tolist() == ["bridge"] * 9 + ["persistence"] * 9
    assert summary["checkpoint"].tolist() == list(CHECKPOINTS) * 2
    for model in BASELINES:
        for checkpoint in CHECKPOINTS:
            part = rows[rows["checkpoint"] == checkpoint]
            crps = crps_gaussian(part["y_C"], part[f"mu_{model}"], part[f"sigma_{model}"])
            reference = crps_gaussian(part["y_C"], part["mu_bridge"], part["sigma_bridge"])
            cell = summary[(summary.model == model) & (summary.checkpoint == checkpoint)].iloc[0]
            cover = interval_coverage(part["y_C"], part[f"mu_{model}"], part[f"sigma_{model}"])
            scored = np.isfinite(crps)
            assert cell["n"] == scored.sum()
            if scored.any():
                assert cell["crps"] == pytest.approx(crps[scored].mean(), rel=0, abs=1e-12)
                skill = crps_skill_score(crps, reference)
                assert cell["skill_vs_bridge"] == pytest.approx(
                    skill, rel=0, abs=1e-12, nan_ok=True
                )
                assert (cell["cover50"], cell["cover90"]) == (cover[0.5], cover[0.9])


def test_a_checkpoint_with_no_prediction_keeps_its_cell_with_n_zero_and_nan_scores(baseline_run):
    rows, summary, extras = baseline_run
    cell = summary[(summary.model == "bridge") & (summary.checkpoint == "5")].iloc[0]
    assert cell["n"] == 0 and cell[["crps", "skill_vs_bridge", "cover50", "cover90"]].isna().all()
    assert rows[rows["checkpoint"] == "5"]["mu_bridge"].isna().all()  # kept in the rows, unscored
    assert (summary[(summary.model == "bridge") & (summary.checkpoint != "5")]["n"] > 0).all()


def test_each_model_is_scored_on_the_rows_where_it_has_a_finite_prediction(inputs, monkeypatch):
    def persistence_without_a_sigma_in_may(train, test):
        mu, sigma = persistence(train, test)
        return mu, np.where(test["month_of_year"].to_numpy() == 5, np.nan, sigma)

    monkeypatch.setattr(benchmark, "persistence", persistence_without_a_sigma_in_may)
    rows, summary, extras = run_nowcast(*inputs, _cfg(BASELINES, 2010, 2010))
    persisted = summary[summary.model == "persistence"].set_index("checkpoint")
    bridged = summary[summary.model == "bridge"].set_index("checkpoint")
    assert persisted.at["+3", "n"] == 11 and bridged.at["+3", "n"] == 12
    assert rows["sigma_persistence"].isna().sum() == 9  # the nine May rows are kept, unscored


def test_the_first_print_summary_has_the_summary_shape_and_scores_the_genuine_months_only(inputs):
    rows, summary, extras = run_nowcast(*inputs, _cfg(BASELINES, 2011, 2011))
    first_print = extras["first_print_summary"]
    assert list(first_print.columns) == SUMMARY_COLUMNS and len(first_print) == 18
    assert first_print["model"].tolist() == summary["model"].tolist()
    assert first_print["checkpoint"].tolist() == summary["checkpoint"].tolist()
    months = sorted(genuine_first_print_months(inputs[3]))
    assert months == ["2011-09", "2011-10"]
    part = rows[(rows["checkpoint"] == "+3") & rows["target_month"].isin(months)]
    crps = crps_gaussian(part["y_C"], part["mu_bridge"], part["sigma_bridge"])
    cell = first_print[(first_print.model == "bridge") & (first_print.checkpoint == "+3")].iloc[0]
    assert cell["n"] == 2 and cell["crps"] == pytest.approx(crps.mean(), rel=0, abs=1e-12)
    assert (first_print["n"] <= 2).all()
    other = summary[(summary.model == "bridge") & (summary.checkpoint == "+3")].iloc[0]
    assert other["n"] == 11  # the current-vintage table keeps every labelled month


def test_without_a_ledger_the_first_print_summary_keeps_its_cells_with_n_zero(inputs):
    daily_K, noaa_C, seas5, _ = inputs
    rows, summary, extras = run_nowcast(daily_K, noaa_C, seas5, None, _cfg(BASELINES, 2011, 2011))
    first_print = extras["first_print_summary"]
    assert len(first_print) == 18 and (first_print["n"] == 0).all()
    assert (summary["n"].sum()) > 0


def test_the_pit_histograms_are_the_ten_bin_frequencies_of_each_models_rows(baseline_run):
    rows, summary, extras = baseline_run
    assert set(extras["pit_hist"]) == set(BASELINES)
    for model, frequencies in extras["pit_hist"].items():
        values = pit(rows["y_C"], rows[f"mu_{model}"], rows[f"sigma_{model}"])
        expected = pit_histogram(values, 10)["frac"].tolist()
        assert len(frequencies) == 10 and sum(frequencies) == pytest.approx(1.0)
        np.testing.assert_allclose(frequencies, expected, rtol=0, atol=1e-12)


def test_the_extras_name_the_protocol_the_kind_and_what_was_dropped_or_skipped(baseline_run):
    rows, summary, extras = baseline_run
    assert extras["protocol"] == "strict" and extras["kind"] == "nowcast"
    assert extras["saved_model"] is None  # no network in the run
    assert extras["bridge_through"] is None and extras["dropped_gap_rows"] == 0
    assert extras["excluded_rows"] == 4 and extras["skipped_windows"] == []
    assert set(extras) >= {"first_print_summary", "pit_hist"}


def test_the_january_rows_decided_before_the_december_print_are_excluded_and_counted(inputs):
    rows, summary, extras = run_nowcast(*inputs, _cfg(BASELINES, 2010, 2010))
    january = rows[rows["target_month"] == "2010-01"]
    assert january["checkpoint"].tolist() == list(CHECKPOINTS[2:])  # "5" and "10" are absent
    february = rows[rows["target_month"] == "2010-02"]
    assert february["checkpoint"].tolist() == list(CHECKPOINTS)
    assert extras["excluded_rows"] == 2 and len(rows) == 12 * 9 - 2
    assert (rows["decision_utc"] >= pd.Timestamp("2010-01-12", tz="UTC")).all()


def test_a_row_is_either_excluded_or_dropped_for_a_gap_never_counted_twice(inputs):
    daily_K, noaa_C, seas5, ledger = inputs
    gappy = daily_K.drop(pd.Timestamp("2010-01-03"))  # every January checkpoint from "10" sees it
    rows, summary, extras = run_nowcast(gappy, noaa_C, seas5, ledger, _cfg(BASELINES, 2010, 2010))
    assert extras["excluded_rows"] == 2  # "5" and "10", decided before the 12th
    assert extras["dropped_gap_rows"] == 7  # "15" to "+10"; "10" is already excluded
    assert rows[rows["target_month"] == "2010-01"].empty and len(rows) == 12 * 9 - 2 - 7


def test_the_excluded_januaries_leave_the_checkpoint_counts_of_the_summary(baseline_run):
    rows, summary, extras = baseline_run
    bridge = summary[summary.model == "bridge"].set_index("checkpoint")
    # 24 months, minus the two Januaries at "10", minus the unlabelled 2011-12
    assert bridge.at["10", "n"] == 21 and bridge.at["15", "n"] == 23


def _with_a_december_first_print(ledger, seen):
    first_print = {
        "series": "gtm",
        "period": "2009-12",
        "value": 0.5,
        "first_seen_utc": seen,
        "vintage": VINTAGE_LIVE,
        "is_revision": False,
        "source_url": "",
    }
    return pd.concat(
        [ledger, pd.DataFrame([first_print], columns=LEDGER_COLUMNS)], ignore_index=True
    )


@pytest.mark.parametrize(
    "seen, first_admitted, excluded",
    [
        ("2010-01-08T00:00:00+00:00", "10", 1),
        ("2010-01-10T00:00:00+00:00", "10", 1),
        ("2010-01-16T00:00:00+00:00", "20", 3),
    ],
    ids=["early", "at-checkpoint-10", "late"],
)
def test_a_genuine_first_print_of_december_moves_the_cutoff(inputs, seen, first_admitted, excluded):
    daily_K, noaa_C, seas5, ledger = inputs
    moved = _with_a_december_first_print(ledger, seen)
    rows, summary, extras = run_nowcast(daily_K, noaa_C, seas5, moved, _cfg(BASELINES, 2010, 2010))
    january = rows[rows["target_month"] == "2010-01"]["checkpoint"].tolist()
    assert january == list(CHECKPOINTS[CHECKPOINTS.index(first_admitted) :])
    assert extras["excluded_rows"] == excluded
    (window,) = _windows((daily_K, noaa_C, seas5, moved), 2010, 2010)
    assert window.cutoff_utc == pd.Timestamp(seen).to_pydatetime()


def test_a_frozen_run_keeps_the_admission_rule(inputs):
    cfg = _cfg(("bridge",), 2010, 2010)
    rows, summary, extras = run_nowcast(*inputs, cfg, bridge_through="2011-11")
    assert extras["protocol"] == "frozen" and extras["excluded_rows"] == 2
    assert rows[rows["target_month"] == "2010-01"]["checkpoint"].tolist() == list(CHECKPOINTS[2:])


def test_a_given_bridge_month_makes_it_a_frozen_run_that_says_so(inputs):
    rows, summary, extras = run_nowcast(
        *inputs, _cfg(("bridge",), 2010, 2010), bridge_through="2011-11"
    )
    assert extras["protocol"] == "frozen" and extras["bridge_through"] == "2011-11"
    (window,) = _windows(inputs, 2010, 2010, bridge_through="2011-11")
    mu, sigma = bridge_nowcast(window.train, window.test, bridge=window.bridge)
    np.testing.assert_array_equal(rows["mu_bridge"], mu)
    np.testing.assert_array_equal(rows["sigma_bridge"], sigma)


def test_a_window_trained_on_fewer_than_four_target_years_is_skipped_for_every_model(
    inputs, capsys
):
    rows, summary, extras = run_nowcast(*inputs, _cfg(BASELINES, 2002, 2004))
    assert extras["skipped_windows"] == [2002, 2003]  # 2 and 3 usable training years
    assert extras["excluded_rows"] == 6  # two January rows in each window, skipped ones too
    assert sorted(rows["test_year"].unique()) == [2004]
    bridge = summary[summary.model == "bridge"].set_index("checkpoint")
    assert bridge.at["+3", "n"] == 12
    lines = capsys.readouterr().err.splitlines()
    assert [line.rsplit(", ", 1)[1] for line in lines] == ["skipped", "skipped", "scored"]


def test_a_test_year_without_test_rows_is_skipped_and_listed(inputs):
    rows, summary, extras = run_nowcast(*inputs, _cfg(BASELINES, 2011, 2012))
    assert extras["skipped_windows"] == [2012] and sorted(rows["test_year"].unique()) == [2011]


def test_a_run_with_nothing_to_score_has_empty_rows_and_n_zero_cells(inputs):
    rows, summary, extras = run_nowcast(*inputs, _cfg(BASELINES, 2012, 2012))
    assert len(rows) == 0 and list(rows.columns)[:5] == ROW_COLUMNS
    assert len(summary) == 18 and (summary["n"] == 0).all()
    assert extras["skipped_windows"] == [2012] and np.isnan(extras["pit_hist"]["bridge"]).all()


def test_the_run_prints_one_line_per_test_year_to_stderr(inputs, capsys):
    windows = _windows(inputs, 2009, 2010)
    run_nowcast(*inputs, _cfg(BASELINES, 2009, 2010))
    lines = capsys.readouterr().err.splitlines()
    assert lines == [
        f"test year {w.test_year}: n_train={len(w.train)}, n_test={len(w.test)}, scored"
        for w in windows
    ]


@pytest.mark.parametrize("column", ["avail_era5_utc", "avail_seas_utc", "avail_prev_label_utc"])
def test_a_late_input_raises_a_leakage_error_before_anything_is_fitted(inputs, monkeypatch, column):
    real = benchmark.nowcast_windows

    def with_a_late_input(*args, **kwargs):
        windows = real(*args, **kwargs)
        test = windows[-1].test
        test.loc[0, column] = test.loc[0, "decision_utc"] + pd.Timedelta(days=1)
        return windows

    def must_not_run(train, test, *, bridge):
        raise AssertionError("fitted before the leakage check finished")

    monkeypatch.setattr(benchmark, "nowcast_windows", with_a_late_input)
    monkeypatch.setattr(benchmark, "bridge_nowcast", must_not_run)
    with pytest.raises(LeakageError, match=rf"2011.*row 0.*{column}"):
        run_nowcast(*inputs, _cfg(("bridge",), 2010, 2011))


def test_unknown_models_duplicates_and_an_empty_list_are_refused(inputs):
    with pytest.raises(ValueError, match="model 'emos'"):
        run_nowcast(*inputs, _cfg(("bridge", "emos"), 2010, 2010))
    with pytest.raises(ValueError, match="twice"):
        run_nowcast(*inputs, _cfg(("bridge", "bridge"), 2010, 2010))
    with pytest.raises(ValueError, match="at least one"):
        run_nowcast(*inputs, _cfg((), 2010, 2010))


def test_no_model_directory_is_made_when_the_network_is_not_in_the_run(inputs, tmp_path):
    run_nowcast(*inputs, _cfg(BASELINES, 2010, 2010), model_dir=tmp_path / "m")
    assert not (tmp_path / "m").exists()


# the network


def test_the_last_windows_network_is_saved_and_its_rows_are_its_predictions(inputs, tmp_path):
    pytest.importorskip("torch")
    from climate.nn.nowcast_model import GtmNowcastModel

    cfg = _cfg(("nn", "bridge"), 2010, 2011, seeds=(0,), hidden=16)
    rows, summary, extras = run_nowcast(*inputs, cfg, model_dir=tmp_path / "m")
    saved = GtmNowcastModel.load(tmp_path / "m")
    assert saved.seeds == (0,) and saved.hidden == 16
    assert saved.validation_years == [2008, 2009, 2010]  # trained on the target years before 2011
    (window,) = _windows(inputs, 2011, 2011)
    in_2011 = rows[rows["test_year"] == 2011]
    expected = saved.predict(window.test)
    np.testing.assert_allclose(
        in_2011["mu_nn"], expected["mu"], rtol=1e-5, atol=1e-6, equal_nan=True
    )
    np.testing.assert_allclose(
        in_2011["sigma_nn"], expected["sigma"], rtol=1e-5, atol=1e-6, equal_nan=True
    )


def test_the_saved_network_is_the_last_scored_windows_when_a_later_year_is_skipped(
    inputs, tmp_path
):
    pytest.importorskip("torch")
    from climate.nn.nowcast_model import GtmNowcastModel

    cfg = _cfg(("nn",), 2010, 2012, seeds=(0,), hidden=8)
    rows, summary, extras = run_nowcast(*inputs, cfg, model_dir=tmp_path / "m")
    assert extras["skipped_windows"] == [2012]  # the data end in 2011
    assert GtmNowcastModel.load(tmp_path / "m").validation_years == [2008, 2009, 2010]
    saved = extras["saved_model"]
    assert (saved["kind"], saved["test_year"], saved["labels_through"]) == (
        "nowcast",
        2011,
        "2010-12",
    )
    (window,) = _windows(inputs, 2011, 2011)
    assert saved["bridge"] == window.bridge.params and set(saved) == {
        "kind",
        "test_year",
        "labels_through",
        "bridge",
    }


def test_the_saved_model_of_a_frozen_run_names_the_bridge_it_was_built_with(inputs):
    pytest.importorskip("torch")
    cfg = _cfg(("nn",), 2010, 2011, seeds=(0,), hidden=8)
    rows, summary, extras = run_nowcast(*inputs, cfg, bridge_through="2011-11")
    saved = extras["saved_model"]
    assert saved["test_year"] == 2011 and saved["labels_through"] == "2010-12"
    assert saved["bridge"]["fit_end"] == "2011-11"  # the network's labels end earlier


def test_the_network_is_built_with_the_seeds_width_and_device_of_the_run(inputs, monkeypatch):
    pytest.importorskip("torch")
    from climate.nn.nowcast_model import GtmNowcastModel

    built = []

    class Recorder(GtmNowcastModel):
        def __init__(self, seeds, hidden, device):
            built.append((seeds, hidden, device))
            super().__init__(seeds, hidden, "cpu")  # the recorded device is a fake, fit on the CPU

    monkeypatch.setattr(benchmark, "GtmNowcastModel", Recorder)
    cfg = NowcastRun(("nn",), 2011, 2011, (3,), 8, "cuda:fake")
    run_nowcast(*inputs, cfg)
    assert built == [((3,), 8, "cuda:fake")]


def test_the_network_rows_are_the_predictions_of_a_model_fitted_on_the_window(inputs):
    pytest.importorskip("torch")
    from climate.nn.nowcast_model import GtmNowcastModel

    rows, summary, extras = run_nowcast(*inputs, _cfg(("nn",), 2011, 2011, seeds=(0, 1), hidden=8))
    (window,) = _windows(inputs, 2011, 2011)
    direct = GtmNowcastModel((0, 1), 8, "cpu").fit(window.train).predict(window.test)
    np.testing.assert_allclose(rows["mu_nn"], direct["mu"], rtol=1e-5, atol=1e-6, equal_nan=True)


def test_a_short_training_window_is_skipped_for_the_network_and_the_baselines_alike(inputs):
    pytest.importorskip("torch")
    cfg = _cfg(("nn", "bridge"), 2003, 2004, seeds=(0,), hidden=8)
    rows, summary, extras = run_nowcast(*inputs, cfg)
    assert extras["skipped_windows"] == [2003]  # three usable training years: the net cannot fit
    assert sorted(rows["test_year"].unique()) == [2004]
    nn = rows[rows["checkpoint"] != "5"]["mu_nn"]
    assert np.isfinite(nn).all()


# the win rule


def _summary(nn_beats_bridge_at, n=30):
    """bridge at 0.05 everywhere; nn at 0.04 on the named checkpoints and 0.06 elsewhere."""
    rows = []
    for checkpoint in CHECKPOINTS:
        nn_crps = 0.04 if checkpoint in nn_beats_bridge_at else 0.06
        for model, crps in (("bridge", 0.05), ("nn", nn_crps)):
            rows.append(
                {
                    "model": model,
                    "checkpoint": checkpoint,
                    "n": n,
                    "crps": crps,
                    "skill_vs_bridge": 0.0,
                    "cover50": 0.5,
                    "cover90": 0.9,
                }
            )
    return pd.DataFrame(rows)


def _empty_summary():
    return pd.DataFrame(columns=SUMMARY_COLUMNS)


def test_nowcast_win_rule():
    assert nowcast_win(_summary(nn_beats_bridge_at=WIN_CHECKPOINTS), _empty_summary()) == (True, "")
    ok, why = nowcast_win(_summary(nn_beats_bridge_at=("10", "15")), _empty_summary())
    assert not ok and "20" in why
    ok, why = nowcast_win(
        _summary(nn_beats_bridge_at=WIN_CHECKPOINTS), _summary(nn_beats_bridge_at=(), n=3)
    )
    assert not ok and "first print" in why
    ok = nowcast_win(
        _summary(nn_beats_bridge_at=WIN_CHECKPOINTS), _summary(nn_beats_bridge_at=(), n=2)
    )
    assert ok == (True, "")


def test_the_reasons_name_the_first_failing_checkpoint():
    assert nowcast_win(_summary(["10", "15"]), _empty_summary()) == (
        False,
        "nn does not beat bridge at 20",
    )
    assert nowcast_win(_summary([]), _empty_summary()) == (False, "nn does not beat bridge at 10")
    only_late = _summary(["25", "+1", "+3"])
    assert nowcast_win(only_late, _empty_summary()) == (False, "nn does not beat bridge at 10")
    assert nowcast_win(_summary(["10", "15", "20", "25", "+1"]), _empty_summary()) == (
        False,
        "nn does not beat bridge at +3",
    )


def test_only_the_win_checkpoints_count():
    assert WIN_CHECKPOINTS == ("10", "15", "20", "25", "+1", "+3")
    summary = _summary(WIN_CHECKPOINTS)  # the network loses at 5, +6 and +10
    assert nowcast_win(summary, _empty_summary()) == (True, "")


def test_a_tie_does_not_beat_the_bridge_and_nan_never_does():
    tied = _summary(WIN_CHECKPOINTS)
    tied.loc[(tied.model == "nn") & (tied.checkpoint == "20"), "crps"] = 0.05
    assert nowcast_win(tied, _empty_summary()) == (False, "nn does not beat bridge at 20")
    unknown = _summary(WIN_CHECKPOINTS)
    unknown.loc[(unknown.model == "nn") & (unknown.checkpoint == "+1"), "crps"] = np.nan
    assert nowcast_win(unknown, _empty_summary()) == (False, "nn does not beat bridge at +1")
    no_bridge_score = _summary(WIN_CHECKPOINTS)
    no_bridge_score.loc[
        (no_bridge_score.model == "bridge") & (no_bridge_score.checkpoint == "10"), "crps"
    ] = np.nan
    assert nowcast_win(no_bridge_score, _empty_summary()) == (
        False,
        "nn does not beat bridge at 10",
    )


def test_a_summary_without_the_network_or_the_bridge_has_no_win_and_does_not_raise():
    summary = _summary(WIN_CHECKPOINTS)
    reason = "the summary needs both nn and bridge rows"
    assert nowcast_win(summary[summary.model != "nn"], _empty_summary()) == (False, reason)
    assert nowcast_win(summary[summary.model != "bridge"], _empty_summary()) == (False, reason)


def test_the_verdict_is_the_same_for_frames_and_for_the_records_of_metrics_json():
    current = _summary(WIN_CHECKPOINTS)
    cases = [
        (_summary(WIN_CHECKPOINTS, n=5), (True, "")),
        (_summary([], n=5), (False, "first print table disagrees at 10")),
        (_empty_summary(), (True, "")),
    ]
    for first, expected in cases:
        assert nowcast_win(current, first) == expected
        assert nowcast_win(current.to_dict("records"), first.to_dict("records")) == expected
        assert nowcast_win(current, first.to_dict("records")) == expected
        assert nowcast_win(current.to_dict("records"), first) == expected


def test_a_json_null_in_the_records_is_a_missing_score_and_nothing_is_a_missing_summary():
    records = _summary(WIN_CHECKPOINTS).to_dict("records")
    for record in records:
        if record["model"] == "nn" and record["checkpoint"] == "+1":
            record["crps"] = None  # metrics.json writes NaN as null
    assert nowcast_win(records, []) == (False, "nn does not beat bridge at +1")
    unscored = _summary(WIN_CHECKPOINTS).to_dict("records")
    for record in unscored:
        if record["model"] == "nn":
            record["crps"], record["n"] = None, 0  # a network without a single score
    assert nowcast_win(unscored, []) == (False, "nn does not beat bridge at 10")
    for record in unscored:
        record["crps"] = None  # not a single score in the whole table
    assert nowcast_win(unscored, []) == (False, "nn does not beat bridge at 10")
    assert nowcast_win([], []) == (False, "the summary needs both nn and bridge rows")


def test_the_first_print_table_must_agree_wherever_it_has_three_rows_at_a_checkpoint():
    current = _summary(WIN_CHECKPOINTS)
    first = _summary(WIN_CHECKPOINTS, n=3)
    assert nowcast_win(current, first) == (True, "")
    first.loc[(first.model == "nn") & (first.checkpoint == "25"), "crps"] = 0.07
    assert nowcast_win(current, first) == (False, "first print table disagrees at 25")
    first.loc[first.checkpoint == "25", "n"] = 2  # too few rows at that checkpoint to disagree
    assert nowcast_win(current, first) == (True, "")


def test_a_tie_in_the_first_print_table_is_a_disagreement_too():
    first = _summary(WIN_CHECKPOINTS, n=3)
    first.loc[(first.model == "nn") & (first.checkpoint == "+3"), "crps"] = 0.05
    assert nowcast_win(_summary(WIN_CHECKPOINTS), first) == (
        False,
        "first print table disagrees at +3",
    )


def test_the_first_print_table_names_its_first_disagreeing_checkpoint_in_order():
    first = _summary([], n=5)
    assert nowcast_win(_summary(WIN_CHECKPOINTS), first) == (
        False,
        "first print table disagrees at 10",
    )
    first = _summary(["10", "15", "20", "25"], n=5)
    assert nowcast_win(_summary(WIN_CHECKPOINTS), first) == (
        False,
        "first print table disagrees at +1",
    )


def test_the_first_print_table_needs_three_rows_of_both_models_and_ignores_other_checkpoints():
    current = _summary(WIN_CHECKPOINTS)
    first = _summary([], n=5)
    first.loc[(first.model == "bridge"), "n"] = 2  # the bridge has two rows: no comparison
    assert nowcast_win(current, first) == (True, "")
    outside = _summary(WIN_CHECKPOINTS, n=5)
    outside.loc[outside.checkpoint.isin(["5", "+6", "+10"]), "crps"] = 1.0  # nn worse there
    assert nowcast_win(current, outside) == (True, "")


def test_a_first_print_summary_with_nan_scores_is_ignored():
    first = _summary([], n=0)
    first[["crps", "skill_vs_bridge", "cover50", "cover90"]] = np.nan
    assert nowcast_win(_summary(WIN_CHECKPOINTS), first) == (True, "")


def test_the_current_vintage_table_is_judged_before_the_first_print_table():
    first = _summary([], n=5)
    assert nowcast_win(_summary(["10"]), first) == (False, "nn does not beat bridge at 15")
