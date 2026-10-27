"""Nowcast frame: checkpoints with the ERA5 lag, month-to-date means and availability columns."""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from climate.bridge import Bridge
from climate.nn.availability import seas5_available_on
from climate.nn.baselines import bridge_nowcast, fit_remainder, persistence
from climate.nn.daily import DAILY_PARQUET
from climate.nn.features import seasonal_frame
from climate.nn.leakage import leakage_violations
from climate.nn.nowcast import (
    CHECKPOINTS,
    NOWCAST_COLUMNS,
    NOWCAST_FEATURES,
    WIN_CHECKPOINTS,
    checkpoint_decision,
    freshest_seasonal,
    load_daily,
    month_to_date,
    nowcast_frame,
)
from climate.storage import LEDGER_COLUMNS, VINTAGE_LIVE
from tests.test_climate.nn_fixtures import nowcast_rows

UTC = timezone.utc
LEAKAGE_INPUTS = ["avail_era5_utc", "avail_seas_utc", "avail_prev_label_utc"]
EXPECTED_COLUMNS = [
    "target_month",
    "checkpoint",
    "decision_utc",
    "month_of_year",
    "moy_sin",
    "moy_cos",
    "target_year_frac",
    "days_in_month",
    "days_used",
    "gap_days",
    "mtd_mean_K",
    "mtd_mean_C",
    "full_month_mean_K",
    "frac_days_used",
    "prev_label_C",
    "prev_label_month",
    "avail_prev_label_utc",
    "seas_mu",
    "seas_sigma",
    "seas_init",
    "avail_seas_utc",
    "avail_era5_utc",
    "y_C",
    "avail_label_utc",
]
TIME_COLUMNS = [
    "decision_utc",
    "avail_prev_label_utc",
    "avail_seas_utc",
    "avail_era5_utc",
    "avail_label_utc",
]
INT_COLUMNS = ["month_of_year", "days_in_month", "days_used", "gap_days"]
TEXT_COLUMNS = ["target_month", "checkpoint", "prev_label_month", "seas_init"]
FLOAT_COLUMNS = [c for c in EXPECTED_COLUMNS if c not in TIME_COLUMNS + INT_COLUMNS + TEXT_COLUMNS]
# days of 2026-02 each checkpoint sees (the ERA5 lag hides the last five days before it)
FEB_DAYS_USED = {
    "5": 0,
    "10": 5,
    "15": 10,
    "20": 15,
    "25": 20,
    "+1": 24,
    "+3": 26,
    "+6": 28,
    "+10": 28,
}


def _daily(start: str, days: int, value=lambda d: 280.0 + d) -> pd.Series:
    """Daily K from start; day 1 gets value(1)."""
    index = pd.date_range(start, periods=days, freq="D", name="date")
    values = [value(day) for day in range(1, days + 1)]
    return pd.Series(values, index=index, name="t2m_global_K", dtype=float)


def _labels(upto: str = "2026-02", first: str = "2025-01") -> pd.Series:
    """Monthly C labels from first through upto; every month has its own value."""
    months = pd.period_range(first, upto, freq="M").astype(str)
    return pd.Series(0.5 + 0.01 * np.arange(len(months)), index=months, name="y_C")


def _seasonal(inits=("2026-01",), target: str = "2026-02") -> pd.DataFrame:
    """One seasonal row per init for one target month; mu and sd differ by init."""
    return pd.DataFrame(
        {
            "target_month": target,
            "init_month": list(inits),
            "decision_utc": pd.to_datetime([seas5_available_on(i) for i in inits], utc=True),
            "fc_mean_C": [1.0 + 0.1 * k for k in range(len(inits))],
            "fc_sd_C": [0.2 + 0.01 * k for k in range(len(inits))],
        }
    )


def _seasonal_all(months: list[str]) -> pd.DataFrame:
    """Every lead 1..6 init of every target month."""
    parts = []
    for target in months:
        inits = [str(pd.Period(target, freq="M") - lead) for lead in range(6)]
        parts.append(_seasonal(inits=inits, target=target))
    return pd.concat(parts, ignore_index=True)


def _bridge() -> Bridge:
    return Bridge(alpha={m: -287.0 for m in range(1, 13)}, beta=1.0, resid_sd=0.03)


def _ledger(live: dict[str, str]) -> pd.DataFrame:
    """GTM ledger rows that are genuine first prints: period -> first_seen_utc."""
    rows = [
        {
            "series": "gtm",
            "period": period,
            "value": 1.0,
            "first_seen_utc": seen,
            "vintage": VINTAGE_LIVE,
            "is_revision": False,
            "source_url": "",
        }
        for period, seen in live.items()
    ]
    return pd.DataFrame(rows, columns=LEDGER_COLUMNS)


def _feb_frame(daily=None, labels=None, seasonal=None, **kwargs) -> pd.DataFrame:
    """The frame of 2026-02 from full daily data, labels through the month and one January init."""
    return nowcast_frame(
        _daily("2026-02-01", 28) if daily is None else daily,
        _labels() if labels is None else labels,
        _seasonal() if seasonal is None else seasonal,
        bridge=_bridge(),
        **({"months": ["2026-02"]} | kwargs),
    )


def _row(frame: pd.DataFrame, checkpoint: str, month: str = "2026-02") -> pd.Series:
    return frame[(frame["target_month"] == month) & (frame["checkpoint"] == checkpoint)].iloc[0]


def _write_daily(root, dates: list[str], values: list[float]) -> None:
    path = root / DAILY_PARQUET[0] / DAILY_PARQUET[1]
    path.parent.mkdir(parents=True)
    stored = pd.DataFrame(
        {"date": pd.to_datetime(dates).astype("datetime64[ns]"), "t2m_global_K": values}
    )
    stored.to_parquet(path, engine="pyarrow", index=False)


# the checkpoints


def test_checkpoint_decisions_inside_and_after_the_month():
    assert checkpoint_decision("2026-02", "5") == datetime(2026, 2, 5, tzinfo=timezone.utc)
    assert checkpoint_decision("2026-02", "+1") == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert checkpoint_decision("2026-02", "+10") == datetime(2026, 3, 10, tzinfo=timezone.utc)


