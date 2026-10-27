"""The exact as-of row, the nowcast payload and the two scripts that price with a saved model."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from climate.bridge import Bridge
from climate.dataset import build_frame_from_inputs, load_era5, load_noaa_gtm, load_seas5
from climate.emos import EmosModel
from climate.ladder import p_yes
from climate.nn.availability import seas5_available_on
from climate.nn.features import seasonal_frame
from climate.nn.nowcast import (
    CHECKPOINTS,
    NOWCAST_COLUMNS,
    checkpoint_decision,
    load_daily,
    month_to_date,
    nowcast_frame,
    nowcast_payload,
    nowcast_row,
)
from climate.nn.nowcast_model import GtmNowcastModel
from climate.nn.nowcast_windows import monthly_means
from climate.nn.seasonal_model import GtmNnModel
from climate.storage import LEDGER_COLUMNS, VINTAGE_LIVE, load_ledger
from scripts.research import gtm_nn_price, gtm_nowcast, nn_cli
from scripts.research.gtm_nn_price import main as price_main
from scripts.research.gtm_nowcast import main as nowcast_main

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "research"
PAYLOAD_KEYS = [
    "as_of",
    "target_month",
    "checkpoint",
    "mu",
    "sigma",
    "days_used",
    "gap_days",
    "last_era5_day",
    "seas_init",
    "prev_label_month",
    "model_fingerprint",
    "usable",
]
ISO_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
PRICE_HEADER = "strike | p_yes_emos | p_yes_nn"
PRICE_SEPARATOR = "|---|---|---|"


def _daily(first: str, last: str) -> pd.Series:
    """Daily K from first through last: 280 plus the day of the month."""
    days = pd.date_range(first, last, freq="D", name="date")
    return pd.Series(280.0 + days.day.to_numpy(), index=days, name="t2m_global_K")


def _labels(upto: str = "2026-10", first: str = "2025-01") -> pd.Series:
    """Monthly C labels from first through upto; the label of a month is 0.5 plus 0.01 per month."""
    months = pd.period_range(first, upto, freq="M").astype(str)
    return pd.Series(0.5 + 0.01 * np.arange(len(months)), index=months, name="y_C")


def _seasonal(inits, target: str) -> pd.DataFrame:
    """One seasonal row per init for one target month; mu and sd grow with the list position."""
    return pd.DataFrame(
        {
            "target_month": target,
            "init_month": list(inits),
            "decision_utc": pd.to_datetime([seas5_available_on(i) for i in inits], utc=True),
            "fc_mean_C": [1.0 + 0.1 * k for k in range(len(inits))],
            "fc_sd_C": [0.2 + 0.01 * k for k in range(len(inits))],
        }
    )


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


def _feb(**over) -> dict:
    """The inputs of 2026-02: complete daily data, labels through the month and two inits."""
    inputs = {
        "daily_K": _daily("2026-01-01", "2026-03-31"),
        "noaa_C": _labels("2026-02"),
        "seasonal": _seasonal(["2026-01", "2026-02"], "2026-02"),
        "bridge": _bridge(),
        "target_month": "2026-02",
    }
    return inputs | over


def _feb_row(decision: datetime, **over) -> pd.Series:
    return nowcast_row(**_feb(**over), decision=decision)


def _feb_frame(**over) -> pd.DataFrame:
    inputs = _feb(**over)
    month = inputs.pop("target_month")
    return nowcast_frame(
        inputs.pop("daily_K"),
        inputs.pop("noaa_C"),
        inputs.pop("seasonal"),
        months=[month],
        **inputs,
    )


def _nowcast_row(*, target: str, checkpoint: str, decision: datetime) -> pd.Series:
    """The exact row of the target month at the decision, from a complete daily series."""
    row = nowcast_row(
        _daily("2026-08-01", "2026-12-31"),
        _labels("2026-10"),
        _seasonal(["2026-09", "2026-10"], target),
        bridge=_bridge(),
        target_month=target,
        decision=decision,
    )
    assert row["checkpoint"] == checkpoint
    return row


# the payload


def test_payload_has_every_key_and_iso_times():
    row = _nowcast_row(
        target="2026-10", checkpoint="15", decision=datetime(2026, 10, 15, tzinfo=timezone.utc)
    )
    p = nowcast_payload(row, 1.21, 0.04, model_fingerprint="abc")
    assert set(p) == set(PAYLOAD_KEYS)
    assert p["as_of"] == "2026-10-15T00:00:00Z" and p["last_era5_day"] == "2026-10-10"
    assert p["usable"] is True


def test_the_payload_reports_the_row_and_the_gaussian_in_the_documented_order():
    row = _nowcast_row(
        target="2026-10", checkpoint="15", decision=datetime(2026, 10, 17, 13, 30, tzinfo=UTC)
    )
    p = nowcast_payload(row, 1.21, 0.04, model_fingerprint="abc")
    assert list(p) == PAYLOAD_KEYS
    assert p == {
        "as_of": "2026-10-17T13:30:00Z",
        "target_month": "2026-10",
        "checkpoint": "15",
        "mu": 1.21,
        "sigma": 0.04,
        "days_used": 12,
        "gap_days": 0,
        "last_era5_day": "2026-10-12",
        "seas_init": "2026-10",
        "prev_label_month": "2026-09",
        "model_fingerprint": "abc",
        "usable": True,
    }


def test_a_nan_gaussian_is_null_and_unusable_and_the_payload_is_strict_json():
    row = _nowcast_row(
        target="2026-10", checkpoint="5", decision=datetime(2026, 10, 3, tzinfo=timezone.utc)
    )
    p = nowcast_payload(row, float("nan"), float("nan"), model_fingerprint="abc")
    assert p["mu"] is None and p["sigma"] is None and p["usable"] is False
    assert p["checkpoint"] == "5" and p["days_used"] == 0 and p["gap_days"] == 0
    assert p["last_era5_day"] is None
    assert json.loads(json.dumps(p, allow_nan=False)) == p


def test_a_nan_gaussian_makes_a_checkpoint_after_5_unusable_too():
    row = _nowcast_row(
        target="2026-10", checkpoint="15", decision=datetime(2026, 10, 15, tzinfo=UTC)
    )
    p = nowcast_payload(row, float("nan"), float("nan"), model_fingerprint="abc")
    assert (p["checkpoint"], p["days_used"], p["gap_days"]) == ("15", 10, 0)
    assert p["mu"] is None and p["sigma"] is None and p["usable"] is False


def test_a_gap_makes_the_payload_unusable_whatever_the_gaussian():
    daily = _daily("2026-08-01", "2026-12-31").drop(pd.Timestamp("2026-10-04"))
    row = nowcast_row(
        daily,
        _labels("2026-10"),
        _seasonal(["2026-10"], "2026-10"),
        bridge=_bridge(),
        target_month="2026-10",
        decision=datetime(2026, 10, 15, tzinfo=UTC),
    )
    p = nowcast_payload(row, 1.2, 0.1, model_fingerprint="abc")
    assert p["gap_days"] == 1 and p["days_used"] == 9
    assert p["mu"] == 1.2 and p["usable"] is False


def test_checkpoint_5_is_not_usable_with_days_used_and_a_finite_gaussian():
    row = _nowcast_row(target="2026-10", checkpoint="5", decision=datetime(2026, 10, 8, tzinfo=UTC))
    p = nowcast_payload(row, 1.21, 0.04, model_fingerprint="abc")
    assert (p["checkpoint"], p["days_used"], p["gap_days"]) == ("5", 3, 0)
    assert p["mu"] == 1.21 and p["sigma"] == 0.04 and p["usable"] is False


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_every_checkpoint_but_5_is_usable_with_a_finite_gaussian_and_no_gap(checkpoint):
    decision = checkpoint_decision("2026-10", checkpoint)
    row = _nowcast_row(target="2026-10", checkpoint=checkpoint, decision=decision)
    p = nowcast_payload(row, 1.21, 0.04, model_fingerprint="abc")
    assert p["gap_days"] == 0 and p["mu"] == 1.21
    assert p["usable"] is (checkpoint != "5")


@pytest.mark.parametrize(
    "decision, last_day",
    [
        (datetime(2026, 9, 20, tzinfo=UTC), None),
        (datetime(2026, 10, 5, 23, 59, 59, tzinfo=UTC), None),
        (datetime(2026, 10, 6, tzinfo=UTC), "2026-10-01"),
        (datetime(2026, 10, 15, tzinfo=UTC), "2026-10-10"),
        (datetime(2026, 10, 31, tzinfo=UTC), "2026-10-26"),
        (datetime(2026, 11, 4, 23, 59, 59, tzinfo=UTC), "2026-10-30"),
        (datetime(2026, 11, 5, tzinfo=UTC), "2026-10-31"),
        (datetime(2026, 11, 10, tzinfo=UTC), "2026-10-31"),
        (datetime(2027, 3, 1, tzinfo=UTC), "2026-10-31"),
    ],
)
def test_the_last_era5_day_is_the_last_public_day_capped_at_the_month_end(decision, last_day):
    row = nowcast_row(
        _daily("2026-08-01", "2027-03-31"),
        _labels("2026-10"),
        _seasonal(["2026-10"], "2026-10"),
        bridge=_bridge(),
        target_month="2026-10",
        decision=decision,
    )
    p = nowcast_payload(row, 1.0, 0.1, model_fingerprint="abc")
    assert p["last_era5_day"] == last_day
    assert (p["days_used"] == 0) == (last_day is None)


def test_the_last_era5_day_does_not_come_from_the_availability_column():
    daily = _daily("2026-08-01", "2026-12-31").drop(pd.Timestamp("2026-10-10"))
    row = nowcast_row(
        daily,
        _labels("2026-10"),
        _seasonal(["2026-10"], "2026-10"),
        bridge=_bridge(),
        target_month="2026-10",
        decision=datetime(2026, 10, 15, tzinfo=UTC),
    )
    assert row["avail_era5_utc"] == pd.Timestamp("2026-10-14", tz="UTC")  # day 9 plus the lag
    p = nowcast_payload(row, 1.0, 0.1, model_fingerprint="abc")
    assert p["last_era5_day"] == "2026-10-10" and p["gap_days"] == 1


def test_every_payload_value_is_plain_json_even_from_a_row_of_numpy_scalars():
    row = _nowcast_row(
        target="2026-10", checkpoint="15", decision=datetime(2026, 10, 15, tzinfo=UTC)
    )
    p = nowcast_payload(row, np.float64(1.21), np.float64(0.04), model_fingerprint="abc")
    assert type(p["mu"]) is float and type(p["sigma"]) is float
    assert type(p["days_used"]) is int and type(p["gap_days"]) is int
    assert type(p["usable"]) is bool and type(p["as_of"]) is str
    assert json.dumps(p, allow_nan=False)


def test_the_payload_takes_a_row_of_the_frame_as_well():
    frame = _feb_frame()
    row = frame[frame["checkpoint"] == "15"].iloc[0]
    p = nowcast_payload(row, 1.0, 0.1, model_fingerprint="abc")
    assert p["as_of"] == "2026-02-15T00:00:00Z" and p["checkpoint"] == "15"
    assert p["days_used"] == 10 and p["last_era5_day"] == "2026-02-10"


def test_a_missing_seasonal_init_and_previous_label_stay_empty_strings():
    row = nowcast_row(
        _daily("2026-08-01", "2026-12-31"),
        _labels("2026-07"),  # the labels stop three months before the target
        _seasonal(["2026-10"], "2026-09"),  # and the seasonal rows are for another month
        bridge=_bridge(),
        target_month="2026-10",
        decision=datetime(2026, 10, 15, tzinfo=UTC),
    )
    p = nowcast_payload(row, float("nan"), float("nan"), model_fingerprint="abc")
    assert p["seas_init"] == "" and p["prev_label_month"] == ""


def test_the_payload_converts_a_decision_in_another_zone_to_utc_and_refuses_a_naive_one():
    row = _nowcast_row(
        target="2026-10", checkpoint="15", decision=datetime(2026, 10, 15, tzinfo=UTC)
    )
    zoned = row.copy()
    zoned["decision_utc"] = pd.Timestamp("2026-10-15T02:00:00+02:00")
    assert nowcast_payload(zoned, 1.0, 0.1, model_fingerprint="abc")["as_of"] == (
        "2026-10-15T00:00:00Z"
    )
    naive = row.copy()
    naive["decision_utc"] = pd.Timestamp("2026-10-15")
    with pytest.raises(ValueError, match="timezone-aware"):
        nowcast_payload(naive, 1.0, 0.1, model_fingerprint="abc")


# the exact as-of row


def test_the_row_has_exactly_the_frames_columns_in_order():
    row = _feb_row(datetime(2026, 2, 17, 13, 30, tzinfo=UTC))
    assert isinstance(row, pd.Series) and list(row.index) == NOWCAST_COLUMNS


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_at_a_checkpoints_own_decision_the_row_is_the_frames_row(checkpoint):
    daily = _daily("2026-01-01", "2026-03-31").drop(pd.Timestamp("2026-02-04"))
    ledger = _ledger({"2026-01": "2026-02-14T06:00:00+00:00"})
    frame = _feb_frame(daily_K=daily, ledger=ledger)
    row = _feb_row(checkpoint_decision("2026-02", checkpoint), daily_K=daily, ledger=ledger)
    expected = frame[frame["checkpoint"] == checkpoint].iloc[0]
    pd.testing.assert_series_equal(row, expected, check_names=False, check_exact=True)


def test_between_checkpoints_the_row_follows_the_exact_decision_not_the_last_checkpoint():
    decision = datetime(2026, 2, 17, 13, 30, tzinfo=UTC)
    row = _feb_row(decision)
    assert row["checkpoint"] == "15" and row["decision_utc"] == pd.Timestamp(decision)
    assert (row["days_used"], row["gap_days"], row["days_in_month"]) == (12, 0, 28)
    assert row["mtd_mean_K"] == pytest.approx(286.5)  # days 1 to 12: 281 to 292
    assert row["mtd_mean_C"] == pytest.approx(-0.5)
    assert row["frac_days_used"] == pytest.approx(12 / 28)
    assert row["avail_era5_utc"] == pd.Timestamp("2026-02-17", tz="UTC")  # day 12 plus the lag
    assert row["prev_label_month"] == "2026-01" and row["prev_label_C"] == pytest.approx(0.62)
    assert row["avail_prev_label_utc"] == pd.Timestamp("2026-02-12", tz="UTC")
    assert row["seas_init"] == "2026-02" and row["seas_mu"] == pytest.approx(1.1)
    assert row["seas_sigma"] == pytest.approx(0.21)
    assert row["avail_seas_utc"] == pd.Timestamp("2026-02-13T12:00:00", tz="UTC")
    assert row["y_C"] == pytest.approx(0.63) and np.isfinite(row["full_month_mean_K"])
    stale = _feb_frame().query("checkpoint == '15'").iloc[0]
    assert stale["days_used"] == 10 and stale["mtd_mean_K"] == pytest.approx(285.5)


@pytest.mark.parametrize(
    "decision, checkpoint",
    [
        (datetime(2025, 12, 15, tzinfo=UTC), "5"),
        (datetime(2026, 2, 1, tzinfo=UTC), "5"),
        (datetime(2026, 2, 4, 23, 59, 59, tzinfo=UTC), "5"),
        (datetime(2026, 2, 5, tzinfo=UTC), "5"),
        (datetime(2026, 2, 9, 23, 59, 59, tzinfo=UTC), "5"),
        (datetime(2026, 2, 10, tzinfo=UTC), "10"),
        (datetime(2026, 2, 14, 23, 59, 59, tzinfo=UTC), "10"),
        (datetime(2026, 2, 15, tzinfo=UTC), "15"),
        (datetime(2026, 2, 19, 23, 59, 59, tzinfo=UTC), "15"),
        (datetime(2026, 2, 20, tzinfo=UTC), "20"),
        (datetime(2026, 2, 24, 23, 59, 59, tzinfo=UTC), "20"),
        (datetime(2026, 2, 25, tzinfo=UTC), "25"),
        (datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC), "25"),
        (datetime(2026, 3, 1, tzinfo=UTC), "+1"),
        (datetime(2026, 3, 2, 23, 59, 59, tzinfo=UTC), "+1"),
        (datetime(2026, 3, 3, tzinfo=UTC), "+3"),
        (datetime(2026, 3, 5, 23, 59, 59, tzinfo=UTC), "+3"),
        (datetime(2026, 3, 6, tzinfo=UTC), "+6"),
        (datetime(2026, 3, 9, 23, 59, 59, tzinfo=UTC), "+6"),
        (datetime(2026, 3, 10, tzinfo=UTC), "+10"),
        (datetime(2026, 6, 1, tzinfo=UTC), "+10"),
    ],
)
def test_the_checkpoint_is_the_latest_one_decided_at_or_before_the_time(decision, checkpoint):
    assert _feb_row(decision)["checkpoint"] == checkpoint


def test_before_day_5_the_row_is_checkpoint_5_with_no_day_used():
    decision = datetime(2026, 2, 3, 10, tzinfo=UTC)
    row = _feb_row(decision)
    assert row["checkpoint"] == "5" and row["days_used"] == 0 and row["gap_days"] == 0
    assert np.isnan(row["mtd_mean_K"]) and np.isnan(row["mtd_mean_C"])
    assert row["frac_days_used"] == 0.0 and row["avail_era5_utc"] == pd.Timestamp(decision)
    p = nowcast_payload(row, float("nan"), float("nan"), model_fingerprint="abc")
    assert p["checkpoint"] == "5" and p["days_used"] == 0
    assert p["last_era5_day"] is None and p["usable"] is False


def test_after_plus_10_the_row_is_plus_10_with_every_day_of_the_month():
    row = _feb_row(datetime(2026, 3, 25, 8, tzinfo=UTC))
    assert row["checkpoint"] == "+10" and row["days_used"] == 28 and row["gap_days"] == 0
    assert row["frac_days_used"] == 1.0
    assert row["mtd_mean_K"] == pytest.approx(row["full_month_mean_K"])
    p = nowcast_payload(row, 1.0, 0.1, model_fingerprint="abc")
    assert p["last_era5_day"] == "2026-02-28"


def test_a_decision_in_another_zone_is_the_same_instant_in_utc():
    zoned = datetime(2026, 2, 17, 1, 30, tzinfo=timezone(timedelta(hours=2)))
    row = _feb_row(zoned)
    same = _feb_row(datetime(2026, 2, 16, 23, 30, tzinfo=UTC))
    pd.testing.assert_series_equal(row, same, check_names=False, check_exact=True)
    assert row["decision_utc"] == pd.Timestamp("2026-02-16T23:30:00", tz="UTC")
    assert row["days_used"] == 11  # the UTC date is the 16th, so the last public day is the 11th


def test_the_seasonal_init_is_the_freshest_one_public_at_the_exact_time():
    before = _feb_row(datetime(2026, 2, 13, 11, 59, 59, tzinfo=UTC))
    at = _feb_row(datetime(2026, 2, 13, 12, tzinfo=UTC))
    assert (before["seas_init"], before["seas_mu"]) == ("2026-01", pytest.approx(1.0))
    assert (at["seas_init"], at["seas_mu"]) == ("2026-02", pytest.approx(1.1))
    assert at["avail_seas_utc"] == pd.Timestamp("2026-02-13T12:00:00", tz="UTC")
    frame = _feb_frame()
    assert frame[frame["checkpoint"] == "10"].iloc[0]["seas_init"] == "2026-01"  # the stale pick


def test_the_previous_label_is_the_latest_one_printed_at_the_exact_time():
    before = _feb_row(datetime(2026, 2, 11, 23, 59, 59, tzinfo=UTC))
    at = _feb_row(datetime(2026, 2, 12, tzinfo=UTC))
    assert before["prev_label_month"] == "2025-12"
    assert at["prev_label_month"] == "2026-01" and at["prev_label_C"] == pytest.approx(0.62)


def test_a_first_print_seen_at_the_exact_time_is_public_then_and_not_a_second_before():
    ledger = _ledger({"2026-01": "2026-02-14T06:00:00+00:00"})
    before = _feb_row(datetime(2026, 2, 14, 5, 59, 59, tzinfo=UTC), ledger=ledger)
    at = _feb_row(datetime(2026, 2, 14, 6, tzinfo=UTC), ledger=ledger)
    assert before["prev_label_month"] == "2025-12"
    assert at["prev_label_month"] == "2026-01"
    assert at["avail_prev_label_utc"] == pd.Timestamp("2026-02-14T06:00:00", tz="UTC")
    assert at["avail_label_utc"] == pd.Timestamp("2026-03-12", tz="UTC")


def test_a_gap_day_is_counted_on_the_exact_row():
    daily = _daily("2026-01-01", "2026-03-31").drop(pd.Timestamp("2026-02-04"))
    row = _feb_row(datetime(2026, 2, 17, 13, 30, tzinfo=UTC), daily_K=daily)
    assert row["days_used"] == 11 and row["gap_days"] == 1
    assert row["avail_era5_utc"] == pd.Timestamp("2026-02-17", tz="UTC")
    assert row["frac_days_used"] == pytest.approx(11 / 28)


def test_a_month_the_series_never_reaches_is_all_gaps_and_uses_no_day():
    row = _feb_row(datetime(2026, 2, 17, tzinfo=UTC), daily_K=_daily("2025-06-01", "2025-12-31"))
    assert row["days_used"] == 0 and row["gap_days"] == 12 and np.isnan(row["mtd_mean_K"])


def test_the_row_leaves_its_inputs_alone():
    inputs = _feb()
    copies = {k: v.copy() for k, v in inputs.items() if isinstance(v, (pd.Series, pd.DataFrame))}
    nowcast_row(**inputs, decision=datetime(2026, 2, 17, 13, 30, tzinfo=UTC))
    for name, copy in copies.items():
        assert inputs[name].equals(copy), name


def test_the_row_needs_an_aware_decision_and_a_real_month():
    with pytest.raises(ValueError, match="timezone-aware"):
        _feb_row(datetime(2026, 2, 17, 13, 30))
    for month in ("2026-2", "2026-13", "feb", ""):
        with pytest.raises(ValueError, match="YYYY-MM"):
            _feb_row(datetime(2026, 2, 17, tzinfo=UTC), target_month=month)


def test_a_daily_index_with_a_time_of_day_is_refused_instead_of_read_as_all_gaps():
    daily = _daily("2026-01-01", "2026-03-31")
    daily.index = daily.index + pd.Timedelta(hours=6)
    with pytest.raises(ValueError, match="daily_K.*time of day"):
        month_to_date(daily, "2026-02", date(2026, 2, 5))
    with pytest.raises(ValueError, match="daily_K.*time of day"):
        _feb_frame(daily_K=daily)
    with pytest.raises(ValueError, match="daily_K.*time of day"):
        _feb_row(datetime(2026, 2, 17, tzinfo=UTC), daily_K=daily)


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_a_midnight_index_of_any_resolution_is_still_accepted(unit):
    daily = _daily("2026-01-01", "2026-03-31")
    daily.index = daily.index.as_unit(unit)
    decision = datetime(2026, 2, 17, 13, 30, tzinfo=UTC)
    pd.testing.assert_series_equal(
        _feb_row(decision, daily_K=daily), _feb_row(decision), check_exact=True
    )


# the nowcast script


def _nowcast(tmp_path, root, model_dir, *argv):
    """(exit code, payload from --out or None) of the script on the root and the model."""
    out = tmp_path / "payload.json"
    given = ["--root", str(root), "--model-dir", str(model_dir), "--out", str(out), *argv]
    rc = nowcast_main(given)
    return rc, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else None)


def _provenance(model_dir: Path) -> dict:
    return json.loads((model_dir / "provenance.json").read_text(encoding="utf-8"))


def _frame_gaussian(root, model_dir, month, checkpoint):
    """(mu, sigma) of the saved nowcast model on the frame row of the checkpoint, via the frame."""
    bridge = Bridge.from_json(json.dumps(_provenance(model_dir)["bridge"]))
    daily_K, noaa_C = load_daily(root), load_noaa_gtm(root)
    seasonal = seasonal_frame(load_seas5(root), monthly_means(daily_K), noaa_C, bridge=bridge)
    frame = nowcast_frame(
        daily_K, noaa_C, seasonal, bridge=bridge, ledger=load_ledger(root), months=[month]
    )
    row = frame[frame["checkpoint"] == checkpoint]
    return GtmNowcastModel.load(model_dir).predict(row).iloc[0]


def _copy_model(model_dir, tmp_path, name="model") -> Path:
    copy = tmp_path / name
    shutil.copytree(model_dir, copy)
    return copy


def test_nowcast_cli_marks_a_gap_as_unusable_and_exits_3(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    pytest.importorskip("torch")
    rc = nowcast_main(
        [
            "--root",
            str(synthetic_root_with_daily),
            "--model-dir",
            str(saved_nowcast_model),
            "--as-of",
            "2011-05-20T00:00:00Z",
            "--out",
            str(tmp_path / "p.json"),
        ]
    )
    payload = json.loads((tmp_path / "p.json").read_text())
    assert rc == 3 and payload["usable"] is False and payload["gap_days"] == 1  # 2011-05-04 is out


def test_the_unusable_payload_is_still_printed_and_the_reason_goes_to_stderr(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    rc, written = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2011-05-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 3 and json.loads(captured.out) == written
    assert list(written) == PAYLOAD_KEYS and written["mu"] is None
    assert written["checkpoint"] == "20" and written["days_used"] == 14
    assert written["last_era5_day"] == "2011-05-15" and written["target_month"] == "2011-05"
    notes = captured.err.strip().splitlines()
    assert len(notes) == 2 and notes[1].startswith("not usable") and "1 public day" in notes[1]


def test_the_context_line_names_the_model_and_the_labels_it_saw(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    _, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2011-06-20T00:00:00Z"
    )
    note = f"nowcast model {payload['model_fingerprint']} trained on labels through 2010-12"
    assert capsys.readouterr().err.strip() == note  # usable: no reason line follows


def test_a_gapless_month_is_usable_and_exits_0_with_a_gaussian(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2011-06-20T00:00:00Z"
    )
    assert rc == 0 and payload["usable"] is True and json.loads(capsys.readouterr().out) == payload
    assert payload["as_of"] == "2011-06-20T00:00:00Z" and payload["checkpoint"] == "20"
    assert (payload["days_used"], payload["gap_days"]) == (15, 0)
    assert payload["last_era5_day"] == "2011-06-15" and payload["target_month"] == "2011-06"
    assert payload["seas_init"] == "2011-06" and payload["prev_label_month"] == "2011-05"
    assert np.isfinite(payload["mu"]) and payload["sigma"] > 0
    fingerprint = GtmNowcastModel.load(saved_nowcast_model).fingerprint()
    assert payload["model_fingerprint"] == fingerprint and len(fingerprint) == 16
    assert ISO_UTC.fullmatch(payload["as_of"])


def test_at_a_checkpoints_decision_the_payload_is_the_models_answer_on_the_frames_row(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2011-06-15T00:00:00Z"
    )
    expected = _frame_gaussian(synthetic_root_with_daily, saved_nowcast_model, "2011-06", "15")
    assert rc == 0 and payload["checkpoint"] == "15" and payload["days_used"] == 10
    assert payload["mu"] == pytest.approx(expected["mu"], abs=1e-9)
    assert payload["sigma"] == pytest.approx(expected["sigma"], abs=1e-9)


def test_the_bridge_comes_from_the_provenance_and_is_never_refitted(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    argv = ["--as-of", "2011-06-15T00:00:00Z"]
    _, original = _nowcast(tmp_path, synthetic_root_with_daily, saved_nowcast_model, *argv)
    shifted_dir = _copy_model(saved_nowcast_model, tmp_path, "shifted")
    provenance = _provenance(shifted_dir)
    provenance["bridge"]["alpha"] = {k: v + 1.0 for k, v in provenance["bridge"]["alpha"].items()}
    (shifted_dir / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    _, shifted = _nowcast(tmp_path, synthetic_root_with_daily, shifted_dir, *argv)
    expected = _frame_gaussian(synthetic_root_with_daily, shifted_dir, "2011-06", "15")
    assert abs(shifted["mu"] - original["mu"]) > 1e-6
    assert shifted["mu"] == pytest.approx(expected["mu"], abs=1e-9)
    assert shifted["sigma"] == pytest.approx(expected["sigma"], abs=1e-9)


def test_a_past_target_month_is_read_at_plus_10_and_capped_at_its_last_day(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    rc, payload = _nowcast(
        tmp_path,
        synthetic_root_with_daily,
        saved_nowcast_model,
        *["--as-of", "2011-05-20T00:00:00Z", "--target-month", "2011-04"],
    )
    assert rc == 0 and payload["usable"] is True
    assert payload["target_month"] == "2011-04" and payload["checkpoint"] == "+10"
    assert payload["days_used"] == 30 and payload["last_era5_day"] == "2011-04-30"
    assert payload["seas_init"] == "2011-04" and payload["prev_label_month"] == "2011-03"


REACHED_10 = (
    "not usable: checkpoint 10 not reached, {} day(s) used; the network trained on 5 or more"
)


@pytest.mark.parametrize(
    "as_of, last_day, days, why",
    [
        ("2011-06-05T23:59:59Z", None, 0, "not usable: no day of 2011-06 is public yet"),
        ("2011-06-06T00:00:00Z", "2011-06-01", 1, REACHED_10.format(1)),
    ],
)
def test_no_day_is_visible_until_the_sixth_and_the_payload_says_so(
    as_of, last_day, days, why, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", as_of
    )
    assert rc == 3 and payload["usable"] is False
    assert payload["checkpoint"] == "5" and payload["gap_days"] == 0
    assert payload["last_era5_day"] == last_day and payload["days_used"] == days
    assert (payload["mu"] is None) == (days == 0)  # a finite mu from the first public day on
    assert capsys.readouterr().err.strip().splitlines()[-1] == why


@pytest.mark.parametrize(
    "as_of, checkpoint, days, rc",
    [
        ("2011-06-08T00:00:00Z", "5", 3, 3),
        ("2011-06-09T23:59:59Z", "5", 4, 3),
        ("2011-06-10T00:00:00Z", "10", 5, 0),
    ],
)
def test_a_nowcast_is_usable_only_from_checkpoint_10_and_says_why_before_it(
    as_of, checkpoint, days, rc, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    code, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", as_of
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload  # the payload is printed whatever the verdict
    assert code == rc and payload["usable"] is (rc == 0)
    assert (payload["checkpoint"], payload["days_used"], payload["gap_days"]) == (
        checkpoint,
        days,
        0,
    )
    assert np.isfinite(payload["mu"]) and payload["sigma"] > 0
    notes = captured.err.strip().splitlines()
    assert notes[1:] == ([REACHED_10.format(days)] if rc else [])


@pytest.mark.parametrize("as_of", ["2012-01-03T00:00:00Z", "2012-01-05T23:59:59Z"])
def test_the_month_after_the_series_is_not_usable_until_a_day_of_it_is_public(
    as_of, tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", as_of
    )
    assert rc == 3 and payload["target_month"] == "2012-01" and payload["days_used"] == 0
    assert payload["checkpoint"] == "5" and payload["last_era5_day"] is None
    assert payload["mu"] is None and payload["usable"] is False and payload["gap_days"] == 0


def test_the_target_month_defaults_to_the_month_of_the_as_of(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    _, default = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2011-06-20T23:59:59Z"
    )
    assert default["target_month"] == "2011-06"
    _, explicit = _nowcast(
        tmp_path,
        synthetic_root_with_daily,
        saved_nowcast_model,
        *["--as-of", "2011-06-20T23:59:59Z", "--target-month", "2011-06"],
    )
    assert default == explicit


def test_the_default_month_is_the_utc_month_of_an_as_of_in_another_zone(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    _, payload = _nowcast(
        tmp_path,
        synthetic_root_with_daily,
        saved_nowcast_model,
        "--as-of",
        "2011-07-01T01:00:00+02:00",
    )
    assert payload["as_of"] == "2011-06-30T23:00:00Z" and payload["target_month"] == "2011-06"
    assert payload["checkpoint"] == "25" and payload["days_used"] == 25


def test_the_as_of_may_carry_any_offset_and_is_reported_in_utc(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    _, payload = _nowcast(
        tmp_path,
        synthetic_root_with_daily,
        saved_nowcast_model,
        "--as-of",
        "2011-06-20T02:00:00+02:00",
    )
    assert payload["as_of"] == "2011-06-20T00:00:00Z" and payload["checkpoint"] == "20"


def test_the_payload_is_written_to_out_with_its_directories_and_matches_what_is_printed(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    out = tmp_path / "deep" / "er" / "payload.json"
    argv = ["--root", str(synthetic_root_with_daily), "--model-dir", str(saved_nowcast_model)]
    assert nowcast_main([*argv, "--as-of", "2011-06-20T00:00:00Z", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(printed)
    assert nowcast_main([*argv, "--as-of", "2011-06-20T00:00:00Z"]) == 0  # --out is optional
    assert capsys.readouterr().out == printed


def _publish(folder, root, model_dir) -> int:
    """The nowcast script's exit code with --out in the folder (payload.json)."""
    argv = ["--root", str(root), "--model-dir", str(model_dir), "--as-of", "2011-06-20T00:00:00Z"]
    return nowcast_main([*argv, "--out", str(folder / "payload.json")])


