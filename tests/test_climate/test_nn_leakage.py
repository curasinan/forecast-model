"""Leakage checker: availability times against decision times, row by row."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from climate.nn.leakage import LeakageError, assert_no_leakage, leakage_violations


def _frame() -> pd.DataFrame:
    times = [datetime(2020, month, 13, 12, tzinfo=timezone.utc) for month in range(1, 6)]
    return pd.DataFrame({"decision_utc": times, "avail_seas5_utc": times})


def test_a_clean_frame_has_no_violations():
    assert leakage_violations(_frame(), "decision_utc", ["avail_seas5_utc"]).empty


def test_a_late_input_is_reported_with_its_reason():
    frame = _frame()
    frame.loc[3, "avail_seas5_utc"] += pd.Timedelta(days=1)
    bad = leakage_violations(frame, "decision_utc", ["avail_seas5_utc"])
    assert list(bad.index) == [3] and bad["reason"].iloc[0] == "avail_seas5_utc"


def test_a_missing_availability_is_a_violation():
    frame = _frame()
    frame.loc[2, "avail_seas5_utc"] = pd.NaT
    bad = leakage_violations(frame, "decision_utc", ["avail_seas5_utc"])
    assert bad["reason"].iloc[0] == "avail_seas5_utc missing"


def test_assert_no_leakage_raises_and_names_the_row():
    frame = _frame()
    frame.loc[3, "avail_seas5_utc"] += pd.Timedelta(hours=1)
    with pytest.raises(LeakageError, match="row 3: avail_seas5_utc"):
        assert_no_leakage(frame, "decision_utc", ["avail_seas5_utc"])


def test_assert_no_leakage_returns_none_for_a_clean_frame():
    assert assert_no_leakage(_frame(), "decision_utc", ["avail_seas5_utc"]) is None


def test_assert_no_leakage_lists_five_rows_at_most_after_the_total():
    frame = pd.concat([_frame(), _frame()], ignore_index=True)
    frame["avail_seas5_utc"] += pd.Timedelta(hours=1)
    with pytest.raises(LeakageError) as caught:
        assert_no_leakage(frame, "decision_utc", ["avail_seas5_utc"])
    total, listing = str(caught.value).split(": ", 1)
    assert total.startswith("10 row")
    assert listing.split("; ") == [f"row {i}: avail_seas5_utc" for i in range(5)]


def test_a_missing_decision_time_is_a_violation():
    frame = _frame()
    frame.loc[1, "decision_utc"] = pd.NaT
    bad = leakage_violations(frame, "decision_utc", ["avail_seas5_utc"])
    assert list(bad.index) == [1] and bad["reason"].iloc[0] == "decision_utc missing"


def test_a_row_names_every_late_input_among_several_columns():
    frame = _frame()
    frame["avail_label_utc"] = frame["decision_utc"]
    frame.loc[1, "avail_label_utc"] += pd.Timedelta(days=1)
    frame.loc[4, "avail_seas5_utc"] += pd.Timedelta(days=1)
    frame.loc[4, "avail_label_utc"] += pd.Timedelta(days=1)
    bad = leakage_violations(frame, "decision_utc", ["avail_seas5_utc", "avail_label_utc"])
    assert list(bad.index) == [1, 4]
    assert list(bad["reason"]) == ["avail_label_utc", "avail_seas5_utc, avail_label_utc"]
    assert list(bad.columns) == [*frame.columns, "reason"]