def test_the_checkpoint_labels_and_the_feature_list_are_the_agreed_ones():
    assert CHECKPOINTS == ("5", "10", "15", "20", "25", "+1", "+3", "+6", "+10")
    assert WIN_CHECKPOINTS == ("10", "15", "20", "25", "+1", "+3")
    assert NOWCAST_FEATURES == [
        "mtd_mean_C",
        "frac_days_used",
        "prev_label_C",
        "seas_mu",
        "seas_sigma",
        "moy_sin",
        "moy_cos",
        "target_year_frac",
    ]


def test_decisions_in_a_leap_february_and_across_the_new_year():
    assert checkpoint_decision("2028-02", "25") == datetime(2028, 2, 25, tzinfo=UTC)
    assert checkpoint_decision("2028-02", "+1") == datetime(2028, 3, 1, tzinfo=UTC)
    assert checkpoint_decision("2028-02", "+10") == datetime(2028, 3, 10, tzinfo=UTC)
    assert checkpoint_decision("2027-02", "+1") == datetime(2027, 3, 1, tzinfo=UTC)
    assert checkpoint_decision("2025-12", "25") == datetime(2025, 12, 25, tzinfo=UTC)
    assert checkpoint_decision("2025-12", "+1") == datetime(2026, 1, 1, tzinfo=UTC)
    assert checkpoint_decision("2025-12", "+10") == datetime(2026, 1, 10, tzinfo=UTC)


def test_every_decision_is_utc_midnight_and_the_checkpoints_run_in_order():
    for month in pd.period_range("2027-01", "2028-12", freq="M").astype(str):
        year, number = int(month[:4]), int(month[5:])
        stamps = [checkpoint_decision(month, name) for name in CHECKPOINTS]
        assert all(s.utcoffset() == timedelta(0) and s.time() == time(0) for s in stamps)
        assert stamps == sorted(set(stamps))
        assert [s.day for s in stamps[:5]] == [5, 10, 15, 20, 25]
        assert {(s.year, s.month) for s in stamps[:5]} == {(year, number)}
        last_day = datetime(year, number, calendar.monthrange(year, number)[1], tzinfo=UTC)
        assert [s - last_day for s in stamps[5:]] == [timedelta(days=n) for n in (1, 3, 6, 10)]


@pytest.mark.parametrize(
    "checkpoint", ["", "0", "+0", "05", "+05", "x", "+", "++1", "-5", "5.0", " 5", "5 ", "+1+1"]
)
def test_a_malformed_checkpoint_is_refused(checkpoint):
    with pytest.raises(ValueError, match="checkpoint"):
        checkpoint_decision("2026-02", checkpoint)


@pytest.mark.parametrize("checkpoint", ["29", "30", "32"])
def test_a_checkpoint_day_that_the_month_does_not_have_is_refused(checkpoint):
    with pytest.raises(ValueError):
        checkpoint_decision("2026-02", checkpoint)
    assert checkpoint_decision("2028-02", "29") == datetime(2028, 2, 29, tzinfo=UTC)


@pytest.mark.parametrize(
    "month",
    ["", "2026", "2026-2", "2026-13", "2026-00", "26-02", "2026-02-01", "2026/02", " 2026-02"],
)
def test_a_malformed_month_is_refused(month):
    with pytest.raises(ValueError, match="YYYY-MM"):
        checkpoint_decision(month, "5")


# the month-to-date mean


def test_month_to_date_uses_only_days_through_the_last_day():
    # day 1 -> 281, day 2 -> 282 ...
    daily = _daily("2026-02-01", days=28, value=lambda d: 280.0 + d)
    mtd, used, gap = month_to_date(daily, "2026-02", last_day=date(2026, 2, 10))
    assert used == 10 and gap == 0 and mtd == pytest.approx(285.5)


@pytest.mark.parametrize("last_day", [date(2026, 1, 31), date(2026, 1, 15), date(2025, 6, 1)])
def test_month_to_date_before_the_first_day_has_nothing_and_no_gap(last_day):
    mtd, used, gap = month_to_date(_daily("2026-02-01", 28), "2026-02", last_day)
    assert math.isnan(mtd) and used == 0 and gap == 0


def test_month_to_date_of_the_first_day_alone():
    assert month_to_date(_daily("2026-02-01", 28), "2026-02", date(2026, 2, 1)) == (281.0, 1, 0)


def test_month_to_date_stops_at_the_month_end_and_ignores_the_neighbouring_months():
    daily = _daily("2026-01-15", 60)  # 2026-01-15 .. 2026-03-15; 2026-02-01 is day 18
    mtd, used, gap = month_to_date(daily, "2026-02", last_day=date(2026, 3, 10))
    assert used == 28 and gap == 0 and mtd == pytest.approx(311.5)  # mean of 298 .. 325


def test_month_to_date_counts_the_days_after_the_end_of_the_series_as_gaps():
    daily = _daily("2026-02-01", 20)
    mtd, used, gap = month_to_date(daily, "2026-02", last_day=date(2026, 2, 28))
    assert used == 20 and gap == 8 and mtd == pytest.approx(290.5)


def test_month_to_date_counts_an_absent_day_and_a_nan_as_gaps():
    daily = _daily("2026-02-01", 28).drop(pd.Timestamp("2026-02-04"))
    daily[pd.Timestamp("2026-02-09")] = np.nan
    mtd, used, gap = month_to_date(daily, "2026-02", date(2026, 2, 10))
    assert used == 8 and gap == 2 and mtd == pytest.approx(285.25)  # 281..290 without 284 and 289


def test_month_to_date_of_a_month_the_series_never_reaches():
    mtd, used, gap = month_to_date(_daily("2026-02-01", 28), "2026-04", date(2026, 4, 10))
    assert math.isnan(mtd) and used == 0 and gap == 10