def test_the_payload_is_written_through_a_temporary_file_that_replaces_the_old_one(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, monkeypatch
):
    folder = tmp_path / "published"
    folder.mkdir()
    out = folder / "payload.json"
    out.write_text("old", encoding="utf-8")
    seen, real = [], os.replace

    def read(name):
        return Path(name).read_text(encoding="utf-8")

    def spy(source, target):
        seen.append((Path(source), Path(target), read(source), read(target)))
        real(source, target)

    monkeypatch.setattr(os, "replace", spy)
    assert _publish(folder, synthetic_root_with_daily, saved_nowcast_model) == 0
    ((source, target, staged, kept),) = seen
    assert (source, target) == (out.with_name("payload.json.tmp"), out)
    assert kept == "old" and json.loads(staged)["target_month"] == "2011-06"  # whole until the swap
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(staged)
    assert sorted(os.listdir(folder)) == ["payload.json"]  # no .tmp is left beside the payload


def test_a_failed_replace_keeps_the_old_payload_and_leaves_no_temporary_file(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, monkeypatch, capsys
):
    folder = tmp_path / "published"
    folder.mkdir()
    (folder / "payload.json").write_text("old", encoding="utf-8")

    def refuse(source, target):
        raise PermissionError("the payload is open elsewhere")

    monkeypatch.setattr(os, "replace", refuse)
    assert _publish(folder, synthetic_root_with_daily, saved_nowcast_model) == 2
    captured = capsys.readouterr()
    assert "cannot write" in captured.err and captured.out == ""
    assert (folder / "payload.json").read_text(encoding="utf-8") == "old"
    assert sorted(os.listdir(folder)) == ["payload.json"]


