"""The replay's quote panel and its point-in-time forecaster."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from climate.dataset import build_frame_from_inputs
from climate.emos import EmosModel
from climate.paper import predictive_by_target_month
from climate.sim.forecast import PointInTimeForecaster, print_times
from climate.sim.panel import HOURLY, SWEEP, load_panel, load_sweeps
from climate.sim.replay import decision_times
from tests.test_climate import nn_fixtures
from tests.test_climate.sim_replay_fixtures import (
    DEC_EXPIRY,
    DEC_QUOTES,
    NO,
    YES,
    times,
    utc,
    write_ladder,
    write_sweep,
)


def _hourly(root, bar_start, con, bid, ask):
    path = root / "quotes_hourly" / f"date={bar_start:%Y-%m-%d}" / "h.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = pd.read_parquet(path).to_dict("records") if path.exists() else []
    rows.append({"ts_utc": bar_start.isoformat(), "con_id": con, "symbol": "GTM",
                 "expiry": DEC_EXPIRY, "strike": 1.3, "right": "C", "what_to_show": "BID_ASK",
                 "bar_size": "1 hour", "open": bid, "high": ask + 0.01, "low": bid - 0.01,
                 "close": ask, "volume": 0.0, "source": "reqHistoricalData"})
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_hourly_bars_fill_only_the_hours_without_a_sweep(tmp_path):
    root = tmp_path / "ticks"
    write_sweep(root, utc(2026, 10, 14, 10, 20), DEC_QUOTES, DEC_EXPIRY)
    _hourly(root, utc(2026, 10, 14, 9, 30), YES[1.3], 0.40, 0.43)   # ends 10:30, a sweep at 10:20
    _hourly(root, utc(2026, 10, 14, 12, 0), YES[1.3], 0.45, 0.48)   # ends 13:00, no sweep
    panel = load_panel(root)
    hourly = panel.frame[panel.frame["source"] == HOURLY]
    assert hourly["ts"].tolist() == [pd.Timestamp(utc(2026, 10, 14, 13, 0))]
    assert hourly[["bid", "ask"]].iloc[0].tolist() == [0.45, 0.48]
    assert set(panel.frame["source"]) == {SWEEP, HOURLY}
    assert load_panel(root, hourly=False).frame["source"].eq(SWEEP).all()


def test_sweeps_without_size_columns_still_load(tmp_path):
    root = tmp_path / "ticks"
    write_sweep(root, utc(2026, 8, 17, 12, 0), DEC_QUOTES, DEC_EXPIRY, with_sizes=False)
    frame = load_sweeps(root)
    assert len(frame) == 6 and frame["ask_size"].isna().all()


def test_minus_one_from_ib_is_missing(tmp_path):
    root = tmp_path / "ticks"
    write_sweep(root, utc(2026, 10, 14, 10, 0), {1.3: (-1.0, 0.44, 50)}, DEC_EXPIRY)
    q = load_panel(root).quote(pd.Timestamp(utc(2026, 10, 14, 10, 0)), YES[1.3])
    assert np.isnan(q[0]) and q[1] == 0.44


def test_lookups(tmp_path):
    root = tmp_path / "ticks"
    write_ladder(root, times(utc(2026, 10, 14, 10, 0), utc(2026, 10, 14, 11, 0)),
                 lambda ts: DEC_QUOTES, DEC_EXPIRY)
    panel = load_panel(root)
    assert panel.latest_at(utc(2026, 10, 14, 11, 30), timedelta(minutes=40))[0] == \
        pd.Timestamp(utc(2026, 10, 14, 11, 0))
    assert panel.latest_at(utc(2026, 10, 14, 12, 0), timedelta(minutes=40)) is None
    assert panel.latest_at(utc(2026, 10, 14, 9, 0), timedelta(hours=9)) is None
    assert panel.last_quote(YES[1.3])[:2] == (0.41, 0.44)
    assert panel.mid_at_or_after(utc(2026, 10, 14, 10, 5), YES[1.3], timedelta(hours=1)) == \
        pytest.approx(0.425)
    assert np.isnan(panel.mid_at_or_after(utc(2026, 10, 14, 12, 0), YES[1.3], timedelta(hours=1)))
    contracts = panel.contracts()
    assert contracts[YES[1.3]].partner == NO[1.3] and contracts[NO[1.3]].right == "P"


def test_decision_times(tmp_path):
    root = tmp_path / "ticks"
    write_ladder(root, times(utc(2026, 10, 12, 0, 0), utc(2026, 10, 14, 23, 40), minutes=60),
                 lambda ts: DEC_QUOTES, DEC_EXPIRY)
    panel = load_panel(root)
    daily = decision_times(panel, cadence="daily")
    assert [t.day for t in daily] == [12, 13, 14] and all(t.hour == 14 for t in daily)
    release = decision_times(panel, cadence="release")
    assert release == [pd.Timestamp(utc(2026, 10, 13, 14, 0))]
    assert len(decision_times(panel, cadence="sweep")) == len(panel.times)
    with pytest.raises(ValueError):
        decision_times(panel, cadence="hourly")


@pytest.fixture(scope="module")
def synthetic_frame():
    seas5, era5_K, noaa_C = nn_fixtures.synthetic_inputs()
    return build_frame_from_inputs(seas5, era5_K, noaa_C)


def test_the_forecast_uses_only_released_inits_and_printed_labels(synthetic_frame):
    printed = print_times(synthetic_frame["target_month"].astype(str), None)
    f = PointInTimeForecaster(synthetic_frame, printed)
    late = f.at(utc(2010, 6, 20, 12, 0))
    assert late.labels_through == "2010-05"  # printed on 2010-06-12
    assert late.inits["2010-08"] == "2010-06"
    early = f.at(utc(2010, 6, 13, 11, 0))  # the June init is out at 12:00
    assert early.inits["2010-08"] == "2010-05"

    work = synthetic_frame.copy()
    work.loc[work["target_month"].astype(str) > "2010-05", "y_C"] = np.nan
    manual = EmosModel(trend=True).fit(work)
    released = pd.to_datetime(synthetic_frame["init_date"]) <= pd.Timestamp("2010-06-01")
    expected = predictive_by_target_month(synthetic_frame[released], manual)
    assert late.predictive["2010-08"] == pytest.approx(expected["2010-08"])


def test_a_label_printed_later_cannot_move_the_forecast(synthetic_frame):
    printed = print_times(synthetic_frame["target_month"].astype(str), None)
    t = utc(2010, 6, 20, 12, 0)
    honest = PointInTimeForecaster(synthetic_frame, printed).at(t)
    poisoned = synthetic_frame.copy()
    poisoned.loc[poisoned["target_month"].astype(str) >= "2010-06", "y_C"] = 99.0
    leaky = PointInTimeForecaster(poisoned, printed).at(t)
    assert leaky.predictive["2010-09"] == pytest.approx(honest.predictive["2010-09"])


def test_a_fixed_model_is_flagged_before_its_fit(synthetic_frame):
    printed = print_times(synthetic_frame["target_month"].astype(str), None)
    model = EmosModel(trend=True).fit(synthetic_frame)
    unknown = PointInTimeForecaster(synthetic_frame, printed, model=model)
    assert unknown.at(utc(2010, 6, 20)).model_after_decision
    dated = PointInTimeForecaster(synthetic_frame, printed, model=model,
                                  model_fitted_at=utc(2010, 1, 1))
    assert not dated.at(utc(2010, 6, 20)).model_after_decision
    assert dated.at(utc(2009, 6, 20)).model_after_decision


def test_no_released_init_or_no_printed_label_means_no_forecast(synthetic_frame):
    printed = print_times(synthetic_frame["target_month"].astype(str), None)
    t = utc(1996, 1, 13, 11, 0)  # before the first init's release and the first target's print
    pit = PointInTimeForecaster(synthetic_frame, printed).at(t)
    assert pit.predictive == {} and "no label printed" in pit.notes[0]
    model = EmosModel(trend=True).fit(synthetic_frame)
    fixed = PointInTimeForecaster(synthetic_frame, printed, model=model).at(t)
    assert fixed.predictive == {} and "no SEAS5 init" in fixed.notes[0]
    with pytest.raises(ValueError, match="timezone"):
        PointInTimeForecaster(synthetic_frame, printed).at(pd.Timestamp("2010-06-20"))
