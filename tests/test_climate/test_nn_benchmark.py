"""The seasonal benchmark: strict and frozen protocols, per-lead scores and the win rule."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from climate.dataset import build_frame_from_inputs
from climate.emos import (
    crps_gaussian,
    expanding_window_scores,
    interval_coverage,
    pit,
    pit_histogram,
    summarise_scores,
    target_year,
)
from climate.nn import benchmark, scoring
from climate.nn.availability import noaa_available_on, seas5_available_on
from climate.nn.baselines import emos_baseline, raw_ensemble, trend_climatology
from climate.nn.benchmark import SeasonalRun, run_seasonal, seasonal_win
from climate.nn.features import SEASONAL_FEATURES
from climate.nn.leakage import LeakageError
from climate.nn.seasonal_model import GtmNnModel, usable_rows, usable_target_years
from climate.nn.windows import strict_windows
from climate.storage import LEDGER_COLUMNS, VINTAGE_LIVE
from tests.test_climate import nn_fixtures

BASELINES = ("emos", "raw", "trend")
ROW_COLUMNS = ["test_year", "lead_month", "target_month", "decision_utc", "y_C"]
SUMMARY_COLUMNS = [
    "model",
    "lead_month",
    "n",
    "crps",
    "skill_vs_trend",
    "skill_vs_emos",
    "cover50",
    "cover90",
]


def _strict(models, first, last, seeds=(), hidden=32):
    return SeasonalRun("strict", tuple(models), first, last, tuple(seeds), hidden, "cpu")


@pytest.fixture(scope="module")
def inputs():
    """The synthetic inputs once per module; the tests below only read them."""
    return nn_fixtures.synthetic_inputs()


@pytest.fixture(scope="module")
def frozen_run(inputs):
    """(rows, summary, extras) of the frozen run over 2006-2008."""
    cfg = SeasonalRun("frozen", ("emos",), 2006, 2008, (), 32, "cpu")
    return run_seasonal(inputs, None, cfg)


@pytest.fixture(scope="module")
def strict_run(inputs):
    """(rows, summary, extras) of the strict run of the three baselines over 2006-2008."""
    return run_seasonal(inputs, None, _strict(BASELINES, 2006, 2008))


# frozen protocol


def test_frozen_protocol_reproduces_expanding_window_scores_exactly(synthetic_inputs):
    cfg = SeasonalRun("frozen", ("emos",), 2006, 2008, (), 32, "cpu")
    rows, summary, extras = run_seasonal(synthetic_inputs, None, cfg)
    frame = build_frame_from_inputs(*synthetic_inputs)
    expected = summarise_scores(expanding_window_scores(frame, 2006, 2008, trend=True))
    got = summary[summary.model == "emos"].sort_values("lead_month")["crps"].to_numpy()
    np.testing.assert_allclose(got, expected["crps_emos"].to_numpy(), rtol=0, atol=1e-12)


def test_the_frozen_summary_has_the_reports_numbers_for_each_model_and_lead(frozen_run, inputs):
    rows, summary, extras = frozen_run
    frame = build_frame_from_inputs(*inputs)
    expected = summarise_scores(expanding_window_scores(frame, 2006, 2008, trend=True))
    assert list(summary.columns) == SUMMARY_COLUMNS
    assert len(summary) == 18 and set(summary["model"]) == {"emos", "trend", "clim"}
    for model in ("emos", "trend", "clim"):
        part = summary[summary.model == model].sort_values("lead_month")
        assert part["lead_month"].tolist() == [1, 2, 3, 4, 5, 6]
        assert part["n"].tolist() == expected["n"].tolist()
        np.testing.assert_allclose(part["crps"], expected[f"crps_{model}"], rtol=0, atol=1e-12)
    emos = summary[summary.model == "emos"].sort_values("lead_month")
    for ours, theirs in (
        ("skill_vs_trend", "skill_vs_trend"),
        ("cover50", "cover50_emos"),
        ("cover90", "cover90_emos"),
    ):
        np.testing.assert_allclose(emos[ours], expected[theirs], rtol=0, atol=1e-12)
    trend = summary[summary.model == "trend"].sort_values("lead_month")
    assert (trend["skill_vs_trend"] == 0.0).all()
    ratio = 1.0 - expected["crps_trend"] / expected["crps_emos"]
    np.testing.assert_allclose(trend["skill_vs_emos"], ratio, rtol=0, atol=1e-12)


def test_the_frozen_rows_are_the_per_row_predictions_of_the_three_models(frozen_run):
    rows, summary, extras = frozen_run
    models = ("emos", "trend", "clim")
    prediction_columns = [f"{kind}_{model}" for model in models for kind in ("mu", "sigma")]
    assert list(rows.columns) == [*ROW_COLUMNS, *prediction_columns]
    assert len(rows) == 3 * 72 and sorted(rows["test_year"].unique()) == [2006, 2007, 2008]
    assert extras["protocol"] == "frozen" and extras["excluded_rows"] == 0
    assert extras["saved_model"] is None  # a frozen run carries no network
    assert extras["skipped_windows"] == []
    assert set(extras["pit_hist"]) == set(models)
    assert all(len(values) == 10 for values in extras["pit_hist"].values())
    first = rows.iloc[0]
    init = (pd.Period(first["target_month"], "M") - (int(first["lead_month"]) - 1)).strftime(
        "%Y-%m"
    )
    assert first["decision_utc"] == seas5_available_on(init)


def test_a_frozen_run_scores_emos_trend_and_clim_whatever_models_it_is_given(inputs):
    cfg = SeasonalRun("frozen", ("nn", "raw"), 2006, 2007, (0,), 16, "cpu")
    rows, summary, extras = run_seasonal(inputs, None, cfg)
    assert set(summary["model"]) == {"emos", "trend", "clim"}
    assert "mu_nn" not in rows.columns and "mu_raw" not in rows.columns


def test_the_frozen_rows_reproduce_the_frozen_summary(frozen_run):
    rows, summary, extras = frozen_run
    for model in ("emos", "trend", "clim"):
        for lead in range(1, 7):
            part = rows[rows["lead_month"] == lead]
            crps = crps_gaussian(part["y_C"], part[f"mu_{model}"], part[f"sigma_{model}"])
            cell = summary[(summary.model == model) & (summary.lead_month == lead)].iloc[0]
            assert cell["n"] == len(part)
            assert cell["crps"] == pytest.approx(crps.mean(), rel=0, abs=1e-12)


def test_the_strict_scorer_on_the_frozen_rows_reproduces_the_frozen_summary(frozen_run):
    rows, summary, extras = frozen_run
    again = scoring.summarise(
        rows,
        benchmark.FROZEN_MODELS,
        key="lead_month",
        key_values=benchmark.LEADS,
        references=benchmark.SEASONAL_REFERENCES,
    )
    assert list(again.columns) == list(summary.columns)
    assert again["n"].tolist() == summary["n"].tolist()
    for column in ("crps", "skill_vs_trend", "skill_vs_emos"):
        np.testing.assert_allclose(again[column], summary[column], rtol=0, atol=1e-12)
    for column in ("cover50", "cover90"):
        emos = again["model"] == "emos"  # the frozen summary tabulates coverage for emos only
        np.testing.assert_allclose(again[emos][column], summary[emos][column], rtol=0, atol=1e-12)
        assert summary[~emos][column].isna().all()


def test_a_frozen_year_without_test_rows_is_listed_as_skipped(inputs):
    cfg = SeasonalRun("frozen", ("emos",), 2015, 2017, (), 32, "cpu")
    rows, summary, extras = run_seasonal(inputs, None, cfg)
    assert sorted(rows["test_year"].unique()) == [2015, 2016]
    assert extras["skipped_windows"] == [2017]  # no target month of 2017 is in the data


# strict protocol


def test_strict_protocol_reports_excluded_rows_and_scores_every_lead(synthetic_inputs):
    cfg = SeasonalRun("strict", ("emos", "raw", "trend"), 2006, 2008, (), 32, "cpu")
    rows, summary, extras = run_seasonal(synthetic_inputs, None, cfg)
    assert extras["excluded_rows"] == 45
    assert sorted(summary["lead_month"].unique()) == [1, 2, 3, 4, 5, 6]
    assert set(summary["model"]) == {"emos", "raw", "trend"} and (summary["n"] > 0).all()
    assert {"mu_emos", "sigma_raw", "mu_trend"} <= set(rows.columns)


def test_strict_rows_carry_the_documented_columns_and_respect_each_windows_cutoff(strict_run):
    rows, summary, extras = strict_run
    prediction_columns = [f"{kind}_{model}" for model in BASELINES for kind in ("mu", "sigma")]
    assert list(rows.columns) == [*ROW_COLUMNS, *prediction_columns]
    assert len(rows) == 3 * (72 - 15)
    assert extras["protocol"] == "strict" and extras["skipped_windows"] == []
    assert extras["saved_model"] is None  # no network in the run
    assert str(rows["decision_utc"].dtype).endswith("UTC]")
    for year, part in rows.groupby("test_year"):
        assert (part["decision_utc"] >= noaa_available_on(f"{year - 1}-12")).all()
        assert part["target_month"].str.startswith(str(year)).all()


def test_the_summary_is_the_mean_crps_and_coverage_of_the_scorable_rows(strict_run):
    rows, summary, extras = strict_run
    assert list(summary.columns) == SUMMARY_COLUMNS and len(summary) == 18
    crps = {}
    for model in BASELINES:
        for lead in range(1, 7):
            part = rows[rows["lead_month"] == lead]
            crps[model, lead] = crps_gaussian(
                part["y_C"], part[f"mu_{model}"], part[f"sigma_{model}"]
            )
            cell = summary[(summary.model == model) & (summary.lead_month == lead)].iloc[0]
            cover = interval_coverage(part["y_C"], part[f"mu_{model}"], part[f"sigma_{model}"])
            assert cell["n"] == len(part)
            assert cell["crps"] == pytest.approx(crps[model, lead].mean(), rel=0, abs=1e-12)
            assert cell["cover50"] == cover[0.5] and cell["cover90"] == cover[0.9]
    for (model, lead), values in crps.items():
        cell = summary[(summary.model == model) & (summary.lead_month == lead)].iloc[0]
        for reference, column in (("trend", "skill_vs_trend"), ("emos", "skill_vs_emos")):
            skill = 1.0 - values.mean() / crps[reference, lead].mean()
            assert cell[column] == pytest.approx(skill, rel=0, abs=1e-12)


def test_each_baseline_row_is_the_direct_prediction_on_its_window(inputs):
    rows, summary, extras = run_seasonal(inputs, None, _strict(BASELINES, 2006, 2006))
    window = strict_windows(*inputs, first_test_year=2006, last_test_year=2006)[0]
    direct = {
        "emos": emos_baseline(window.train, window.test, trend=True),
        "raw": raw_ensemble(window.test),
        "trend": trend_climatology(window.train, window.test),
    }
    for name, (mu, sigma) in direct.items():
        np.testing.assert_array_equal(rows[f"mu_{name}"], mu)
        np.testing.assert_array_equal(rows[f"sigma_{name}"], sigma)
    np.testing.assert_array_equal(rows["target_month"], window.test["target_month"])
    np.testing.assert_array_equal(rows["y_C"], window.test["y_C"])


def test_the_pit_histograms_are_the_ten_bin_frequencies_of_each_models_rows(strict_run, frozen_run):
    for rows, summary, extras in (strict_run, frozen_run):
        assert set(extras["pit_hist"]) == set(summary["model"])
        for model, frequencies in extras["pit_hist"].items():
            values = pit(rows["y_C"], rows[f"mu_{model}"], rows[f"sigma_{model}"])
            expected = pit_histogram(values, 10)["frac"].tolist()
            assert len(frequencies) == 10 and sum(frequencies) == pytest.approx(1.0)
            np.testing.assert_allclose(frequencies, expected, rtol=0, atol=1e-12)


def test_each_model_is_scored_on_the_rows_where_it_has_a_finite_prediction(inputs, monkeypatch):
    def raw_without_lead_one(frame):
        mu, sigma = raw_ensemble(frame)
        return mu, np.where(frame["lead_month"].to_numpy() == 1, np.nan, sigma)

    monkeypatch.setattr(benchmark, "raw_ensemble", raw_without_lead_one)
    rows, summary, extras = run_seasonal(inputs, None, _strict(("raw", "trend"), 2006, 2006))
    raw = summary[summary.model == "raw"].set_index("lead_month")
    trend = summary[summary.model == "trend"].set_index("lead_month")
    assert raw.loc[1, "n"] == 0 and np.isnan(raw.loc[1, "crps"])
    assert (raw.loc[2:, "n"] == trend.loc[2:, "n"]).all() and (trend["n"] > 0).all()
    assert rows["sigma_raw"].isna().sum() == (rows["lead_month"] == 1).sum()  # unscored, kept


def test_a_model_without_a_scorable_row_keeps_its_cells_with_n_zero_and_nan_scores(
    inputs, monkeypatch
):
    def emos_without_predictions(train, test, trend=True):
        return np.full(len(test), np.nan), np.full(len(test), np.nan)

    monkeypatch.setattr(benchmark, "emos_baseline", emos_without_predictions)
    rows, summary, extras = run_seasonal(inputs, None, _strict(("emos", "trend"), 2006, 2006))
    emos = summary[summary.model == "emos"]
    assert len(emos) == 6 and (emos["n"] == 0).all()
    assert (
        emos[["crps", "skill_vs_trend", "skill_vs_emos", "cover50", "cover90"]].isna().all().all()
    )
    trend = summary[summary.model == "trend"]
    assert (trend["n"] > 0).all() and trend["skill_vs_emos"].isna().all()
    assert (trend["skill_vs_trend"] == 0.0).all()
    assert len(rows) == 57 and rows["mu_emos"].isna().all()
    assert len(extras["pit_hist"]["emos"]) == 10 and np.isnan(extras["pit_hist"]["emos"]).all()


def test_unlabelled_test_rows_stay_in_rows_but_are_never_scored(inputs):
    seas5, era5_K, noaa_C = inputs
    unlabelled = noaa_C.drop(noaa_C.index[noaa_C.index >= "2008-07"])
    rows, summary, extras = run_seasonal(
        (seas5, era5_K, unlabelled), None, _strict(BASELINES, 2008, 2008)
    )
    assert len(rows) == 57 and rows["y_C"].isna().sum() > 0
    for lead in range(1, 7):
        labelled = np.isfinite(rows.loc[rows["lead_month"] == lead, "y_C"]).sum()
        cells = summary[summary.lead_month == lead]
        assert (cells["n"] == labelled).all() and labelled > 0


def test_a_window_with_no_admitted_test_rows_is_skipped_and_listed(inputs):
    rows, summary, extras = run_seasonal(inputs, None, _strict(BASELINES, 2015, 2017))
    assert sorted(rows["test_year"].unique()) == [2015]
    assert extras["skipped_windows"] == [2016, 2017]
    # the fixture's last init is 2015-12, before the 2016 cutoff, so all 15 of its rows are excluded
    assert extras["excluded_rows"] == 15 + 15


def test_a_window_trained_on_fewer_than_four_target_years_is_skipped_for_every_model(inputs):
    rows, summary, extras = run_seasonal(inputs, None, _strict(BASELINES, 1997, 2000))
    # 1997 has too few labelled targets for strict_windows, 1998 and 1999 train on 2 and 3 years
    assert extras["skipped_windows"] == [1997, 1998, 1999]
    assert sorted(rows["test_year"].unique()) == [2000] and (summary["n"] > 0).all()


def test_only_training_rows_with_a_label_and_every_feature_count_towards_the_four_years(inputs):
    seas5, era5_K, noaa_C = inputs
    no_members = seas5["init_date"].dt.year.isin([1997, 1998, 1999])
    blind = seas5.assign(t2m_global_K=seas5["t2m_global_K"].mask(no_members))
    # 60 labelled target months, but only the target years 1996, 1997 and 2000 have features
    rows, summary, extras = run_seasonal(
        (blind, era5_K, noaa_C), None, _strict(BASELINES, 2001, 2002)
    )
    assert extras["skipped_windows"] == [2001]
    assert sorted(rows["test_year"].unique()) == [2002]


def test_the_usable_rows_have_a_finite_label_and_every_feature_finite():
    frame = pd.DataFrame(
        {
            "target_year_frac": [2001.5, 2001.5, 2002.5, 2002.5, 2003.5, 2004.5, 2004.5],
            "x": [1.0, 2.0, np.nan, 4.0, np.inf, 6.0, 7.0],
            "z": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
            "y_C": [0.1, np.nan, 0.3, 0.4, 0.5, 0.6, 0.7],
        }
    )
    mask = usable_rows(frame, ["x", "z", "target_year_frac"])
    assert mask.tolist() == [True, False, False, True, False, True, True]
    assert usable_target_years(frame, ["x", "z"]).tolist() == [2001, 2002, 2004]
    assert usable_target_years(frame, ["z"]).tolist() == [2001, 2002, 2003, 2004]
    assert usable_target_years(frame.iloc[:0], ["x"]).tolist() == []


def test_the_benchmark_counts_training_years_with_the_rule_the_network_fits_by(inputs):
    train = strict_windows(*inputs, first_test_year=2006, last_test_year=2006)[0].train.copy()
    every_year = benchmark._training_years(train)
    train.loc[target_year(train) <= 2002, "q50_C"] = np.nan  # those years no longer count
    expected = len(usable_target_years(train, SEASONAL_FEATURES))
    assert benchmark._training_years(train) == expected < every_year


def test_a_strict_run_prints_one_line_per_test_year_to_stderr(inputs, capsys):
    windows = strict_windows(*inputs, first_test_year=1997, last_test_year=2000)
    run_seasonal(inputs, None, _strict(BASELINES, 1997, 2000))
    lines = capsys.readouterr().err.splitlines()
    by_year = {window.test_year: window for window in windows}
    # 1997 is dropped by strict_windows, 1998 and 1999 train on too few target years
    assert lines[0] == "test year 1997: n_train=0, n_test=0, skipped"
    for line, year, outcome in zip(lines[1:], (1998, 1999, 2000), ("skipped",) * 2 + ("scored",)):
        window = by_year[year]
        expected = f"test year {year}: n_train={len(window.train)}, n_test={len(window.test)}"
        assert line == f"{expected}, {outcome}"
    assert len(lines) == 4


def test_the_rows_of_a_strict_run_over_one_window_carry_no_frame_attributes(inputs, tmp_path):
    rows, summary, extras = run_seasonal(inputs, None, _strict(BASELINES, 2015, 2016))
    assert rows["test_year"].unique().tolist() == [2015] and rows.attrs == {}
    rows.to_parquet(tmp_path / "rows.parquet")  # the window frame's Bridge cannot be written


def test_a_frozen_run_prints_nothing_to_stderr(inputs, capsys):
    run_seasonal(inputs, None, SeasonalRun("frozen", ("emos",), 2006, 2007, (), 32, "cpu"))
    assert capsys.readouterr().err == ""


def test_a_late_input_raises_a_leakage_error_before_anything_is_fitted(inputs, monkeypatch):
    def must_not_run(train, test, trend=True):
        raise AssertionError("fitted before the leakage check finished")

    monkeypatch.setattr(benchmark, "strict_windows", nn_fixtures.windows_with_a_late_input)
    monkeypatch.setattr(benchmark, "emos_baseline", must_not_run)
    with pytest.raises(LeakageError, match=r"2007.*row 0.*avail_seas5_utc"):
        run_seasonal(inputs, None, _strict(("emos",), 2006, 2007))


def test_a_ledger_first_print_moves_the_cutoff_and_the_excluded_count(inputs):
    first_print = {
        "series": "gtm",
        "period": "2005-12",
        "value": 1.0,
        "first_seen_utc": "2006-02-20T00:00:00+00:00",
        "vintage": VINTAGE_LIVE,
        "is_revision": False,
        "source_url": "",
    }
    ledger = pd.DataFrame([first_print], columns=LEDGER_COLUMNS)
    rows, summary, extras = run_seasonal(inputs, ledger, _strict(BASELINES, 2006, 2006))
    assert extras["excluded_rows"] == 15 + 6 + 6  # the January and February inits are too early
    assert (rows["decision_utc"] >= pd.Timestamp("2006-02-20", tz="UTC")).all()


def test_unknown_protocols_models_and_duplicates_are_refused(inputs):
    with pytest.raises(ValueError, match="protocol"):
        run_seasonal(inputs, None, SeasonalRun("loose", BASELINES, 2006, 2006, (), 32, "cpu"))
    with pytest.raises(ValueError, match="model 'gbm'"):
        run_seasonal(inputs, None, _strict(("emos", "gbm"), 2006, 2006))
    with pytest.raises(ValueError, match="twice"):
        run_seasonal(inputs, None, _strict(("emos", "emos"), 2006, 2006))
    with pytest.raises(ValueError, match="at least one"):
        run_seasonal(inputs, None, _strict((), 2006, 2006))


def test_no_model_directory_is_made_when_the_network_is_not_in_the_run(inputs, tmp_path):
    run_seasonal(inputs, None, _strict(BASELINES, 2006, 2006), model_dir=tmp_path / "m")
    assert not (tmp_path / "m").exists()


# the win rule


def _summary(nn_better_at, cover50, cover90):
    """emos at 0.05 everywhere; nn at 0.04 on the named leads and 0.06 elsewhere."""
    rows = []
    for lead in range(1, 7):
        nn_crps = 0.04 if lead in nn_better_at else 0.06
        for model, crps, c50, c90 in (
            ("emos", 0.05, 0.5, 0.9),
            ("nn", nn_crps, cover50, cover90),
        ):
            rows.append(
                {
                    "model": model,
                    "lead_month": lead,
                    "n": 30,
                    "crps": crps,
                    "skill_vs_trend": 0.2,
                    "skill_vs_emos": 0.0,
                    "cover50": c50,
                    "cover90": c90,
                }
            )
    return pd.DataFrame(rows)


def test_seasonal_win_rule():
    assert seasonal_win(_summary(nn_better_at=[1, 2, 3, 4], cover50=0.5, cover90=0.9)) == (True, "")
    ok, why = seasonal_win(_summary(nn_better_at=[1, 2, 3], cover50=0.5, cover90=0.9))
    assert not ok and "3 of 6" in why
    ok, why = seasonal_win(_summary(nn_better_at=[1, 2, 3, 4], cover50=0.7, cover90=0.9))
    assert not ok and "cover50" in why


def test_the_win_reasons_name_the_count_and_the_first_failing_lead():
    assert seasonal_win(_summary([1, 2, 3], 0.5, 0.9)) == (False, "nn beats emos at 3 of 6 leads")
    assert seasonal_win(_summary([], 0.5, 0.9)) == (False, "nn beats emos at 0 of 6 leads")
    assert seasonal_win(_summary([1, 2, 3, 4], 0.7, 0.9)) == (False, "cover50 at lead 1")
    assert seasonal_win(_summary([1, 2, 3, 4], 0.5, 0.7)) == (False, "cover90 at lead 1")
    assert seasonal_win(_summary(range(1, 7), 0.5, 0.9)) == (True, "")


def test_a_tie_counts_as_beating_emos_and_nan_does_not():
    tied = _summary([1, 2, 3], 0.5, 0.9)
    tied.loc[(tied.model == "nn") & (tied.lead_month == 4), "crps"] = 0.05
    assert seasonal_win(tied) == (True, "")
    unknown = tied.copy()
    unknown.loc[(unknown.model == "nn") & (unknown.lead_month == 4), "crps"] = np.nan
    assert seasonal_win(unknown) == (False, "nn beats emos at 3 of 6 leads")


@pytest.mark.parametrize("cover50", [0.35, 0.65, 0.5])
def test_the_cover50_band_is_closed_at_both_ends(cover50):
    assert seasonal_win(_summary([1, 2, 3, 4], cover50, 0.9)) == (True, "")


@pytest.mark.parametrize("cover90", [0.75, 0.98, 0.9])
def test_the_cover90_band_is_closed_at_both_ends(cover90):
    assert seasonal_win(_summary([1, 2, 3, 4], 0.5, cover90)) == (True, "")


@pytest.mark.parametrize(
    "cover50, cover90, reason",
    [
        (0.349, 0.9, "cover50 at lead 1"),
        (0.651, 0.9, "cover50 at lead 1"),
        (0.5, 0.749, "cover90 at lead 1"),
        (0.5, 0.981, "cover90 at lead 1"),
        (np.nan, 0.9, "cover50 at lead 1"),
        (0.5, np.nan, "cover90 at lead 1"),
    ],
)
def test_a_coverage_outside_its_band_or_missing_fails_the_rule(cover50, cover90, reason):
    assert seasonal_win(_summary([1, 2, 3, 4], cover50, cover90)) == (False, reason)


def test_the_band_is_checked_at_every_lead_and_the_count_comes_first():
    summary = _summary([1, 2, 3, 4], 0.5, 0.9)
    summary.loc[(summary.model == "nn") & (summary.lead_month == 5), "cover50"] = 0.8
    assert seasonal_win(summary) == (False, "cover50 at lead 5")
    summary.loc[(summary.model == "nn") & (summary.lead_month == 3), "cover90"] = 0.5
    assert seasonal_win(summary) == (False, "cover90 at lead 3")
    losing = _summary([1, 2, 3], 0.9, 0.5)
    assert seasonal_win(losing) == (False, "nn beats emos at 3 of 6 leads")


def test_a_summary_without_the_network_or_emos_has_no_win_and_does_not_raise():
    summary = _summary([1, 2, 3, 4], 0.5, 0.9)
    ok, why = seasonal_win(summary[summary.model != "nn"])
    assert not ok and "nn" in why
    ok, why = seasonal_win(summary[summary.model != "emos"])
    assert not ok and "emos" in why


def test_an_unpaired_lead_gives_no_verdict_and_names_the_lead_and_both_counts():
    summary = _summary([1, 2, 3, 4], 0.5, 0.9)
    summary.loc[(summary.model == "nn") & (summary.lead_month == 3), "n"] = 28
    assert seasonal_win(summary) == (None, "unpaired rows at lead 3: nn has 28, emos 30")
    losing = _summary([1], 0.5, 0.9)
    losing.loc[(losing.model == "emos") & (losing.lead_month == 6), "n"] = 31
    assert seasonal_win(losing) == (None, "unpaired rows at lead 6: nn has 30, emos 31")


def test_the_seasonal_verdict_is_the_same_for_frames_and_for_the_records_of_metrics_json():
    cases = [
        (_summary([1, 2, 3, 4], 0.5, 0.9), (True, "")),
        (_summary([1, 2, 3], 0.5, 0.9), (False, "nn beats emos at 3 of 6 leads")),
        (_summary([1, 2, 3, 4], 0.7, 0.9), (False, "cover50 at lead 1")),
    ]
    for summary, expected in cases:
        assert seasonal_win(summary) == expected
        assert seasonal_win(summary.to_dict("records")) == expected
    records = _summary([1, 2, 3, 4], 0.5, 0.9).to_dict("records")
    for record in records:
        if record["model"] == "nn" and record["lead_month"] == 2:
            record["cover90"] = None  # metrics.json writes NaN as null
    assert seasonal_win(records) == (False, "cover90 at lead 2")
    assert seasonal_win([]) == (False, "the summary needs both nn and emos rows")


# the network


def test_strict_protocol_with_the_network(synthetic_inputs):
    pytest.importorskip("torch")
    cfg = SeasonalRun("strict", ("nn", "emos"), 2010, 2012, (0,), 16, "cpu")
    rows, summary, extras = run_seasonal(synthetic_inputs, None, cfg)
    assert {"mu_nn", "sigma_nn", "mu_emos"} <= set(rows.columns)
    assert np.isfinite(rows["mu_nn"]).all()
    assert set(summary[summary.model == "nn"]["lead_month"]) == {1, 2, 3, 4, 5, 6}


def test_the_last_windows_network_is_saved_and_its_rows_are_its_predictions(inputs, tmp_path):
    pytest.importorskip("torch")
    rows, summary, extras = run_seasonal(
        inputs,
        None,
        _strict(("nn", "emos"), 2010, 2012, seeds=(0,), hidden=16),
        model_dir=tmp_path / "m",
    )
    saved = GtmNnModel.load(tmp_path / "m")
    assert saved.seeds == (0,) and saved.hidden == 16
    assert saved.validation_years == [2009, 2010, 2011]  # trained on the target years before 2012
    window = strict_windows(*inputs, first_test_year=2012, last_test_year=2012)[0]
    assert extras["saved_model"] == {
        "kind": "seasonal",
        "protocol": "strict",
        "test_year": 2012,
        "labels_through": "2011-12",
        "bridge": window.bridge.params,
    }
    in_2012 = rows[rows["test_year"] == 2012]
    expected = saved.predict(window.test)
    np.testing.assert_allclose(in_2012["mu_nn"], expected["mu"], rtol=1e-5, atol=1e-6)
    np.testing.assert_allclose(in_2012["sigma_nn"], expected["sigma"], rtol=1e-5, atol=1e-6)


def test_the_network_is_built_with_the_seeds_width_and_device_of_the_run(inputs, monkeypatch):
    pytest.importorskip("torch")
    built = []

    class Recorder(GtmNnModel):
        def __init__(self, seeds, hidden, device):
            built.append((seeds, hidden, device))
            super().__init__(seeds, hidden, "cpu")  # the recorded device is a fake, fit on the CPU

    monkeypatch.setattr(benchmark, "GtmNnModel", Recorder)
    run_seasonal(inputs, None, SeasonalRun("strict", ("nn",), 2012, 2012, (3,), 8, "cuda:fake"))
    assert built == [((3,), 8, "cuda:fake")]


def test_a_short_training_window_is_skipped_for_the_network_and_the_baselines_alike(inputs):
    pytest.importorskip("torch")
    cfg = _strict(("nn", "emos"), 1999, 2000, seeds=(0,), hidden=16)
    rows, summary, extras = run_seasonal(inputs, None, cfg)
    assert extras["skipped_windows"] == [1999]  # three training target years: the net cannot fit
    assert sorted(rows["test_year"].unique()) == [2000] and np.isfinite(rows["mu_nn"]).all()
    assert (summary["n"] > 0).all()