def test_month_to_date_does_not_depend_on_the_order_of_the_series():
    daily = _daily("2026-01-15", 60)
    assert month_to_date(daily.iloc[::-1], "2026-02", date(2026, 2, 10)) == month_to_date(
        daily, "2026-02", date(2026, 2, 10)
    )


def test_month_to_date_never_looks_past_its_last_day():
    daily = _daily("2026-02-01", 28)
    expected = month_to_date(daily, "2026-02", date(2026, 2, 10))
    assert (
        month_to_date(daily[daily.index <= "2026-02-10"], "2026-02", date(2026, 2, 10)) == expected
    )
    poisoned = daily.copy()
    poisoned[poisoned.index > "2026-02-10"] = 1.0e9
    assert month_to_date(poisoned, "2026-02", date(2026, 2, 10)) == expected


def test_month_to_date_in_a_leap_february_reaches_the_29th():
    daily = _daily("2028-02-01", 29)
    mtd, used, gap = month_to_date(daily, "2028-02", date(2028, 3, 20))
    assert used == 29 and gap == 0 and mtd == pytest.approx(295.0)


def test_a_daily_series_not_indexed_by_naive_unique_dates_is_refused():
    monthly = pd.Series([287.0, 287.5], index=["2026-01", "2026-02"])
    aware = _daily("2026-02-01", 28)
    aware.index = aware.index.tz_localize("UTC")
    repeated = pd.concat([_daily("2026-02-01", 5), _daily("2026-02-03", 2)])
    for bad in (monthly, aware, repeated):
        with pytest.raises(ValueError, match="daily_K"):
            month_to_date(bad, "2026-02", date(2026, 2, 5))
        with pytest.raises(ValueError, match="daily_K"):
            nowcast_frame(bad, _labels(), _seasonal(), bridge=_bridge(), months=["2026-02"])


# the freshest seasonal row


def test_freshest_seasonal_takes_the_latest_row_public_by_the_decision():
    seasonal = _seasonal(inits=["2026-01", "2026-02"], target="2026-02")
    mu, sd, init = freshest_seasonal(seasonal, "2026-02", datetime(2026, 2, 10, tzinfo=UTC))
    assert (mu, sd, init) == (pytest.approx(1.0), pytest.approx(0.2), "2026-01")
    mu, sd, init = freshest_seasonal(seasonal, "2026-02", datetime(2026, 2, 15, tzinfo=UTC))
    assert (mu, sd, init) == (pytest.approx(1.1), pytest.approx(0.21), "2026-02")


def test_freshest_seasonal_admits_a_row_public_exactly_at_the_decision():
    seasonal = _seasonal(inits=["2026-01", "2026-02"], target="2026-02")
    release = seas5_available_on("2026-02")
    assert freshest_seasonal(seasonal, "2026-02", release)[2] == "2026-02"
    assert freshest_seasonal(seasonal, "2026-02", release - timedelta(seconds=1))[2] == "2026-01"


def test_freshest_seasonal_is_nan_and_blank_without_a_public_row_for_the_target():
    seasonal = _seasonal(inits=["2026-01"], target="2026-02")
    before_release = seas5_available_on("2026-01") - timedelta(minutes=1)
    for found in (
        freshest_seasonal(seasonal, "2026-02", before_release),
        freshest_seasonal(seasonal, "2026-03", datetime(2027, 1, 1, tzinfo=UTC)),
        freshest_seasonal(seasonal.iloc[:0], "2026-02", datetime(2027, 1, 1, tzinfo=UTC)),
    ):
        assert math.isnan(found[0]) and math.isnan(found[1]) and found[2] == ""


def test_freshest_seasonal_does_not_depend_on_row_order_or_index_labels():
    seasonal = _seasonal(inits=["2026-01", "2026-02"], target="2026-02")
    decision = datetime(2026, 2, 20, tzinfo=UTC)
    expected = freshest_seasonal(seasonal, "2026-02", decision)
    assert freshest_seasonal(seasonal.iloc[::-1], "2026-02", decision) == expected
    repeated = seasonal.set_axis([7, 7])
    assert freshest_seasonal(repeated, "2026-02", decision) == expected


def test_a_seasonal_frame_with_naive_decision_times_is_refused_not_compared():
    seasonal = _seasonal(inits=["2026-01"], target="2026-02")
    seasonal["decision_utc"] = seasonal["decision_utc"].dt.tz_localize(None)
    with pytest.raises(TypeError):
        freshest_seasonal(seasonal, "2026-02", datetime(2026, 2, 10, tzinfo=UTC))
    with pytest.raises(TypeError):
        _feb_frame(seasonal=seasonal)


def test_a_seasonal_row_without_a_decision_time_is_never_chosen():
    seasonal = _seasonal(inits=["2026-01", "2026-02"], target="2026-02")
    seasonal.loc[1, "decision_utc"] = pd.NaT
    assert freshest_seasonal(seasonal, "2026-02", datetime(2026, 3, 1, tzinfo=UTC))[2] == "2026-01"


def test_freshest_seasonal_returns_plain_numbers_and_text():
    seasonal = _seasonal(inits=["2026-01"], target="2026-02")
    mu, sd, init = freshest_seasonal(seasonal, "2026-02", datetime(2026, 2, 1, tzinfo=UTC))
    assert type(mu) is float and type(sd) is float and type(init) is str


def test_freshest_seasonal_needs_an_aware_decision_time():
    with pytest.raises(ValueError, match="aware"):
        freshest_seasonal(_seasonal(), "2026-02", datetime(2026, 2, 10))


# loading the daily series


def test_load_daily_sorts_and_keeps_the_first_stored_value_of_a_repeated_date(tmp_path):
    dates = ["2026-02-03", "2026-02-01", "2026-02-03", "2026-02-02"]
    _write_daily(tmp_path, dates, [3.0, 1.0, 99.0, 2.0])
    daily = load_daily(tmp_path)
    assert list(daily.index) == [pd.Timestamp(f"2026-02-0{d}") for d in (1, 2, 3)]
    assert daily.tolist() == [1.0, 2.0, 3.0]
    assert daily.index.name == "date" and daily.name == "t2m_global_K"
    assert daily.index.tz is None and daily.dtype == np.float64