def test_an_out_that_is_a_directory_exits_2_and_leaves_no_temporary_file(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    folder = tmp_path / "published"
    (folder / "payload.json").mkdir(parents=True)
    assert _publish(folder, synthetic_root_with_daily, saved_nowcast_model) == 2
    assert "cannot write" in capsys.readouterr().err
    assert sorted(os.listdir(folder)) == ["payload.json"] and (folder / "payload.json").is_dir()


def _frozen_clock(moment: datetime):
    """A datetime class whose now() answers the moment and counts the reads."""

    class Clock(datetime):
        reads = 0

        @classmethod
        def now(cls, tz=None):
            Clock.reads += 1
            return moment

    return Clock


def _freeze_clock(monkeypatch, moment: datetime):
    """The frozen clock installed in every module that could read the time."""
    clock = _frozen_clock(moment)
    for module in (nn_cli, gtm_nowcast, gtm_nn_price):
        monkeypatch.setattr(module, "datetime", clock)
    return clock


def test_the_clock_is_read_once_and_only_without_an_as_of(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, monkeypatch
):
    clock = _freeze_clock(monkeypatch, datetime(2011, 6, 20, 12, 30, 45, 999999, tzinfo=UTC))
    _, payload = _nowcast(tmp_path, synthetic_root_with_daily, saved_nowcast_model)
    assert clock.reads == 1
    assert payload["as_of"] == "2011-06-20T12:30:45Z" and payload["target_month"] == "2011-06"
    assert payload["checkpoint"] == "20" and payload["days_used"] == 15
    _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2011-06-20T00:00:00Z"
    )
    assert clock.reads == 1  # an as-of given leaves the clock alone


