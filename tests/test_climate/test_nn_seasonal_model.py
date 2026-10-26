"""The seasonal GTM network behind the EMOS-shaped fit and predict interface."""

from __future__ import annotations

import hashlib
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
from climate.nn import seasonal_model  # noqa: E402
from climate.nn.baselines import raw_ensemble  # noqa: E402
from climate.nn.features import SEASONAL_FEATURES, seasonal_frame  # noqa: E402
from climate.nn.seasonal_model import SEEDS, VALIDATION_YEARS, GtmNnModel  # noqa: E402
from climate.nn.torch_model import collapse_seeds, predict_gaussian  # noqa: E402
from tests.test_climate import nn_fixtures  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _strict_frame(inputs, through):
    """The seasonal frame with the bridge fitted on the labels through December of `through`."""
    seas5, era5_K, noaa_C = inputs
    bridge = Bridge().fit(era5_K, noaa_C, end=f"{through}-12")
    return seasonal_frame(seas5, era5_K, noaa_C, bridge=bridge)


def _same_weights(a, b) -> bool:
    return all(torch.equal(p, q) for p, q in zip(a.state_dict().values(), b.state_dict().values()))


@pytest.fixture(scope="module")
def fitted():
    """(frame, model): two seeds fitted on the target years through 2012; tests only read them."""
    frame = _strict_frame(nn_fixtures.synthetic_inputs(), 2012)
    return frame, GtmNnModel(seeds=(0, 1)).fit(frame[target_year(frame) <= 2012])