def test_a_frame_from_the_loaded_parquet_equals_the_frame_from_the_same_series(tmp_path):
    series = _daily("2026-02-01", 28)
    _write_daily(tmp_path, [d.strftime("%Y-%m-%d") for d in series.index], series.tolist())
    pd.testing.assert_frame_equal(
        nowcast_frame(
            load_daily(tmp_path), _labels(), _seasonal(), bridge=_bridge(), months=["2026-02"]
        ),
        _feb_frame(),
    )


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_the_resolution_of_the_daily_index_does_not_change_the_frame(unit):
    daily = _daily("2026-02-01", 28)
    daily.index = daily.index.as_unit(unit)
    pd.testing.assert_frame_equal(_feb_frame(daily=daily), _feb_frame())


# the frame


def test_nowcast_frame_flags_a_gap():
    daily = _daily("2026-02-01", days=28).drop(pd.Timestamp("2026-02-04"))
    frame = nowcast_frame(daily, _labels(), _seasonal(), bridge=_bridge(), months=["2026-02"])
    row = frame[frame.checkpoint == "15"].iloc[0]
    assert row["gap_days"] == 1 and row["days_used"] == 9  # days 1..10 minus the missing 4th
    assert (frame[frame.checkpoint == "5"]["days_used"] == 0).all()  # the lag hides every day


def test_a_gap_on_the_last_visible_day_moves_the_era5_availability_back():
    daily = _daily("2026-02-01", 28).drop(pd.Timestamp("2026-02-10"))
    row = _row(_feb_frame(daily=daily), "15")
    assert row["days_used"] == 9 and row["gap_days"] == 1
    assert row["avail_era5_utc"] == datetime(2026, 2, 14, tzinfo=UTC)  # day 9 plus the lag


def test_nowcast_uses_the_freshest_available_init_only():
    seasonal = _seasonal(inits=["2026-01", "2026-02"], target="2026-02")
    frame = nowcast_frame(
        _daily("2026-02-01", 28), _labels(), seasonal, bridge=_bridge(), months=["2026-02"]
    )
    assert frame[frame.checkpoint == "10"]["seas_init"].iloc[0] == "2026-01"
    assert frame[frame.checkpoint == "15"]["seas_init"].iloc[0] == "2026-02"


def test_prev_label_availability_respects_the_print_date():
    labels = _labels(upto="2026-01")
    frame = nowcast_frame(
        _daily("2026-02-01", 28), labels, _seasonal(), bridge=_bridge(), months=["2026-02"]
    )
    row5, row15 = frame[frame.checkpoint == "5"].iloc[0], frame[frame.checkpoint == "15"].iloc[0]
    # 2026-01 prints on 2026-02-12: before that the latest printed label is 2025-12
    assert row5["prev_label_C"] == labels["2025-12"] and row5["prev_label_month"] == "2025-12"
    assert row5["avail_prev_label_utc"] == datetime(2026, 1, 12, tzinfo=timezone.utc)
    assert row15["prev_label_C"] == labels["2026-01"] and row15["prev_label_month"] == "2026-01"
    assert row15["avail_prev_label_utc"] == datetime(2026, 2, 12, tzinfo=timezone.utc)


def test_prev_label_is_the_latest_label_printed_by_each_checkpoint():
    labels = _labels()
    frame = _feb_frame(labels=labels)
    used = dict(zip(frame["checkpoint"], frame["prev_label_month"], strict=True))
    # 2026-01 prints on the 12th, so "5" and "10" still look at 2025-12
    assert used == {name: "2025-12" if name in ("5", "10") else "2026-01" for name in CHECKPOINTS}
    for _, row in frame.iterrows():
        assert row["prev_label_C"] == labels[row["prev_label_month"]]


def test_a_series_two_months_behind_leaves_the_previous_label_missing_and_visible():
    frame = _feb_frame(labels=_labels(upto="2025-11"))
    assert frame["prev_label_C"].isna().all()
    assert (frame["prev_label_month"] == "").all()
    assert (frame["avail_prev_label_utc"] == frame["decision_utc"]).all()
    row5, row15 = _row(frame, "5"), _row(frame, "15")
    assert np.isnan(row5["prev_label_C"]) and row5["prev_label_month"] == ""
    assert row5["avail_prev_label_utc"] == datetime(2026, 2, 5, tzinfo=UTC)
    assert np.isnan(row15["prev_label_C"]) and row15["prev_label_month"] == ""
    assert row15["avail_prev_label_utc"] == datetime(2026, 2, 15, tzinfo=UTC)


def test_a_printed_month_missing_from_the_series_is_not_replaced_by_an_older_one():
    for hole in (_labels().drop("2026-01"), _labels().mask(_labels().index == "2026-01")):
        frame = _feb_frame(labels=hole)
        row = _row(frame, "15")
        assert np.isnan(row["prev_label_C"]) and row["prev_label_month"] == ""
        assert row["avail_prev_label_utc"] == row["decision_utc"]
        assert _row(frame, "5")["prev_label_month"] == "2025-12"  # 2026-01 is not yet printed


def test_a_genuine_first_print_moves_the_label_times():
    ledger = _ledger(
        {"2026-01": "2026-02-03T08:00:00+00:00", "2026-02": "2026-03-09T13:30:00+00:00"}
    )
    labels = _labels()
    early = _row(_feb_frame(labels=labels, ledger=ledger), "5")
    assert early["prev_label_month"] == "2026-01"  # seen on the 3rd, before the 12th
    assert early["prev_label_C"] == labels["2026-01"]
    assert early["avail_prev_label_utc"] == datetime(2026, 2, 3, 8, tzinfo=UTC)
    assert early["avail_label_utc"] == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    plain = _row(_feb_frame(labels=labels), "5")
    assert plain["prev_label_month"] == "2025-12"
    assert plain["avail_label_utc"] == datetime(2026, 3, 12, tzinfo=UTC)


