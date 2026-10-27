"""Strict expanding windows: the bridge refit per test year and rows admitted by decision time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from climate.bridge import Bridge
from climate.emos import target_year
from climate.nn.windows import FIRST_TEST_YEAR, n_labelled_targets, strict_windows
from climate.storage import LEDGER_COLUMNS, VINTAGE_LIVE


def _first_print_ledger(period: str, seen: datetime) -> pd.DataFrame:
    row = {
        "series": "gtm",
        "period": period,
        "value": 1.0,
        "first_seen_utc": seen.isoformat(),
        "vintage": VINTAGE_LIVE,
        "is_revision": False,
        "source_url": "",
    }
    return pd.DataFrame([row], columns=LEDGER_COLUMNS)


def test_each_window_bridge_ends_in_december_before_the_test_year(synthetic_inputs):
    windows = strict_windows(*synthetic_inputs, first_test_year=2006, last_test_year=2008)
    assert [w.test_year for w in windows] == [2006, 2007, 2008]
    assert all(w.bridge.fit_end == f"{w.test_year - 1}-12" for w in windows)


def test_training_rows_are_labelled_and_before_the_test_year(synthetic_inputs):
    w = strict_windows(*synthetic_inputs, first_test_year=2006, last_test_year=2006)[0]
    assert (target_year(w.train) < 2006).all()
    assert np.isfinite(w.train["y_C"]).all()


def test_rows_decided_before_the_last_training_label_was_published_are_excluded(synthetic_inputs):
    w = strict_windows(*synthetic_inputs, first_test_year=2006, last_test_year=2006)[0]
    assert w.cutoff_utc == datetime(2006, 1, 12, tzinfo=timezone.utc)
    assert (w.test["decision_utc"] >= w.cutoff_utc).all()
    assert w.excluded == 15
    assert (w.test["init_date"] >= "2006-01-01").all()


def test_too_few_training_targets_skips_the_year(synthetic_inputs):
    windows = strict_windows(
        *synthetic_inputs, first_test_year=1997, last_test_year=1997, min_train_targets=24
    )
    assert windows == []


def test_n_labelled_targets_counts_distinct_target_months_with_a_finite_label():
    frame = pd.DataFrame(
        {
            "target_month": ["2000-01", "2000-01", "2000-02", "2000-03", "2000-03", "2000-04"],
            "y_C": [0.1, 0.1, np.nan, 0.3, 0.3, np.inf],
        }
    )
    assert n_labelled_targets(frame) == 2
    assert n_labelled_targets(frame.iloc[0:0]) == 0


def test_each_window_is_built_with_the_bridge_fitted_on_the_series_through_the_year_before(
    synthetic_inputs,
):
    _, era5_K, noaa_C = synthetic_inputs
    windows = strict_windows(*synthetic_inputs, first_test_year=2006, last_test_year=2007)
    for w in windows:
        last_label = f"{w.test_year - 1}-12"
        direct = Bridge().fit(era5_K.loc[:last_label], noaa_C.loc[:last_label])
        assert w.bridge.params == direct.params
        for part in (w.train, w.test):
            expected = direct.predict_mean(
                part["ens_mean_K"].to_numpy(), part["month_of_year"].to_numpy()
            )
            np.testing.assert_allclose(part["fc_mean_C"], expected)
    assert windows[1].bridge.n_obs - windows[0].bridge.n_obs == 12


def test_the_admitted_and_the_excluded_rows_are_all_the_test_years_rows(synthetic_inputs):
    w = strict_windows(*synthetic_inputs, first_test_year=2006, last_test_year=2006)[0]
    assert (target_year(w.test) == 2006).all()
    assert len(w.test) + w.excluded == 72  # 12 target months x 6 leads
    assert list(w.train.index) == list(range(len(w.train)))
    assert list(w.test.index) == list(range(len(w.test)))


def test_a_ledger_first_print_sets_the_cutoff_and_a_row_decided_at_it_is_admitted(
    synthetic_inputs,
):
    january_decision = datetime(2006, 1, 13, 12, tzinfo=timezone.utc)
    at_cutoff = strict_windows(
        *synthetic_inputs,
        first_test_year=2006,
        last_test_year=2006,
        ledger=_first_print_ledger("2005-12", january_decision),
    )[0]
    assert at_cutoff.cutoff_utc == january_decision
    assert at_cutoff.excluded == 15
    assert at_cutoff.test["init_date"].min() == pd.Timestamp("2006-01-01")
    after_cutoff = strict_windows(
        *synthetic_inputs,
        first_test_year=2006,
        last_test_year=2006,
        ledger=_first_print_ledger("2005-12", january_decision + timedelta(seconds=1)),
    )[0]
    assert after_cutoff.excluded == 15 + 6  # the six January inits are now too early
    assert after_cutoff.test["init_date"].min() == pd.Timestamp("2006-02-01")


def test_the_skip_threshold_is_inclusive_and_later_years_are_still_returned(synthetic_inputs):
    windows = strict_windows(*synthetic_inputs, first_test_year=1997, last_test_year=1999)
    assert [w.test_year for w in windows] == [1998, 1999]
    assert [n_labelled_targets(w.train) for w in windows] == [24, 36]
    one_short = strict_windows(
        *synthetic_inputs, first_test_year=1998, last_test_year=1998, min_train_targets=25
    )
    assert one_short == []


def test_an_unlabelled_training_row_is_dropped_but_an_unlabelled_test_row_is_kept(
    synthetic_inputs,
):
    seas5, era5_K, noaa_C = synthetic_inputs
    unlabelled = ["2004-06", *noaa_C.index[noaa_C.index >= "2006-07"]]
    w = strict_windows(
        seas5, era5_K, noaa_C.drop(unlabelled), first_test_year=2006, last_test_year=2006
    )[0]
    assert "2004-06" not in set(w.train["target_month"])
    assert n_labelled_targets(w.train) == 119
    assert len(w.test) == 72 - 15
    assert w.test["y_C"].isna().sum() == 36  # July to December 2006, 6 leads each


def test_the_first_test_year_defaults_to_2001(synthetic_inputs):
    windows = strict_windows(*synthetic_inputs, last_test_year=2002)
    assert FIRST_TEST_YEAR == 2001
    assert [w.test_year for w in windows] == [2001, 2002]


def _years_later(inputs, years: int):
    """The synthetic inputs with every init and every month moved later by whole years."""
    seas5, era5_K, noaa_C = inputs
    moved = seas5.assign(init_date=seas5["init_date"] + pd.DateOffset(years=years))
    months = (pd.PeriodIndex(era5_K.index, freq="M") + 12 * years).astype(str)
    return moved, era5_K.set_axis(months), noaa_C.set_axis(months)


def test_the_late_december_2018_print_keeps_the_january_2019_inits_out(synthetic_inputs):
    (w,) = strict_windows(
        *_years_later(synthetic_inputs, 4), first_test_year=2019, last_test_year=2019
    )
    assert w.cutoff_utc == datetime(2019, 2, 6, 16, tzinfo=timezone.utc)
    assert (w.test["init_date"] != pd.Timestamp("2019-01-01")).all()  # decided 01-13 12:00
    assert (w.test["init_date"] == pd.Timestamp("2019-02-01")).sum() == 6
    assert w.excluded == 15 + 6