def test_predict_returns_emos_shaped_columns_aligned_to_the_frame(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    model = GtmNnModel(seeds=(0, 1)).fit(frame[target_year(frame) <= 2012])
    pred = model.predict(frame)
    assert list(pred.columns) == ["mu", "sigma"] and pred.index.equals(frame.index)
    assert (pred["sigma"].dropna() > 0).all()


def test_fit_uses_the_three_most_recent_target_years_for_validation(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    model = GtmNnModel(seeds=(0,)).fit(frame[target_year(frame) <= 2012])
    assert model.validation_years == [2010, 2011, 2012]


def test_rows_with_a_missing_feature_predict_nan(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    model = GtmNnModel(seeds=(0,)).fit(frame[target_year(frame) <= 2012])
    probe = frame.iloc[:2].copy()
    probe.loc[probe.index[0], "q50_C"] = np.nan
    pred = model.predict(probe)
    assert np.isnan(pred["mu"].iloc[0]) and np.isfinite(pred["mu"].iloc[1])


def test_save_and_load_give_identical_predictions(tmp_path, synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    model = GtmNnModel(seeds=(0, 1)).fit(frame[target_year(frame) <= 2012])
    model.save(tmp_path / "m")
    again = GtmNnModel.load(tmp_path / "m")
    np.testing.assert_allclose(again.predict(frame)["mu"], model.predict(frame)["mu"])
    assert again.fingerprint() == model.fingerprint() and len(model.fingerprint()) == 16


def test_the_network_beats_the_raw_ensemble_on_the_synthetic_problem(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2010)
    train, test = frame[target_year(frame) <= 2010], frame[target_year(frame) > 2010]
    model = GtmNnModel(seeds=(0, 1, 2)).fit(train)
    assert model.crps(test).mean() < crps_gaussian(test["y_C"], *raw_ensemble(test)).mean()


def test_the_defaults_are_the_protocol_constants():
    model = GtmNnModel()
    assert (SEEDS, VALIDATION_YEARS) == ((0, 1, 2, 3, 4), 3)
    assert (model.seeds, model.hidden, model.device) == (SEEDS, 32, "cpu")
    with pytest.raises(ValueError, match="seed"):
        GtmNnModel(seeds=())


def test_fit_returns_the_model_with_one_history_per_seed_and_the_hidden_width(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    model = GtmNnModel(seeds=(3, 5), hidden=8)
    assert model.fit(frame[target_year(frame) <= 2012]) is model
    assert sorted(model.history) == [3, 5]
    keys = {"best_epoch", "best_val_crps", "train_crps"}
    assert all(set(history) == keys for history in model.history.values())
    first_layer = model.nets[3].state_dict()["layers.0.weight"]
    assert tuple(first_layer.shape) == (8, len(SEASONAL_FEATURES))


def test_the_validation_years_come_from_the_rows_used_for_fitting(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    rows = frame[target_year(frame) <= 2012].copy()
    rows.loc[target_year(rows) == 2012, "y_C"] = np.nan  # a year without labels cannot validate
    model = GtmNnModel(seeds=(0,)).fit(rows)
    assert model.validation_years == [2009, 2010, 2011]
    assert all(type(year) is int for year in model.validation_years)


def test_fit_ignores_rows_with_a_missing_label_or_a_non_finite_feature(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    rows = frame[target_year(frame) <= 2012].copy()
    rows.loc[rows.index[:5], "y_C"] = np.nan
    rows.loc[rows.index[5:10], "q10_C"] = np.nan
    rows.loc[rows.index[10:15], "fc_sd_C"] = np.inf
    model = GtmNnModel(seeds=(0,)).fit(rows)  # the torch core raises on one non-finite value
    kept = rows.iloc[15:]
    train = kept[target_year(kept) <= 2009]
    np.testing.assert_allclose(model.standardiser.mean, train[SEASONAL_FEATURES].mean())
    assert model.validation_years == [2010, 2011, 2012]


def test_the_standardiser_is_fitted_on_the_training_rows_only(fitted):
    frame, model = fitted
    train = frame[target_year(frame) <= 2009][SEASONAL_FEATURES].to_numpy(dtype=float)
    np.testing.assert_allclose(model.standardiser.mean, train.mean(axis=0))
    np.testing.assert_allclose(model.standardiser.sd, train.std(axis=0))


def test_fit_needs_four_distinct_target_years_among_the_usable_rows(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    through_1999 = frame[target_year(frame) <= 1999].copy()
    model = GtmNnModel(seeds=(0,)).fit(through_1999)  # one year to train on, three to validate
    assert model.validation_years == [1997, 1998, 1999]
    with pytest.raises(ValueError, match="got 3"):
        GtmNnModel(seeds=(0,)).fit(frame[target_year(frame) <= 1998])
    through_1999.loc[target_year(through_1999) == 1999, "y_C"] = np.nan
    with pytest.raises(ValueError, match="got 3"):
        GtmNnModel(seeds=(0,)).fit(through_1999)


def test_each_seed_trains_its_own_network_and_a_refit_reproduces_it(fitted):
    frame, model = fitted
    again = GtmNnModel(seeds=(0, 1)).fit(frame[target_year(frame) <= 2012])
    assert _same_weights(model.nets[0], again.nets[0])
    assert _same_weights(model.nets[1], again.nets[1])
    assert not _same_weights(model.nets[0], model.nets[1])


def test_predict_collapses_the_seeds_over_the_standardised_features(fitted):
    frame, model = fitted
    X = model.standardiser.transform(frame[SEASONAL_FEATURES].to_numpy(dtype=float))
    per_seed = [predict_gaussian(model.nets[seed], X, "cpu") for seed in (0, 1)]
    mu, sigma = collapse_seeds([m for m, _ in per_seed], [s for _, s in per_seed])
    pred = model.predict(frame)
    np.testing.assert_allclose(pred["mu"], mu)
    np.testing.assert_allclose(pred["sigma"], sigma)
    assert not np.allclose(per_seed[0][0], per_seed[1][0])  # the seeds differ, so the collapse acts


def test_a_non_finite_feature_gives_nan_for_that_row_only_whatever_the_index(fitted):
    frame, model = fitted
    probe = frame.iloc[:4].iloc[::-1].copy()  # index 3, 2, 1, 0
    probe.loc[2, "q50_C"] = np.nan
    probe.loc[1, "fc_sd_C"] = np.inf
    pred = model.predict(probe)
    assert pred.index.tolist() == [3, 2, 1, 0] and list(pred.columns) == ["mu", "sigma"]
    expected_nan = [[False, False], [True, True], [True, True], [False, False]]
    assert pred.isna().to_numpy().tolist() == expected_nan
    full = model.predict(frame)
    np.testing.assert_allclose(pred.loc[[3, 0]], full.loc[[3, 0]], atol=1e-5)


def test_crps_scores_the_predictions_and_is_nan_without_a_prediction_or_a_label(fitted):
    frame, model = fitted
    probe = frame.iloc[:3].copy()
    probe.loc[0, "q50_C"] = np.nan  # no prediction
    probe.loc[1, "y_C"] = np.nan  # no label
    crps, pred = model.crps(probe), model.predict(probe)
    assert isinstance(crps, np.ndarray) and crps.shape == (3,)
    assert np.isnan(crps[:2]).all() and np.isfinite(crps[2])
    expected = crps_gaussian(probe["y_C"].iloc[2], pred["mu"].iloc[2], pred["sigma"].iloc[2])
    np.testing.assert_allclose(crps[2], expected)


def test_save_writes_a_manifest_and_one_state_dict_per_seed(tmp_path, fitted):
    _, model = fitted
    model.save(tmp_path / "m")
    manifest = json.loads((tmp_path / "m" / "manifest.json").read_text(encoding="utf-8"))
    keys = {"features", "standardiser", "seeds", "hidden", "validation_years", "history"}
    assert set(manifest) == keys
    assert manifest["features"] == SEASONAL_FEATURES and manifest["seeds"] == [0, 1]
    assert manifest["hidden"] == 32 and manifest["validation_years"] == [2010, 2011, 2012]
    assert manifest["standardiser"] == model.standardiser.to_dict()
    assert manifest["history"]["1"] == model.history[1]
    weights = tmp_path / "m" / "weights"
    assert sorted(p.name for p in weights.iterdir()) == ["seed_0.pt", "seed_1.pt"]
    state = torch.load(weights / "seed_1.pt", map_location="cpu", weights_only=True)
    assert tuple(state["layers.0.weight"].shape) == (32, len(SEASONAL_FEATURES))


def test_a_loaded_model_has_the_setup_and_history_of_the_saved_one(tmp_path, fitted):
    frame, model = fitted
    model.save(tmp_path / "m")
    again = GtmNnModel.load(tmp_path / "m")
    assert (again.seeds, again.hidden, again.features) == ((0, 1), 32, SEASONAL_FEATURES)
    assert again.validation_years == [2010, 2011, 2012] and again.history == model.history
    np.testing.assert_allclose(again.predict(frame), model.predict(frame))


def test_a_loaded_model_keeps_the_features_it_was_saved_with(tmp_path, fitted, monkeypatch):
    frame, model = fitted
    model.save(tmp_path / "m")
    monkeypatch.setattr(seasonal_model, "SEASONAL_FEATURES", SEASONAL_FEATURES[:7])
    again = GtmNnModel.load(tmp_path / "m")
    assert again.features == SEASONAL_FEATURES
    np.testing.assert_allclose(again.predict(frame), model.predict(frame))


def test_the_fingerprint_is_the_sha256_of_the_manifest_without_the_history(tmp_path, fitted):
    _, model = fitted
    model.save(tmp_path / "m")
    manifest = json.loads((tmp_path / "m" / "manifest.json").read_text(encoding="utf-8"))
    del manifest["history"]
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()
    assert model.fingerprint() == digest[:16]
    again = GtmNnModel.load(tmp_path / "m")
    again.history[0]["best_epoch"] += 1
    assert again.fingerprint() == model.fingerprint()


def test_the_fingerprint_changes_with_the_seeds_and_with_the_training_window(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    through_2012 = frame[target_year(frame) <= 2012]
    through_2011 = frame[target_year(frame) <= 2011]
    base = GtmNnModel(seeds=(0,)).fit(through_2012).fingerprint()
    other_seed = GtmNnModel(seeds=(1,)).fit(through_2012).fingerprint()
    shorter = GtmNnModel(seeds=(0,)).fit(through_2011).fingerprint()
    assert len({base, other_seed, shorter}) == 3


def test_an_unfitted_model_refuses_to_predict_save_or_fingerprint(tmp_path):
    model = GtmNnModel()
    calls = [
        lambda: model.predict(pd.DataFrame()),
        lambda: model.save(tmp_path / "m"),
        model.fingerprint,
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="not fitted"):
            call()
    assert not (tmp_path / "m").exists()


def test_a_feature_constant_in_training_does_not_blow_up_a_later_row(synthetic_inputs):
    frame = _strict_frame(synthetic_inputs, 2012)
    rows = frame[target_year(frame) <= 2012]
    model = GtmNnModel(seeds=(0,)).fit(rows[rows["lead_month"] <= 5])  # lead_6 is 0 in every row
    late = frame[(frame["lead_month"] == 6) & (target_year(frame) == 2013)]
    assert len(late) > 0 and (model.predict(late)["mu"].abs() < 5).all()


BLOCK_TORCH = """
import sys

sys.path.insert(0, sys.argv[1])


class BlockTorch:
    def find_spec(self, name, path=None, target=None):
        if name == "torch" or name.startswith("torch."):
            raise ModuleNotFoundError("torch is blocked", name=name)


sys.meta_path.insert(0, BlockTorch())
import climate.nn.seasonal_model
"""


def test_the_module_imports_without_torch():
    done = subprocess.run(
        [sys.executable, "-c", BLOCK_TORCH, str(REPO_ROOT)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
