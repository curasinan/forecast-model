"""Scoring shared by the GTM benchmarks: row CRPS, skill, PIT histograms and the summary by key."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from climate.emos import crps_gaussian, crps_skill_score, interval_coverage, pit, pit_histogram
from climate.nn import scoring

REFERENCES = {"skill_vs_ref": "ref", "skill_vs_other": "other"}
KEYS = ("a", "+1", "c")


def _rows() -> pd.DataFrame:
    """Rows of the models m, ref and other over the keys a and +1 (none for c)."""
    rng = np.random.default_rng(3)
    n = 40
    y = rng.normal(0.5, 0.2, n)
    frame = pd.DataFrame({"k": np.where(np.arange(n) % 2 == 0, "a", "+1"), "y_C": y})
    for name, bias, spread in (("m", 0.0, 0.2), ("ref", 0.1, 0.25), ("other", -0.2, 0.15)):
        frame[f"mu_{name}"] = y + rng.normal(bias, 0.1, n)
        frame[f"sigma_{name}"] = spread
    return frame


def _summary(rows, models=("m", "ref", "other")) -> pd.DataFrame:
    return scoring.summarise(rows, models, key="k", key_values=KEYS, references=REFERENCES)


def test_row_crps_is_the_gaussian_crps_where_the_row_is_scorable_and_nan_elsewhere():
    part = pd.DataFrame(
        {
            "y_C": [0.5, np.nan, 0.5, 0.5, 0.5],
            "mu_m": [0.4, 0.4, np.nan, 0.4, 0.4],
            "sigma_m": [0.2, 0.2, 0.2, np.inf, 0.3],
        }
    )
    got = scoring.row_crps(part, "m")
    assert np.isnan(got[[1, 2, 3]]).all()
    np.testing.assert_allclose(got[[0, 4]], crps_gaussian([0.5, 0.5], [0.4, 0.4], [0.2, 0.3]))


def test_skill_is_nan_without_a_reference_and_paired_with_one():
    own = np.array([0.1, 0.2, np.nan, 0.3])
    reference = np.array([0.2, 0.2, 0.2, np.nan])
    assert np.isnan(scoring.skill(own, None))
    assert scoring.skill(own, reference) == crps_skill_score(own, reference)
    assert scoring.skill(own, reference) == pytest.approx(1.0 - 0.15 / 0.2)


def test_the_summary_has_the_key_the_references_and_one_row_per_model_and_key_value():
    summary = _summary(_rows())
    assert list(summary.columns) == [
        "model",
        "k",
        "n",
        "crps",
        "skill_vs_ref",
        "skill_vs_other",
        "cover50",
        "cover90",
    ]
    assert list(scoring.summary_columns("k", REFERENCES)) == list(summary.columns)
    assert summary["model"].tolist() == ["m"] * 3 + ["ref"] * 3 + ["other"] * 3
    assert summary["k"].tolist() == list(KEYS) * 3


def test_a_cell_is_the_mean_crps_the_paired_skills_and_the_coverage_of_its_rows():
    rows = _rows()
    summary = _summary(rows)
    part = rows[rows["k"] == "+1"]
    own = crps_gaussian(part["y_C"], part["mu_m"], part["sigma_m"])
    ref = crps_gaussian(part["y_C"], part["mu_ref"], part["sigma_ref"])
    other = crps_gaussian(part["y_C"], part["mu_other"], part["sigma_other"])
    cell = summary[(summary.model == "m") & (summary.k == "+1")].iloc[0]
    cover = interval_coverage(part["y_C"], part["mu_m"], part["sigma_m"])
    assert cell["n"] == len(part) == 20
    assert cell["crps"] == pytest.approx(own.mean(), rel=0, abs=1e-15)
    assert cell["skill_vs_ref"] == pytest.approx(crps_skill_score(own, ref), rel=0, abs=1e-15)
    assert cell["skill_vs_other"] == pytest.approx(crps_skill_score(own, other), rel=0, abs=1e-15)
    assert (cell["cover50"], cell["cover90"]) == (cover[0.5], cover[0.9])
    own_reference = summary[(summary.model == "ref") & (summary.k == "+1")].iloc[0]
    assert own_reference["skill_vs_ref"] == 0.0


def test_a_key_value_without_rows_keeps_its_cell_with_n_zero_and_nan_scores():
    summary = _summary(_rows())
    empty = summary[summary.k == "c"]
    assert len(empty) == 3 and (empty["n"] == 0).all()
    assert empty[["crps", "skill_vs_ref", "cover50", "cover90"]].isna().all().all()


def test_a_cell_counts_only_the_rows_that_have_a_label_a_mean_and_a_sigma():
    rows = _rows()
    rows.loc[rows.index[:4], "y_C"] = np.nan
    rows.loc[rows.index[4:8], "mu_m"] = np.nan
    rows.loc[rows.index[8:12], "sigma_m"] = np.nan
    counts = _summary(rows).query("k == 'a'").set_index("model")["n"]
    in_a = (rows["k"] == "a").to_numpy()
    assert counts["m"] == in_a[12:].sum() and counts["ref"] == in_a[4:].sum()
    assert counts["other"] == in_a[4:].sum()


def test_a_missing_reference_model_gives_nan_skills_and_the_other_reference_still_works():
    summary = _summary(_rows(), models=("m", "other"))
    assert summary["skill_vs_ref"].isna().all()
    assert summary[(summary.model == "m") & (summary.k == "a")]["skill_vs_other"].notna().all()


def test_integer_keys_work_like_text_keys():
    rows = _rows().assign(k=lambda frame: (frame["k"] == "a").astype(int))
    summary = scoring.summarise(rows, ("m",), key="k", key_values=(0, 1, 2), references={})
    assert list(summary.columns) == ["model", "k", "n", "crps", "cover50", "cover90"]
    assert summary["n"].tolist() == [20, 20, 0]


def test_the_pit_histograms_have_ten_bins_per_model_and_nan_where_nothing_is_scorable():
    rows = _rows()
    rows["mu_other"] = np.nan
    histograms = scoring.pit_histograms(rows, ("m", "other"))
    expected = pit_histogram(pit(rows["y_C"], rows["mu_m"], rows["sigma_m"]), 10)["frac"].tolist()
    assert scoring.PIT_BINS == 10 and set(histograms) == {"m", "other"}
    np.testing.assert_allclose(histograms["m"], expected, rtol=0, atol=1e-15)
    assert len(histograms["other"]) == 10 and np.isnan(histograms["other"]).all()
