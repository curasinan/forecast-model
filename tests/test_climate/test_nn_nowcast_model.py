"""The nowcast network: the seasonal network's machinery on the month-to-date features."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from climate.bridge import Bridge  # noqa: E402
from climate.emos import crps_gaussian, target_year  # noqa: E402
from climate.nn.features import SEASONAL_FEATURES, seasonal_frame  # noqa: E402
from climate.nn.nowcast import NOWCAST_FEATURES, nowcast_frame  # noqa: E402
from climate.nn.nowcast_model import GtmNowcastModel  # noqa: E402
from climate.nn.seasonal_model import SEEDS, GtmNnModel  # noqa: E402
from tests.test_climate import nn_fixtures  # noqa: E402
from tests.test_climate.conftest import BLOCK_TORCH  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
LATE = ["20", "25", "+1", "+3"]


def _frames(through: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(seasonal, nowcast) frames with the bridge fitted on the labels through `through`-12."""
    daily_K, noaa_C, seas5, ledger = nn_fixtures.synthetic_nowcast()
    monthly = daily_K.groupby(daily_K.index.to_period("M")).mean()
    monthly.index = monthly.index.astype(str)
    bridge = Bridge().fit(monthly, noaa_C, end=f"{through}-12")
    seasonal = seasonal_frame(seas5, monthly, noaa_C, bridge=bridge)
    return seasonal, nowcast_frame(daily_K, noaa_C, seasonal, bridge=bridge, ledger=ledger)


def _train(frame: pd.DataFrame, last_year: int) -> pd.DataFrame:
    """The labelled rows through the target year `last_year`; the model drops the gap rows."""
    return frame[(target_year(frame) <= last_year) & np.isfinite(frame["y_C"])]


def _same_weights(a, b) -> bool:
    return all(torch.equal(p, q) for p, q in zip(a.state_dict().values(), b.state_dict().values()))


@pytest.fixture(scope="module")
def frame():
    """The nowcast frame with the bridge fitted through 2009; the tests only read it."""
    return _frames(2009)[1]


@pytest.fixture(scope="module")
def seasonal_fitted():
    """A seasonal model of the same setup as `fitted` (one seed, width 8) on the seasonal frame."""
    rows = _frames(2009)[0]
    return GtmNnModel(seeds=(0,), hidden=8).fit(
        rows[(target_year(rows) <= 2009) & np.isfinite(rows["y_C"])]
    )


@pytest.fixture(scope="module")
def fitted(frame):
    """Two seeds of width 16 fitted on the target years through 2009."""
    return GtmNowcastModel(seeds=(0, 1), hidden=16).fit(_train(frame, 2009))


def test_the_model_has_the_seasonal_models_methods_attributes_and_defaults_with_its_own_features():
    model = GtmNowcastModel()
    for name in ("fit", "predict", "crps", "save", "load", "fingerprint"):
        assert callable(getattr(model, name))
    for name in ("seeds", "hidden", "device", "features", "standardiser", "nets", "history"):
        assert hasattr(model, name)
    assert model.validation_years == []
    assert (model.seeds, model.hidden, model.device) == (SEEDS, 32, "cpu")
    assert model.features == NOWCAST_FEATURES != SEASONAL_FEATURES
    assert GtmNnModel().features == SEASONAL_FEATURES  # the seasonal model is untouched
    with pytest.raises(ValueError, match="seed"):
        GtmNowcastModel(seeds=())


def test_predict_returns_emos_shaped_columns_aligned_to_the_frame(frame, fitted):
    pred = fitted.predict(frame)
    assert list(pred.columns) == ["mu", "sigma"] and pred.index.equals(frame.index)
    assert (pred["sigma"].dropna() > 0).all()


def test_fit_returns_the_model_with_one_history_per_seed_and_the_hidden_width(frame):
    model = GtmNowcastModel(seeds=(3, 5), hidden=8)
    assert model.fit(_train(frame, 2009)) is model
    assert sorted(model.history) == [3, 5]
    first_layer = model.nets[3].state_dict()["layers.0.weight"]
    assert tuple(first_layer.shape) == (8, len(NOWCAST_FEATURES))


def test_the_three_most_recent_usable_target_years_validate(fitted):
    assert fitted.validation_years == [2007, 2008, 2009]


def test_a_row_with_a_gap_day_or_a_missing_feature_predicts_nan(frame, fitted):
    probe = frame[(target_year(frame) == 2010) & (frame["checkpoint"] == "20")].iloc[:4].copy()
    probe.loc[probe.index[0], "gap_days"] = 1
    probe.loc[probe.index[1], "prev_label_C"] = np.nan
    probe.loc[probe.index[2], "mtd_mean_C"] = np.inf
    pred = fitted.predict(probe)
    assert pred.isna().to_numpy().tolist() == [[True, True]] * 3 + [[False, False]]
    clean = fitted.predict(frame.loc[probe.index[3:]])
    np.testing.assert_allclose(pred.iloc[3:], clean, rtol=0, atol=1e-6)