def test_a_first_print_seen_exactly_at_the_decision_is_public_then():
    ledger = _ledger({"2026-01": "2026-02-10T00:00:00+00:00"})
    frame = _feb_frame(ledger=ledger)
    row = _row(frame, "10")
    assert row["prev_label_month"] == "2026-01"
    assert row["avail_prev_label_utc"] == row["decision_utc"]
    assert _row(frame, "5")["prev_label_month"] == "2025-12"
    assert leakage_violations(frame, "decision_utc", LEAKAGE_INPUTS).empty


def test_the_lookback_stops_at_two_months_when_both_labels_are_still_unseen():
    ledger = _ledger(
        {"2025-12": "2026-02-20T00:00:00+00:00", "2026-01": "2026-02-25T00:00:00+00:00"}
    )
    frame = _feb_frame(ledger=ledger)
    used = dict(zip(frame["checkpoint"], frame["prev_label_month"], strict=True))
    # 2025-11 is public by the rule, but it is a third month back: it stays unused
    assert used["5"] == used["10"] == used["15"] == ""
    assert used["20"] == "2025-12" and used["25"] == "2026-01"
    row = _row(frame, "15")
    assert np.isnan(row["prev_label_C"]) and row["avail_prev_label_utc"] == row["decision_utc"]
    assert leakage_violations(frame, "decision_utc", LEAKAGE_INPUTS).empty


def test_a_late_first_print_keeps_the_label_out_until_it_is_seen():
    ledger = _ledger({"2026-01": "2026-02-20T08:00:00+00:00"})
    frame = _feb_frame(ledger=ledger)
    assert _row(frame, "20")["prev_label_month"] == "2025-12"  # 20th 00:00 is before 08:00
    late = _row(frame, "25")
    assert late["prev_label_month"] == "2026-01"
    assert late["avail_prev_label_utc"] == datetime(2026, 2, 20, 8, tzinfo=UTC)
    assert leakage_violations(frame, "decision_utc", LEAKAGE_INPUTS).empty


def test_full_month_mean_is_nan_until_the_month_is_complete():
    frame = nowcast_frame(
        _daily("2026-02-01", 20), _labels(), _seasonal(), bridge=_bridge(), months=["2026-02"]
    )
    assert np.isnan(frame["full_month_mean_K"]).all()


def test_full_month_mean_is_the_same_on_every_row_of_a_complete_month():
    frame = _feb_frame()
    assert (frame["full_month_mean_K"] == 294.5).all()  # mean of 281 .. 308
    assert _row(frame, "+6")["mtd_mean_K"] == _row(frame, "+6")["full_month_mean_K"]


def test_a_gap_anywhere_in_the_month_removes_its_full_month_mean():
    daily = _daily("2026-02-01", 28).drop(pd.Timestamp("2026-02-27"))
    frame = _feb_frame(daily=daily)
    assert frame["full_month_mean_K"].isna().all()
    assert _row(frame, "15")["gap_days"] == 0  # the missing day is beyond what "15" sees
    assert _row(frame, "+6")["gap_days"] == 1


def test_no_leakage_in_a_synthetic_nowcast_frame():
    frame = nowcast_frame(
        _daily("2026-02-01", 28),
        _labels(),
        _seasonal(inits=["2026-01", "2026-02"], target="2026-02"),
        bridge=_bridge(),
        months=["2026-02"],
    )
    assert leakage_violations(
        frame, "decision_utc", ["avail_era5_utc", "avail_seas_utc", "avail_prev_label_utc"]
    ).empty


def test_no_leakage_over_two_years_with_first_prints_in_the_ledger():
    months = pd.period_range("2024-01", "2026-02", freq="M").astype(str).tolist()
    ledger = _ledger(
        {"2025-10": "2025-11-10T13:30:00+00:00", "2025-11": "2025-12-20T09:00:00+00:00"}
    )
    frame = nowcast_frame(
        _daily("2024-01-01", 790),
        _labels(first="2023-01"),
        _seasonal_all(months),
        bridge=_bridge(),
        ledger=ledger,
    )
    assert len(frame) == 9 * len(months)
    assert leakage_violations(frame, "decision_utc", LEAKAGE_INPUTS).empty
    # the ledger decides which of the two months the December rows can see
    assert _row(frame, "15", "2025-12")["prev_label_month"] == "2025-10"
    assert _row(frame, "25", "2025-12")["prev_label_month"] == "2025-11"
    assert _row(frame, "25", "2025-12")["avail_prev_label_utc"] == datetime(
        2025, 12, 20, 9, tzinfo=UTC
    )


def test_every_checkpoint_sees_the_days_public_through_the_lag():
    frame = _feb_frame()
    assert list(frame["checkpoint"]) == list(CHECKPOINTS)
    assert dict(zip(frame["checkpoint"], frame["days_used"], strict=True)) == FEB_DAYS_USED
    assert (frame["days_in_month"] == 28).all() and (frame["gap_days"] == 0).all()
    expected_mean = [280.0 + (n + 1) / 2 if n else np.nan for n in FEB_DAYS_USED.values()]
    np.testing.assert_allclose(frame["mtd_mean_K"], expected_mean)
    np.testing.assert_allclose(frame["frac_days_used"], [n / 28 for n in FEB_DAYS_USED.values()])
    decisions = [(2, 5), (2, 10), (2, 15), (2, 20), (2, 25), (3, 1), (3, 3), (3, 6), (3, 10)]
    assert list(frame["decision_utc"]) == [datetime(2026, m, d, tzinfo=UTC) for m, d in decisions]
    # the last day used plus the lag; "5" uses no day and holds the decision time itself
    available = [(2, 5), (2, 10), (2, 15), (2, 20), (2, 25), (3, 1), (3, 3), (3, 5), (3, 5)]
    assert list(frame["avail_era5_utc"]) == [datetime(2026, m, d, tzinfo=UTC) for m, d in available]


def test_after_the_month_only_plus_one_and_plus_three_are_incomplete():
    frame = _feb_frame()
    after = frame[frame["checkpoint"].str.startswith("+")]
    assert list(after["checkpoint"]) == ["+1", "+3", "+6", "+10"]
    assert list(after["days_used"] < after["days_in_month"]) == [True, True, False, False]