@pytest.mark.parametrize(
    "as_of", ["2011-06-20", "2011-06-20T12:00:00", "yesterday", "", "2011-13-01T00:00:00Z"]
)
def test_an_as_of_that_is_not_a_time_with_an_offset_exits_2_and_writes_nothing(
    as_of, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", as_of
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == "" and "--as-of" in captured.err


@pytest.mark.parametrize("month", ["2011-13", "2011-6", "June", "2011-06-01", "0000-01"])
def test_a_target_month_that_is_not_a_month_exits_2(
    month, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    rc, payload = _nowcast(
        tmp_path,
        synthetic_root_with_daily,
        saved_nowcast_model,
        *["--as-of", "2011-06-20T00:00:00Z", "--target-month", month],
    )
    assert rc == 2 and payload is None and capsys.readouterr().out == ""


def test_a_model_directory_without_provenance_exits_2_and_says_so(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    bare = _copy_model(saved_nowcast_model, tmp_path, "bare")
    (bare / "provenance.json").unlink()
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, bare, "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert "provenance.json" in captured.err and len(captured.err.strip().splitlines()) == 1


def test_a_provenance_that_is_not_json_exits_2_and_names_the_file(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    broken = _copy_model(saved_nowcast_model, tmp_path, "broken")
    (broken / "provenance.json").write_text("{not json", encoding="utf-8")
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, broken, "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert "provenance.json" in captured.err and "Traceback" not in captured.err


@pytest.mark.parametrize("kind", ["seasonal", "", None, 7])
def test_a_model_directory_of_another_kind_exits_2_and_names_the_kind(
    kind, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    other = _copy_model(saved_nowcast_model, tmp_path, "other")
    provenance = _provenance(other) | {"kind": kind}
    (other / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, other, "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert "nowcast" in captured.err and len(captured.err.strip().splitlines()) == 1


def test_a_seasonal_models_directory_is_refused_by_the_nowcast_script(
    tmp_path, synthetic_root_with_daily, saved_models, capsys
):
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_models[1], "--as-of", "2011-06-20T00:00:00Z"
    )
    assert rc == 2 and payload is None and "seasonal" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["manifest.json", "weights"])
def test_a_model_directory_that_misses_its_files_exits_2_and_names_the_problem(
    missing, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    broken = _copy_model(saved_nowcast_model, tmp_path, "broken")
    target = broken / missing
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, broken, "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert f"cannot load the model in {broken}" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("damage", ["garbage", "width", "keys", "features", "json"])
def test_a_damaged_model_directory_exits_2_instead_of_a_traceback(
    damage, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    broken = _copy_model(saved_nowcast_model, tmp_path, "broken")
    manifest = json.loads((broken / "manifest.json").read_text(encoding="utf-8"))
    if damage == "garbage":
        (broken / "weights" / "seed_0.pt").write_bytes(b"not a checkpoint")
    else:
        manifest = {
            "width": manifest | {"hidden": 16},  # a width that the saved weights do not have
            "keys": {},
            "features": manifest | {"features": ["not_a_feature"]},
            "json": None,
        }[damage]
        text = "{" if manifest is None else json.dumps(manifest)
        (broken / "manifest.json").write_text(text, encoding="utf-8")
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, broken, "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert f"cannot load the model in {broken}" in captured.err
    assert "Traceback" not in captured.err


def test_a_model_directory_that_does_not_exist_exits_2(tmp_path, synthetic_root_with_daily, capsys):
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, tmp_path / "nowhere", "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and "nowhere" in captured.err


@pytest.mark.parametrize(
    "bridge",
    [None, "text", {"alpha": {}, "beta": None}, {"beta": 1.0}, {"alpha": {"1": "x"}, "beta": 1.0}],
)
def test_a_provenance_without_a_usable_bridge_exits_2(
    bridge, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    broken = _copy_model(saved_nowcast_model, tmp_path, "broken")
    provenance = _provenance(broken) | {"bridge": bridge}
    (broken / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, broken, "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert f"{broken / 'provenance.json'} " in captured.err and "bridge" in captured.err


def test_a_root_without_the_research_files_exits_2_and_names_the_daily_file(
    tmp_path, saved_nowcast_model, capsys
):
    rc, payload = _nowcast(
        tmp_path, tmp_path / "missing", saved_nowcast_model, "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert "era5_daily_global_mean.parquet" in captured.err
    assert "Traceback" not in captured.err


def test_a_seas5_file_without_its_columns_exits_2(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    path = synthetic_root_with_daily / "forecasts" / "seas5_global_mean.parquet"
    pd.DataFrame({"unrelated": [1.0]}).to_parquet(path, index=False)
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2011-06-20T00:00:00Z"
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and "cannot read the research inputs" in captured.err
    assert "Traceback" not in captured.err


def test_a_root_without_labels_exits_2(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    (synthetic_root_with_daily / "labels" / "labels_ledger.parquet").unlink()
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2011-06-20T00:00:00Z"
    )
    assert rc == 2 and payload is None and "labels" in capsys.readouterr().err


def test_the_labels_ledger_of_the_root_decides_when_a_label_is_public(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    path = synthetic_root_with_daily / "labels" / "labels_ledger.parquet"
    ledger = pd.read_parquet(path)
    may = ledger["period"] == "2011-05"  # a genuine first print, seen on the 14th
    ledger.loc[may, "vintage"] = VINTAGE_LIVE
    ledger.loc[may, "first_seen_utc"] = "2011-06-14T00:00:00+00:00"
    ledger.to_parquet(path, index=False)
    argv = ["--target-month", "2011-06", "--as-of"]
    _, before = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, *argv, "2011-06-13T23:59:59Z"
    )
    _, at = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, *argv, "2011-06-14T00:00:00Z"
    )
    assert before["prev_label_month"] == "2011-04" and at["prev_label_month"] == "2011-05"


@pytest.mark.parametrize("given", [None, "2011-06-14T00:00:00.900000Z"])
def test_the_decision_is_whole_seconds_so_the_reported_as_of_is_the_time_that_was_used(
    given, tmp_path, synthetic_root_with_daily, saved_nowcast_model, monkeypatch
):
    path = synthetic_root_with_daily / "labels" / "labels_ledger.parquet"
    ledger = pd.read_parquet(path)
    may = ledger["period"] == "2011-05"  # a first print seen half a second after the as-of's second
    ledger.loc[may, "vintage"] = VINTAGE_LIVE
    ledger.loc[may, "first_seen_utc"] = "2011-06-14T00:00:00.500000+00:00"
    ledger.to_parquet(path, index=False)
    _freeze_clock(monkeypatch, datetime(2011, 6, 14, 0, 0, 0, 900000, tzinfo=UTC))
    argv = ["--target-month", "2011-06"] + ([] if given is None else ["--as-of", given])
    _, payload = _nowcast(tmp_path, synthetic_root_with_daily, saved_nowcast_model, *argv)
    assert payload["as_of"] == "2011-06-14T00:00:00Z"
    assert payload["prev_label_month"] == "2011-04"  # the 14th at 00:00:00 precedes the print


def test_the_root_defaults_to_the_climate_data_root_variable(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    out = tmp_path / "p.json"  # synthetic_root_with_daily set the variable
    argv = ["--model-dir", str(saved_nowcast_model), "--as-of", "2011-06-20T00:00:00Z"]
    assert nowcast_main([*argv, "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["target_month"] == "2011-06"


def _daily_without(root, drop_month: str) -> None:
    path = root / "reference" / "era5_daily_global_mean.parquet"
    stored = pd.read_parquet(path)
    keep = pd.to_datetime(stored["date"]).dt.strftime("%Y-%m") != drop_month
    stored[keep].to_parquet(path, index=False)


@pytest.mark.parametrize("day", ["2011-07-01", "2011-07-31"])
def test_a_series_with_only_the_first_or_the_last_day_of_the_month_is_a_gap_not_a_usage_error(
    day, tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    path = synthetic_root_with_daily / "reference" / "era5_daily_global_mean.parquet"
    stored = pd.read_parquet(path)
    dates = pd.to_datetime(stored["date"])
    stored[(dates.dt.strftime("%Y-%m") != "2011-07") | (dates == day)].to_parquet(path, index=False)
    rc, payload = _nowcast(
        tmp_path,
        synthetic_root_with_daily,
        saved_nowcast_model,
        *["--as-of", "2011-08-20T00:00:00Z", "--target-month", "2011-07"],
    )
    assert rc == 3 and (payload["days_used"], payload["gap_days"]) == (1, 30)
    assert payload["usable"] is False and payload["checkpoint"] == "+10"


@pytest.mark.parametrize(
    "month, as_of, words",
    [
        ("2012-03", "2012-03-20T00:00:00Z", ["after the daily series", "2011-12-31"]),
        ("2012-01", "2012-01-06T00:00:00Z", ["after the daily series", "2011-12-31"]),
        ("1999-06", "2011-06-20T00:00:00Z", ["before the daily series", "2000-01-01"]),
        ("2011-07", "2011-08-20T00:00:00Z", ["no day of target month 2011-07"]),
    ],
)
def test_a_target_month_that_the_series_lacks_while_era5_has_published_it_exits_2(
    month, as_of, words, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    _daily_without(synthetic_root_with_daily, "2011-07")
    rc, payload = _nowcast(
        tmp_path,
        synthetic_root_with_daily,
        saved_nowcast_model,
        *["--as-of", as_of, "--target-month", month],
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert all(word in captured.err for word in [month, *words])
    assert len(captured.err.strip().splitlines()) == 1


@pytest.mark.parametrize(
    "month, as_of",
    [("2013-01", "2011-06-20T00:00:00Z"), ("2012-01", "2011-12-31T23:59:59Z")],
)
def test_a_month_that_has_not_started_exits_2_and_says_so(
    month, as_of, tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    rc, payload = _nowcast(
        tmp_path,
        synthetic_root_with_daily,
        saved_nowcast_model,
        *["--as-of", as_of, "--target-month", month],
    )
    captured = capsys.readouterr()
    assert rc == 2 and payload is None and captured.out == ""
    assert f"target month {month} has not started at {as_of}" in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def test_a_month_is_under_way_from_its_first_instant_even_before_any_day_is_public(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model
):
    rc, payload = _nowcast(
        tmp_path, synthetic_root_with_daily, saved_nowcast_model, "--as-of", "2012-01-01T00:00:00Z"
    )
    assert rc == 3 and payload["target_month"] == "2012-01" and payload["days_used"] == 0
    assert payload["last_era5_day"] is None and payload["mu"] is None


def test_the_nowcast_script_refuses_without_torch_before_reading_anything(
    tmp_path, no_torch, capsys
):
    rc = nowcast_main(
        [
            "--root",
            str(tmp_path / "x"),
            "--model-dir",
            str(tmp_path / "y"),
            "--out",
            str(tmp_path / "o"),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2 and captured.out == "" and captured.err.strip() == nn_cli.TORCH_MISSING
    assert not (tmp_path / "o").exists()


def test_the_nowcast_script_needs_its_model_directory(capsys):
    assert nowcast_main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--model-dir" in captured.err


def test_the_nowcast_script_prints_its_usage_for_help(capsys):
    assert nowcast_main(["--help"]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_an_unwritable_out_exits_2_instead_of_a_traceback(
    tmp_path, synthetic_root_with_daily, saved_nowcast_model, capsys
):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed", encoding="utf-8")
    argv = ["--root", str(synthetic_root_with_daily), "--model-dir", str(saved_nowcast_model)]
    rc = nowcast_main([*argv, "--as-of", "2011-06-20T00:00:00Z", "--out", str(blocker / "p.json")])
    captured = capsys.readouterr()
    assert rc == 2 and "cannot write" in captured.err and "Traceback" not in captured.err


# the pricing script


def _price(root, emos_json, nn_dir, *argv):
    return price_main(
        ["--emos", str(emos_json), "--nn-dir", str(nn_dir), "--root", str(root), *argv]
    )


def _gaussians(root, emos_json, nn_dir, target, init):
    """((mu, sigma) of EMOS, (mu, sigma) of the network) on the target month's row of the init."""
    seas5, era5_K, noaa_C = load_seas5(root), load_era5(root), load_noaa_gtm(root)
    frame = build_frame_from_inputs(seas5, era5_K, noaa_C)  # the bridge fitted on everything
    init_date = pd.Timestamp(f"{init}-01")
    row = frame[(frame["target_month"] == target) & (frame["init_date"] == init_date)]
    emos = EmosModel.from_json(emos_json).predict(row).iloc[0]
    bridge = Bridge.from_json(json.dumps(_provenance(nn_dir)["bridge"]))
    seasonal = seasonal_frame(seas5, era5_K, noaa_C, bridge=bridge)
    pick = seasonal[(seasonal["target_month"] == target) & (seasonal["init_month"] == init)]
    net = GtmNnModel.load(nn_dir).predict(pick).iloc[0]
    return (emos["mu"], emos["sigma"]), (net["mu"], net["sigma"])


def _cell(mu: float, sigma: float, strike: float) -> str:
    return (
        "-" if not (np.isfinite(mu) and np.isfinite(sigma)) else f"{p_yes(strike, mu, sigma):.4f}"
    )


def _table(strikes, emos, net) -> list[str]:
    rows = [f"| {s!r} | {_cell(*emos, s)} | {_cell(*net, s)} |" for s in strikes]
    return [PRICE_HEADER, PRICE_SEPARATOR, *rows]


def test_price_cli_prints_one_row_per_strike(tmp_path, synthetic_root, saved_models, capsys):
    pytest.importorskip("torch")
    emos_json, nn_dir = saved_models
    rc = price_main(
        [
            "--emos",
            str(emos_json),
            "--nn-dir",
            str(nn_dir),
            "--root",
            str(synthetic_root),
            "--target-month",
            "2012-03",
            "--as-of",
            "2012-01-20T00:00:00Z",
            "--strikes",
            "0.4",
            "0.5",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0 and out.count("\n| ") == 2
    assert all(0.0 <= float(v) <= 1.0 for v in re.findall(r"\| (0\.\d+) \|", out))


def test_the_table_is_each_models_gaussian_at_the_strikes_for_the_freshest_public_init(
    synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv, "--strikes", "0.4", "0.5", "1.0") == 0
    emos, net = _gaussians(synthetic_root, emos_json, nn_dir, "2012-03", "2012-01")  # lead 3
    captured = capsys.readouterr()
    assert captured.out.splitlines() == _table([0.4, 0.5, 1.0], emos, net)
    assert captured.out.endswith("\n") and "2012-01" in captured.err  # the init goes to stderr


def test_the_two_sides_use_their_own_bridges_the_emos_one_fitted_on_everything(
    synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 0
    printed = capsys.readouterr().out.splitlines()[2]
    seas5, era5_K, noaa_C = (
        load_seas5(synthetic_root),
        load_era5(synthetic_root),
        load_noaa_gtm(synthetic_root),
    )
    through = Bridge().fit(era5_K, noaa_C, end="2011-12")  # the network's bridge on the EMOS side
    wrong = build_frame_from_inputs(seas5, era5_K, noaa_C, bridge=through)
    row = wrong[
        (wrong["target_month"] == "2012-03") & (wrong["init_date"] == pd.Timestamp("2012-01-01"))
    ]
    mu, sigma = EmosModel.from_json(emos_json).predict(row).iloc[0]
    emos, net = _gaussians(synthetic_root, emos_json, nn_dir, "2012-03", "2012-01")
    assert printed == _table([0.4], emos, net)[2] and printed != _table([0.4], (mu, sigma), net)[2]


def test_the_network_side_follows_the_bridge_of_the_provenance_and_the_emos_side_does_not(
    tmp_path, synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    shifted_dir = _copy_model(nn_dir, tmp_path, "shifted")
    provenance = _provenance(shifted_dir)
    provenance["bridge"]["alpha"] = {k: v + 1.0 for k, v in provenance["bridge"]["alpha"].items()}
    (shifted_dir / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 0
    original = capsys.readouterr().out.splitlines()[2]
    assert _price(synthetic_root, emos_json, shifted_dir, *argv) == 0
    shifted = capsys.readouterr().out.splitlines()[2]
    emos, net = _gaussians(synthetic_root, emos_json, shifted_dir, "2012-03", "2012-01")
    assert shifted == _table([0.4], emos, net)[2]
    assert shifted.split(" | ")[1] == original.split(" | ")[1]  # the emos cell is the same
    assert shifted.split(" | ")[2] != original.split(" | ")[2]  # the network's moved with it


def test_a_strike_is_printed_as_a_float_and_may_be_negative_or_whole(
    synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv, "--strikes", "-0.5", "1", "0.45") == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" | ")[0] for line in lines[2:]] == ["| -0.5", "| 1.0", "| 0.45"]


@pytest.mark.parametrize(
    "as_of, init",
    [
        ("2012-01-13T11:59:59Z", "2011-12"),
        ("2012-01-13T12:00:00Z", "2012-01"),
        ("2011-10-13T12:00:00Z", "2011-10"),
        ("2012-03-31T00:00:00Z", "2012-03"),
        ("2013-01-01T00:00:00Z", "2012-03"),
    ],
)
def test_the_freshest_init_public_at_the_as_of_is_priced_and_one_public_at_it_counts(
    as_of, init, synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", as_of, "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 0
    emos, net = _gaussians(synthetic_root, emos_json, nn_dir, "2012-03", init)
    assert capsys.readouterr().out.splitlines() == _table([0.4], emos, net)


@pytest.mark.parametrize(
    "as_of", ["2011-10-13T11:59:59Z", "2011-09-01T00:00:00Z", "1999-01-01T00:00:00Z"]
)
def test_a_target_month_without_a_public_init_exits_2_with_a_message(
    as_of, synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", as_of, "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "2012-03" in captured.err and "init" in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def test_a_target_month_that_seas5_never_covers_exits_2(synthetic_root, saved_models, capsys):
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2031-03", "--as-of", "2030-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 2
    assert "2031-03" in capsys.readouterr().err


def test_a_nan_gaussian_on_the_emos_side_prints_a_dash_there_only(
    tmp_path, synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    partial = EmosModel.from_json(emos_json)
    del partial.params[3]  # the lead of the freshest init
    partial_json = tmp_path / "partial_emos.json"
    partial.to_json(partial_json)
    argv = [
        "--target-month",
        "2012-03",
        "--as-of",
        "2012-01-20T00:00:00Z",
        "--strikes",
        "0.4",
        "0.5",
    ]
    assert _price(synthetic_root, partial_json, nn_dir, *argv) == 0
    _, net = _gaussians(synthetic_root, emos_json, nn_dir, "2012-03", "2012-01")
    lines = capsys.readouterr().out.splitlines()
    assert lines == _table([0.4, 0.5], (float("nan"), float("nan")), net)
    assert all(line.split(" | ")[1] == "-" for line in lines[2:])


def test_a_nan_gaussian_on_the_network_side_prints_a_dash_there_only(
    synthetic_root, saved_models, capsys
):
    path = synthetic_root / "forecasts" / "seas5_global_mean.parquet"
    seas5 = pd.read_parquet(path)
    cell = (pd.to_datetime(seas5["init_date"]) == "2012-01-01") & (seas5["lead_month"] == 3)
    seas5[~(cell & (seas5["member"] >= 5))].to_parquet(path, index=False)  # 5 members: no quantiles
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 0
    emos, _ = _gaussians(synthetic_root, emos_json, nn_dir, "2012-03", "2012-01")
    lines = capsys.readouterr().out.splitlines()
    assert lines == _table([0.4], emos, (float("nan"), float("nan")))
    assert lines[2].endswith(" | - |") and np.isfinite(emos[0])


def test_an_infinite_sigma_prints_a_dash_instead_of_failing(
    tmp_path, synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    broken = EmosModel.from_json(emos_json)
    broken.params[3].c = float("inf")  # the lead of the freshest init
    broken_json = tmp_path / "inf_emos.json"
    broken.to_json(broken_json)
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, broken_json, nn_dir, *argv) == 0
    _, net = _gaussians(synthetic_root, emos_json, nn_dir, "2012-03", "2012-01")
    nothing = (float("nan"), float("nan"))
    assert capsys.readouterr().out.splitlines() == _table([0.4], nothing, net)


def test_the_context_line_goes_to_stderr_and_stdout_is_only_the_table(
    synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 3 and captured.err.count("\n") == 1
    assert "2012-03" in captured.err and "2012-01-20T00:00:00Z" in captured.err
    assert "init 2012-01 (lead 3)" in captured.err
    assert "nn trained on labels through 2011-12" in captured.err


def test_the_clock_is_read_once_by_the_price_script_and_only_without_an_as_of(
    synthetic_root, saved_models, capsys, monkeypatch
):
    emos_json, nn_dir = saved_models
    clock = _freeze_clock(monkeypatch, datetime(2012, 1, 20, 0, 0, 0, 999999, tzinfo=UTC))
    argv = ["--target-month", "2012-03", "--strikes", "0.4", "0.5"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 0
    now_out = capsys.readouterr().out
    assert clock.reads == 1
    assert _price(synthetic_root, emos_json, nn_dir, *argv, "--as-of", "2012-01-20T00:00:00Z") == 0
    assert clock.reads == 1 and capsys.readouterr().out == now_out


def test_the_price_script_refuses_a_nowcast_model_and_a_directory_without_provenance(
    tmp_path, synthetic_root, saved_models, saved_nowcast_model, capsys
):
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, saved_nowcast_model, *argv) == 2
    first = capsys.readouterr()
    assert first.out == "" and "seasonal" in first.err and "nowcast" in first.err
    bare = _copy_model(nn_dir, tmp_path, "bare")
    (bare / "provenance.json").unlink()
    assert _price(synthetic_root, emos_json, bare, *argv) == 2
    second = capsys.readouterr()
    assert second.out == "" and "provenance.json" in second.err
    assert _price(synthetic_root, emos_json, tmp_path / "nowhere", *argv) == 2
    assert "nowhere" in capsys.readouterr().err


def test_the_price_script_exits_2_for_an_emos_file_that_is_missing_or_not_a_model(
    tmp_path, synthetic_root, saved_models, capsys
):
    _, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, tmp_path / "gone.json", nn_dir, *argv) == 2
    assert "gone.json" in capsys.readouterr().err
    for text in ("not json", '{"leads": {"1": {"nope": 1}}}', "[]", "{}"):
        bad = tmp_path / "bad.json"
        bad.write_text(text, encoding="utf-8")
        assert _price(synthetic_root, bad, nn_dir, *argv) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and "bad.json" in captured.err
        assert "Traceback" not in captured.err


def test_the_price_script_exits_2_for_a_root_without_the_monthly_era5_product(
    tmp_path, synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    (synthetic_root / "reference" / "era5_global_mean.parquet").unlink()
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 2
    captured = capsys.readouterr()
    assert "era5_global_mean.parquet" in captured.err and captured.out == ""


def test_the_price_script_needs_no_daily_series_and_does_not_read_one(
    synthetic_root_with_daily, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    (synthetic_root_with_daily / "reference" / "era5_daily_global_mean.parquet").unlink()
    argv = ["--target-month", "2011-08", "--as-of", "2011-07-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root_with_daily, emos_json, nn_dir, *argv) == 0
    assert len(capsys.readouterr().out.splitlines()) == 3


def test_the_price_script_exits_2_for_a_seas5_file_without_its_columns(
    synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    path = synthetic_root / "forecasts" / "seas5_global_mean.parquet"
    pd.DataFrame({"unrelated": [1.0]}).to_parquet(path, index=False)
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 2
    captured = capsys.readouterr()
    assert "cannot read the research inputs" in captured.err and captured.out == ""


def test_the_price_script_exits_2_when_the_labels_cannot_fit_a_bridge(
    synthetic_root, saved_models, capsys
):
    path = synthetic_root / "labels" / "labels_ledger.parquet"
    pd.read_parquet(path).iloc[:5].to_parquet(path, index=False)  # five labels: no bridge
    emos_json, nn_dir = saved_models
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 2
    captured = capsys.readouterr()
    assert "overlapping months" in captured.err and captured.out == ""
    assert "Traceback" not in captured.err


def test_the_price_script_exits_2_without_labels(synthetic_root, saved_models, capsys):
    emos_json, nn_dir = saved_models
    (synthetic_root / "labels" / "labels_ledger.parquet").unlink()
    argv = ["--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z", "--strikes", "0.4"]
    assert _price(synthetic_root, emos_json, nn_dir, *argv) == 2
    assert "labels" in capsys.readouterr().err


def test_the_root_of_the_price_script_defaults_to_the_climate_data_root_variable(
    synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models  # synthetic_root set the variable
    argv = ["--emos", str(emos_json), "--nn-dir", str(nn_dir), "--strikes", "0.4"]
    assert price_main([*argv, "--target-month", "2012-03", "--as-of", "2012-01-20T00:00:00Z"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 3


@pytest.mark.parametrize(
    "options",
    [
        ["--target-month", "2012-03", "--strikes", "abc"],
        ["--target-month", "2012-03", "--strikes", "nan"],
        ["--target-month", "2012-03", "--strikes", "inf"],
        ["--target-month", "2012-03", "--strikes"],
        ["--target-month", "2012-03"],
        ["--strikes", "0.4"],
        ["--target-month", "2012-13", "--strikes", "0.4"],
        ["--target-month", "2012-03", "--strikes", "0.4", "--as-of", "2012-01-20"],
    ],
    ids=["text", "nan", "inf", "none", "no-strikes", "no-month", "bad-month", "naive-as-of"],
)
def test_bad_price_options_exit_2_and_print_nothing_on_stdout(
    options, synthetic_root, saved_models, capsys
):
    emos_json, nn_dir = saved_models
    assert _price(synthetic_root, emos_json, nn_dir, *options) == 2
    assert capsys.readouterr().out == ""


def test_the_price_script_refuses_without_torch_before_reading_anything(tmp_path, no_torch, capsys):
    argv = ["--emos", str(tmp_path / "e.json"), "--nn-dir", str(tmp_path / "n")]
    rc = price_main(
        [*argv, "--root", str(tmp_path / "r"), "--target-month", "2012-03", "--strikes", "0.4"]
    )
    captured = capsys.readouterr()
    assert rc == 2 and captured.out == "" and captured.err.strip() == nn_cli.TORCH_MISSING


def test_the_price_script_prints_its_usage_for_help(capsys):
    assert price_main(["--help"]) == 0
    assert "usage" in capsys.readouterr().out.lower()


# the helpers both scripts share

SHARED_HELPERS = (
    "parse_as_of",
    "resolve_as_of",
    "month_text",
    "read_provenance",
    "bridge_of",
    "training_note",
    "load_model",
)


def test_the_helpers_of_both_scripts_live_in_nn_cli_and_no_script_borrows_from_another():
    for name in SHARED_HELPERS:
        assert getattr(nn_cli, name).__module__ == nn_cli.__name__
    for module in (gtm_nowcast, gtm_nn_price):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert [name for name in SHARED_HELPERS if f"def {name}" in source] == []
    assert "gtm_nowcast" not in Path(gtm_nn_price.__file__).read_text(encoding="utf-8")


def test_parse_as_of_takes_z_and_any_offset_and_refuses_a_time_without_one():
    noon = datetime(2026, 10, 27, 12, tzinfo=UTC)
    assert nn_cli.parse_as_of("2026-10-27T12:00:00Z") == noon
    assert nn_cli.parse_as_of("2026-10-27T14:00:00+02:00") == noon
    for text in ("2026-10-27", "2026-10-27T12:00:00", "noon", ""):
        with pytest.raises(argparse.ArgumentTypeError, match="offset or Z"):
            nn_cli.parse_as_of(text)


def test_resolve_as_of_is_utc_in_whole_seconds_and_reads_the_clock_only_without_a_time(
    monkeypatch,
):
    clock = _freeze_clock(monkeypatch, datetime(2026, 10, 27, 12, 30, 45, 999999, tzinfo=UTC))
    zoned = datetime(2026, 10, 27, 14, 0, 0, 500000, tzinfo=timezone(timedelta(hours=2)))
    assert nn_cli.resolve_as_of(zoned) == datetime(2026, 10, 27, 12, tzinfo=UTC)
    assert nn_cli.resolve_as_of(zoned).utcoffset() == timedelta(0) and clock.reads == 0
    assert nn_cli.resolve_as_of(None) == datetime(2026, 10, 27, 12, 30, 45, tzinfo=UTC)
    assert nn_cli.resolve_as_of(None).utcoffset() == timedelta(0) and clock.reads == 2


def test_month_text_takes_a_month_and_refuses_anything_else():
    assert nn_cli.month_text("2026-10") == "2026-10"
    for text in ("2026-13", "2026-1", "2026-10-01", "October", ""):
        with pytest.raises(argparse.ArgumentTypeError, match="YYYY-MM"):
            nn_cli.month_text(text)


def test_training_note_names_the_labels_the_network_saw_and_admits_when_it_does_not_know():
    assert nn_cli.training_note({"labels_through": "2010-12"}) == (
        "trained on labels through 2010-12"
    )
    assert nn_cli.training_note({}) == "trained on labels through an unknown month"


# without torch


class _BlockTorch:
    """A meta-path finder that makes `import torch` fail as on a machine without it."""

    def find_spec(self, name, path=None, target=None):
        if name == "torch" or name.startswith("torch."):
            raise ModuleNotFoundError("torch is blocked", name=name)


@pytest.fixture
def no_torch(monkeypatch):
    """A machine without torch: the import machinery refuses it, whatever was imported before."""
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockTorch(), *sys.meta_path])


BLOCK_TORCH = """
import sys
sys.path.insert(0, sys.argv[1])


class BlockTorch:
    def find_spec(self, name, path=None, target=None):
        if name == "torch" or name.startswith("torch."):
            raise ModuleNotFoundError("torch is blocked", name=name)


sys.meta_path.insert(0, BlockTorch())
"""
CHECK_BLOCKED = (
    BLOCK_TORCH
    + """
import contextlib
import io
import runpy

import climate.nn.nowcast
import scripts.research.gtm_nn_price
import scripts.research.gtm_nowcast

for script in sys.argv[2:]:
    shown, code = io.StringIO(), None
    sys.argv = [script, "--help"]
    with contextlib.redirect_stdout(shown):
        try:
            runpy.run_path(script, run_name="__main__")
        except SystemExit as exc:
            code = exc.code
    assert code in (0, None) and "usage" in shown.getvalue().lower(), (script, code)
"""
)


def test_the_scripts_import_and_print_their_help_as_programs_without_torch():
    scripts = [str(SCRIPTS / "gtm_nowcast.py"), str(SCRIPTS / "gtm_nn_price.py")]
    done = subprocess.run(
        [sys.executable, "-c", CHECK_BLOCKED, str(REPO_ROOT), *scripts],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