def test_checkpoint_five_predicts_nan_because_no_era5_day_is_visible_yet(frame, fitted):
    pred = fitted.predict(frame[frame["checkpoint"] == "5"])
    assert pred.isna().all().all()
    assert fitted.predict(frame[frame["checkpoint"] == "10"]).iloc[24:]["mu"].notna().all()


def test_rows_with_a_gap_day_are_left_out_of_the_fit(frame):
    rows = _train(frame, 2009).copy()
    gap = (rows["checkpoint"] == "20") & (rows["month_of_year"] == 3)
    rows.loc[gap, "gap_days"] = 2
    rows.loc[gap, "mtd_mean_C"] = 1000.0  # would move the mean a lot if it were used
    model = GtmNowcastModel(seeds=(0,), hidden=8).fit(rows)
    kept = rows[rows["gap_days"] == 0]
    kept = kept[np.isfinite(kept[NOWCAST_FEATURES].to_numpy(dtype=float)).all(axis=1)]
    train = kept[target_year(kept) <= 2006]  # 2007 to 2009 validate
    np.testing.assert_allclose(model.standardiser.mean, train[NOWCAST_FEATURES].mean())
    assert model.validation_years == [2007, 2008, 2009]


def test_fit_ignores_rows_with_a_missing_label_or_a_non_finite_feature(frame):
    rows = _train(frame, 2009).copy()
    rows.loc[rows.index[:5], "y_C"] = np.nan
    rows.loc[rows.index[5:10], "seas_mu"] = np.nan
    rows.loc[rows.index[10:15], "frac_days_used"] = np.inf
    model = GtmNowcastModel(seeds=(0,), hidden=8).fit(rows)  # the torch core raises on one of them
    assert model.validation_years == [2007, 2008, 2009]


@pytest.mark.parametrize("column", [*NOWCAST_FEATURES, "y_C"])
def test_a_missing_value_in_any_feature_or_the_label_keeps_its_row_out_of_the_fit(column, frame):
    rows = _train(frame, 2009).reset_index(drop=True)
    row = rows.index[(rows["checkpoint"] == "15") & (rows["target_month"] == "2005-06")][0]
    holed = rows.copy()
    holed.loc[row, column] = np.nan
    full = nn_fixtures.rows_a_fit_uses(GtmNowcastModel(seeds=(0,)), rows)
    assert nn_fixtures.rows_a_fit_uses(GtmNowcastModel(seeds=(0,)), holed) == full - 1


def test_fit_needs_four_distinct_target_years_among_the_usable_rows(frame):
    model = GtmNowcastModel(seeds=(0,), hidden=8).fit(
        _train(frame, 2003)
    )  # 1 to train, 3 to validate
    assert model.validation_years == [2001, 2002, 2003]
    with pytest.raises(ValueError, match="no gap day.*got 3"):
        GtmNowcastModel(seeds=(0,), hidden=8).fit(_train(frame, 2002))
    rows = _train(frame, 2003).copy()
    rows.loc[target_year(rows) == 2003, "gap_days"] = 1  # a year of gap rows cannot count
    with pytest.raises(ValueError, match="got 3"):
        GtmNowcastModel(seeds=(0,), hidden=8).fit(rows)


def test_each_seed_trains_its_own_network_and_a_refit_reproduces_it(frame, fitted):
    again = GtmNowcastModel(seeds=(0, 1), hidden=16).fit(_train(frame, 2009))
    assert _same_weights(fitted.nets[0], again.nets[0])
    assert _same_weights(fitted.nets[1], again.nets[1])
    assert not _same_weights(fitted.nets[0], fitted.nets[1])


def test_crps_scores_the_predictions_and_is_nan_without_a_prediction_or_a_label(frame, fitted):
    probe = frame[(target_year(frame) == 2010) & (frame["checkpoint"] == "25")].iloc[:3].copy()
    probe.loc[probe.index[0], "gap_days"] = 1  # no prediction
    probe.loc[probe.index[1], "y_C"] = np.nan  # no label
    crps, pred = fitted.crps(probe), fitted.predict(probe)
    assert crps.shape == (3,) and np.isnan(crps[:2]).all() and np.isfinite(crps[2])
    expected = crps_gaussian(probe["y_C"].iloc[2], pred["mu"].iloc[2], pred["sigma"].iloc[2])
    np.testing.assert_allclose(crps[2], expected)