def test_month_inputs_are_encoded_like_the_seasonal_frame():
    row = _row(_feb_frame(), "15")
    assert row["month_of_year"] == 2 and row["days_in_month"] == 28
    assert row["moy_sin"] == pytest.approx(math.sqrt(3) / 2)  # 2 * pi * 2 / 12, as in features.py
    assert row["moy_cos"] == pytest.approx(0.5)
    assert row["target_year_frac"] == pytest.approx(2026.0 + 1.5 / 12.0)
    assert _row(_feb_frame(), "+1")["frac_days_used"] == pytest.approx(24 / 28)


def test_the_month_encodings_equal_the_seasonal_frames_for_every_month_of_the_year():
    inits = pd.date_range("2020-01-01", periods=12, freq="MS")
    members = np.arange(10)
    seas5 = pd.DataFrame(
        {
            "init_date": np.repeat(inits, len(members)),
            "lead_month": 1,
            "member": np.tile(members, len(inits)),
            "t2m_global_K": 287.0 + 0.01 * np.arange(12 * len(members)),
        }
    )
    months = pd.period_range("2020-01", "2020-12", freq="M").astype(str)
    era5_K = pd.Series(287.0 + np.arange(12) * 0.1, index=months)
    noaa_C = pd.Series(0.5 + np.arange(12) * 0.01, index=months)
    seasonal = seasonal_frame(seas5, era5_K, noaa_C, bridge=_bridge())
    frame = nowcast_frame(_daily("2020-01-01", 366), noaa_C, seasonal, bridge=_bridge())
    assert sorted(frame["month_of_year"].unique()) == list(range(1, 13))
    reference = seasonal.set_index("target_month")
    for _, row in frame.iterrows():
        expected = reference.loc[row["target_month"]]
        for column in ("moy_sin", "moy_cos", "target_year_frac"):
            assert row[column] == pytest.approx(expected[column], abs=1e-12), column


def test_month_to_date_is_bridged_with_the_month_of_the_target():
    bridge = Bridge(alpha={m: -280.0 - m for m in range(1, 13)}, beta=2.0, resid_sd=0.03)
    frame = nowcast_frame(
        _daily("2026-02-01", 28), _labels(), _seasonal(), bridge=bridge, months=["2026-02"]
    )
    row = _row(frame, "15")
    assert row["mtd_mean_C"] == pytest.approx(-282.0 + 2.0 * 285.5)
    assert np.isnan(_row(frame, "5")["mtd_mean_C"])
    expected = bridge.predict_mean(
        frame["mtd_mean_K"].to_numpy(), frame["month_of_year"].to_numpy()
    )
    np.testing.assert_allclose(frame["mtd_mean_C"], expected)


def test_the_label_is_the_target_months_and_is_public_after_the_checkpoints():
    labels = _labels()
    frame = _feb_frame(labels=labels)
    assert (frame["y_C"] == labels["2026-02"]).all()
    assert (frame["avail_label_utc"] == datetime(2026, 3, 12, tzinfo=UTC)).all()
    assert (frame["avail_label_utc"] > frame["decision_utc"]).all()


def test_an_unlabelled_target_month_has_a_nan_label_and_still_a_print_time():
    frame = _feb_frame(labels=_labels(upto="2026-01"))
    assert frame["y_C"].isna().all()
    assert (frame["avail_label_utc"] == datetime(2026, 3, 12, tzinfo=UTC)).all()


def test_a_seasonal_frame_without_the_target_month_leaves_the_seasonal_inputs_unused():
    frame = _feb_frame(seasonal=_seasonal(target="2026-03"))
    assert frame["seas_mu"].isna().all() and frame["seas_sigma"].isna().all()
    assert (frame["seas_init"] == "").all()
    assert (frame["avail_seas_utc"] == frame["decision_utc"]).all()
    assert leakage_violations(frame, "decision_utc", LEAKAGE_INPUTS).empty
    empty = _feb_frame(seasonal=_seasonal(inits=[]))
    assert empty["seas_mu"].isna().all() and (empty["seas_init"] == "").all()


def test_the_frame_chooses_a_seasonal_row_by_its_own_decision_time():
    seasonal = _seasonal(inits=["2026-01", "2026-02"], target="2026-02")
    stamps = ["2026-01-13T12:00:00Z", "2026-02-15T01:00:00Z"]
    seasonal["decision_utc"] = pd.to_datetime(stamps, utc=True)
    frame = _feb_frame(seasonal=seasonal)
    assert _row(frame, "15")["seas_init"] == "2026-01"  # released an hour after the checkpoint
    assert _row(frame, "20")["seas_init"] == "2026-02"
    seasonal["decision_utc"] = pd.to_datetime([stamps[0], "2026-02-15T00:00:00Z"], utc=True)
    assert _row(_feb_frame(seasonal=seasonal), "15")["seas_init"] == "2026-02"  # at the checkpoint


def test_a_seasonal_init_is_unused_until_it_is_released():
    frame = _feb_frame(seasonal=_seasonal(inits=["2026-02"], target="2026-02"))
    for name in ("5", "10"):  # released on 2026-02-13 at 12:00
        row = _row(frame, name)
        assert np.isnan(row["seas_mu"]) and row["seas_init"] == ""
        assert row["avail_seas_utc"] == row["decision_utc"]
    row = _row(frame, "15")
    assert row["seas_mu"] == pytest.approx(1.0) and row["seas_sigma"] == pytest.approx(0.2)
    assert row["seas_init"] == "2026-02"
    assert row["avail_seas_utc"] == datetime(2026, 2, 13, 12, tzinfo=UTC)


def test_a_month_the_series_never_reaches_is_all_gaps_and_uses_no_day():
    frame = _feb_frame(months=["2026-04"], seasonal=_seasonal(target="2026-04"))
    considered = [0, 5, 10, 15, 20, 26, 28, 30, 30]  # April has 30 days; "+1" is May 1
    assert list(frame["gap_days"]) == considered
    assert (frame["days_used"] == 0).all() and frame["mtd_mean_K"].isna().all()
    assert (frame["avail_era5_utc"] == frame["decision_utc"]).all()
    assert (frame["frac_days_used"] == 0.0).all()