def test_save_and_load_give_identical_predictions_and_keep_the_gap_rule(tmp_path, frame, fitted):
    fitted.save(tmp_path / "m")
    again = GtmNowcastModel.load(tmp_path / "m")
    assert isinstance(again, GtmNowcastModel) and again.features == NOWCAST_FEATURES
    np.testing.assert_allclose(again.predict(frame), fitted.predict(frame), equal_nan=True)
    assert again.fingerprint() == fitted.fingerprint()
    probe = frame[(target_year(frame) == 2010) & (frame["checkpoint"] == "20")].iloc[:2].copy()
    probe.loc[probe.index[0], "gap_days"] = 1
    assert again.predict(probe)["mu"].isna().tolist() == [True, False]


def test_save_writes_the_manifest_and_one_state_dict_per_seed_under_the_features_of_the_model(
    tmp_path, fitted
):
    fitted.save(tmp_path / "m")
    manifest = json.loads((tmp_path / "m" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["features"] == NOWCAST_FEATURES and manifest["seeds"] == [0, 1]
    assert manifest["hidden"] == 16 and manifest["validation_years"] == [2007, 2008, 2009]
    assert sorted(p.name for p in (tmp_path / "m" / "weights").iterdir()) == [
        "seed_0.pt",
        "seed_1.pt",
    ]
    saved = tmp_path / "m" / "weights" / "seed_1.pt"
    state = torch.load(saved, map_location="cpu", weights_only=True)
    assert tuple(state["layers.0.weight"].shape) == (16, len(NOWCAST_FEATURES))


def test_the_fingerprint_changes_with_the_seeds_and_the_training_window(frame):
    through_2009 = _train(frame, 2009)
    base = GtmNowcastModel(seeds=(0,), hidden=8).fit(through_2009).fingerprint()
    shorter = GtmNowcastModel(seeds=(0,), hidden=8).fit(_train(frame, 2008)).fingerprint()
    other_seed = GtmNowcastModel(seeds=(1,), hidden=8).fit(through_2009).fingerprint()
    assert len(base) == 16 and len({base, shorter, other_seed}) == 3


def test_the_fingerprint_differs_from_a_seasonal_models_with_the_same_setup(frame):
    seasonal_rows, _ = _frames(2009)
    seasonal = GtmNnModel(seeds=(0,), hidden=8).fit(
        seasonal_rows[(target_year(seasonal_rows) <= 2009) & np.isfinite(seasonal_rows["y_C"])]
    )
    nowcast = GtmNowcastModel(seeds=(0,), hidden=8).fit(_train(frame, 2009))
    assert seasonal.validation_years == nowcast.validation_years  # the setup is the same
    assert (seasonal.seeds, seasonal.hidden) == (nowcast.seeds, nowcast.hidden)
    assert seasonal.fingerprint() != nowcast.fingerprint()


def test_a_nowcast_model_refuses_the_weights_of_a_seasonal_model_at_load(tmp_path, seasonal_fitted):
    seasonal_fitted.save(tmp_path / "m")
    with pytest.raises(ValueError, match=r"GtmNowcastModel.*mtd_mean_C.*fc_mean_C"):
        GtmNowcastModel.load(tmp_path / "m")


def test_a_seasonal_model_refuses_the_weights_of_a_nowcast_model_at_load(tmp_path, fitted):
    fitted.save(tmp_path / "m")
    with pytest.raises(ValueError, match=r"GtmNnModel.*fc_mean_C.*mtd_mean_C"):
        GtmNnModel.load(tmp_path / "m")


def test_each_class_loads_its_own_weights(tmp_path, fitted, seasonal_fitted):
    fitted.save(tmp_path / "nowcast")
    seasonal_fitted.save(tmp_path / "seasonal")
    assert GtmNowcastModel.load(tmp_path / "nowcast").features == NOWCAST_FEATURES
    assert GtmNnModel.load(tmp_path / "seasonal").features == SEASONAL_FEATURES


def test_an_unfitted_model_refuses_to_predict_save_or_fingerprint(tmp_path):
    model = GtmNowcastModel()
    for call in (lambda: model.predict(pd.DataFrame()), lambda: model.save(tmp_path / "m")):
        with pytest.raises(RuntimeError, match="not fitted"):
            call()
    with pytest.raises(RuntimeError, match="not fitted"):
        model.fingerprint()
    assert not (tmp_path / "m").exists()


def test_the_network_beats_the_seasonal_forecast_alone_late_in_the_month(frame):
    train = _train(frame, 2008)
    model = GtmNowcastModel(seeds=(0, 1, 2), hidden=16).fit(train)
    test = frame[(target_year(frame) == 2010) & frame["checkpoint"].isin(LATE)]
    seasonal_only = crps_gaussian(test["y_C"], test["seas_mu"], test["seas_sigma"]).mean()
    assert model.crps(test).mean() < 0.7 * seasonal_only


def test_the_module_imports_without_torch():
    code = BLOCK_TORCH + "import climate.nn.nowcast_model\n"
    done = subprocess.run(
        [sys.executable, "-c", code, str(REPO_ROOT)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