def test_a_series_that_starts_late_or_ends_early_shows_its_gaps_row_by_row():
    frame = nowcast_frame(
        _daily("2026-01-20", 50),
        _labels(),
        _seasonal(),
        bridge=_bridge(),
        months=["2026-01", "2026-03"],
    )
    jan25 = _row(frame, "25", "2026-01")  # days 1..20 are visible, the series starts on the 20th
    assert jan25["days_used"] == 1 and jan25["gap_days"] == 19
    mar15, mar20 = _row(frame, "15", "2026-03"), _row(frame, "20", "2026-03")
    assert mar15["days_used"] == 10 and mar15["gap_days"] == 0
    assert mar20["days_used"] == 10 and mar20["gap_days"] == 5  # the series ends on 2026-03-10


def test_a_leap_february_has_29_days_and_shifts_the_plus_checkpoints_by_a_day():
    leap = nowcast_frame(
        _daily("2028-02-01", 29),
        _labels(first="2027-01", upto="2028-02"),
        _seasonal(["2028-01"], "2028-02"),
        bridge=_bridge(),
        months=["2028-02"],
    )
    assert (leap["days_in_month"] == 29).all()
    assert _row(leap, "+1", "2028-02")["decision_utc"] == datetime(2028, 3, 1, tzinfo=UTC)
    assert _row(leap, "+1", "2028-02")["days_used"] == 25  # 2028-03-01 minus five days
    assert _row(leap, "+3", "2028-02")["days_used"] == 27
    assert _row(leap, "+6", "2028-02")["days_used"] == 29
    assert (leap["full_month_mean_K"] == 295.0).all()  # mean of 281 .. 309
    assert (leap["avail_label_utc"] == datetime(2028, 3, 12, tzinfo=UTC)).all()
    assert _row(_feb_frame(), "+1")["days_used"] == 24  # the same checkpoint in a common year


def test_a_december_target_runs_its_plus_checkpoints_into_january():
    frame = nowcast_frame(
        _daily("2025-12-01", 31),
        _labels(upto="2025-12"),
        _seasonal(["2025-11", "2025-12"], "2025-12"),
        bridge=_bridge(),
        months=["2025-12"],
    )
    assert _row(frame, "+1", "2025-12")["decision_utc"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert _row(frame, "+10", "2025-12")["decision_utc"] == datetime(2026, 1, 10, tzinfo=UTC)
    assert _row(frame, "+1", "2025-12")["days_used"] == 27
    assert _row(frame, "+6", "2025-12")["days_used"] == 31
    assert (frame["avail_label_utc"] == datetime(2026, 1, 12, tzinfo=UTC)).all()
    np.testing.assert_allclose(frame["target_year_frac"], 2025.0 + 11.5 / 12.0)
    used = dict(zip(frame["checkpoint"], frame["prev_label_month"], strict=True))
    assert used["5"] == "2025-10" and used["15"] == "2025-11" and used["+1"] == "2025-11"
    assert _row(frame, "10", "2025-12")["seas_init"] == "2025-11"
    assert _row(frame, "+1", "2025-12")["seas_init"] == "2025-12"
    assert leakage_violations(frame, "decision_utc", LEAKAGE_INPUTS).empty


def test_the_columns_are_the_agreed_ones_in_order_and_the_features_are_among_them():
    frame = _feb_frame()
    assert list(frame.columns) == EXPECTED_COLUMNS == NOWCAST_COLUMNS
    assert set(NOWCAST_FEATURES) <= set(frame.columns)
    assert len(frame) == len(CHECKPOINTS) and list(frame.index) == list(range(len(CHECKPOINTS)))


def test_the_column_types():
    frame = _feb_frame()
    for column in TIME_COLUMNS:
        assert isinstance(frame[column].dtype, pd.DatetimeTZDtype), column
        assert str(frame[column].dt.tz) == "UTC", column
        assert frame[column].notna().all(), column
    for column in INT_COLUMNS:
        assert frame[column].dtype == np.int64, column
    for column in FLOAT_COLUMNS:
        assert frame[column].dtype == np.float64, column
    for column in TEXT_COLUMNS:
        assert pd.api.types.is_string_dtype(frame[column]), column
    assert sorted(INT_COLUMNS + FLOAT_COLUMNS + TEXT_COLUMNS + TIME_COLUMNS) == sorted(
        EXPECTED_COLUMNS
    )


def test_the_frame_has_the_columns_and_kinds_that_task_4s_fixture_rows_assume():
    frame = nowcast_frame(_daily("2026-01-20", 50), _labels(), _seasonal(), bridge=_bridge())
    reference = nowcast_rows("15", 2)
    for column in reference.columns:
        assert column in frame.columns
        assert frame[column].dtype.kind == reference[column].dtype.kind, column
    assert frame["target_month"].is_monotonic_increasing
    assert set(frame["checkpoint"]) == set(CHECKPOINTS)


def test_the_features_are_finite_once_a_day_is_visible():
    frame = _feb_frame()
    features = frame[NOWCAST_FEATURES]
    assert np.isfinite(features[frame["checkpoint"] != "5"].to_numpy()).all()
    five = features[frame["checkpoint"] == "5"].iloc[0]
    assert np.isnan(five["mtd_mean_C"]) and five.drop("mtd_mean_C").notna().all()


def test_months_default_to_every_month_the_series_touches_and_come_out_in_order():
    frame = nowcast_frame(_daily("2026-01-20", 50), _labels(), _seasonal(), bridge=_bridge())
    assert list(frame["target_month"].unique()) == ["2026-01", "2026-02", "2026-03"]
    assert list(frame["checkpoint"]) == list(CHECKPOINTS) * 3
    assert list(frame.index) == list(range(27))


def test_months_may_be_given_in_any_order_and_repeated():
    frame = _feb_frame(months=["2026-03", "2026-02", "2026-03"])
    assert list(frame["target_month"].unique()) == ["2026-02", "2026-03"]
    assert len(frame) == 18
    pd.testing.assert_frame_equal(frame.iloc[:9], _feb_frame())


def test_a_malformed_month_in_the_list_is_refused():
    with pytest.raises(ValueError, match="YYYY-MM"):
        _feb_frame(months=["2026-02", "2026-2"])


def test_an_empty_series_gives_an_empty_frame_with_the_columns():
    frame = nowcast_frame(_daily("2026-02-01", 0), _labels(), _seasonal(), bridge=_bridge())
    assert len(frame) == 0 and list(frame.columns) == EXPECTED_COLUMNS
    empty_months = _feb_frame(months=[])
    assert len(empty_months) == 0 and list(empty_months.columns) == EXPECTED_COLUMNS


def test_an_empty_frame_has_the_column_types_of_a_full_one():
    empty = nowcast_frame(_daily("2026-02-01", 0), _labels(), _seasonal(), bridge=_bridge())
    full = _feb_frame()
    for column in EXPECTED_COLUMNS:
        if column in TIME_COLUMNS:
            assert isinstance(empty[column].dtype, pd.DatetimeTZDtype), column
            assert str(empty[column].dt.tz) == "UTC", column
        else:
            assert empty[column].dtype == full[column].dtype, column


def test_labels_indexed_by_periods_or_with_a_repeated_month_give_the_same_frame():
    labels = _labels()
    as_periods = labels.copy()
    as_periods.index = pd.PeriodIndex(labels.index, freq="M")
    repeated = pd.concat([labels, pd.Series([9.9], index=["2026-01"])])
    pd.testing.assert_frame_equal(_feb_frame(labels=as_periods), _feb_frame())
    pd.testing.assert_frame_equal(_feb_frame(labels=repeated), _feb_frame())


def test_the_inputs_are_not_modified():
    daily, labels = _daily("2026-02-01", 28), _labels()
    seasonal = _seasonal(inits=["2026-01", "2026-02"])
    ledger = _ledger({"2026-01": "2026-02-03T08:00:00+00:00"})
    copies = [part.copy() for part in (daily, labels, seasonal, ledger)]
    _feb_frame(daily=daily, labels=labels, seasonal=seasonal, ledger=ledger)
    pd.testing.assert_series_equal(daily, copies[0])
    pd.testing.assert_series_equal(labels, copies[1])
    pd.testing.assert_frame_equal(seasonal, copies[2])
    pd.testing.assert_frame_equal(ledger, copies[3])


def test_nothing_after_the_era5_horizon_reaches_a_row():
    daily = _daily("2026-02-01", 28)
    reference = _feb_frame(daily=daily)
    inputs = [
        "days_used",
        "gap_days",
        "mtd_mean_K",
        "mtd_mean_C",
        "frac_days_used",
        "avail_era5_utc",
    ]
    for name in CHECKPOINTS:
        decision = _row(reference, name)["decision_utc"]
        last_public_day = pd.Timestamp(decision.date()) - pd.Timedelta(days=5)
        poisoned = daily.copy()
        poisoned[poisoned.index > last_public_day] = 1.0e9
        row = _row(_feb_frame(daily=poisoned), name)
        pd.testing.assert_series_equal(row[inputs], _row(reference, name)[inputs])


def test_the_frame_rows_agree_with_month_to_date_when_the_series_has_holes():
    daily = _daily("2026-02-01", 28).drop([pd.Timestamp("2026-02-04"), pd.Timestamp("2026-02-22")])
    frame = _feb_frame(daily=daily)
    for _, row in frame.iterrows():
        last_visible = row["decision_utc"].date() - timedelta(days=5)
        mean, used, gap = month_to_date(daily, "2026-02", min(last_visible, date(2026, 2, 28)))
        assert (row["days_used"], row["gap_days"]) == (used, gap), row["checkpoint"]
        assert row["mtd_mean_K"] == pytest.approx(mean, nan_ok=True), row["checkpoint"]
    # day 4 is visible from "10" on, day 22 from "+1" on
    assert list(frame["gap_days"]) == [0, 1, 1, 1, 1, 2, 2, 2, 2]


def test_the_order_of_the_daily_series_does_not_change_the_frame():
    daily = _daily("2026-02-01", 28).drop(pd.Timestamp("2026-02-04"))
    pd.testing.assert_frame_equal(_feb_frame(daily=daily.iloc[::-1]), _feb_frame(daily=daily))


# the baselines of Task 4 on a real frame


def test_task_4_baselines_run_on_a_nowcast_frame():
    frame = nowcast_frame(
        _daily("2025-09-01", 181),  # 2025-09-01 .. 2026-02-28
        _labels(),
        _seasonal_all(["2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]),
        bridge=_bridge(),
    )
    train = frame[frame["target_month"] < "2026-02"]
    test = frame[frame["target_month"] == "2026-02"]
    mu, sigma = bridge_nowcast(train, test, bridge=_bridge())
    by_checkpoint = dict(zip(test["checkpoint"], zip(mu, sigma, strict=True), strict=True))
    assert np.isfinite(by_checkpoint["15"]).all()  # ERA5 days are visible at "15"
    assert math.isnan(by_checkpoint["5"][0])  # no day is visible at "5": nothing to bridge
    stats = fit_remainder(train)
    mtd_15 = _row(test, "15")["mtd_mean_K"]
    assert by_checkpoint["15"][0] == pytest.approx(
        _bridge().predict_mean(mtd_15 + stats.pooled_mean["15"], 2)
    )
    # the full month needs no remainder, whatever the training months say
    assert by_checkpoint["+6"][0] == pytest.approx(_row(test, "+6")["mtd_mean_C"])
    persistent_mu, persistent_sigma = persistence(train, test)
    assert np.isfinite(persistent_mu).all() and np.isfinite(persistent_sigma).all()
    assert persistent_mu[0] == _labels()["2025-12"] and persistent_mu[2] == _labels()["2026-01"]
