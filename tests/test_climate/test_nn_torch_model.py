"""The torch core of the GTM research models: device, CRPS loss, a small Gaussian network."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from climate.emos import crps_gaussian  # noqa: E402
from climate.nn import torch_model  # noqa: E402
from climate.nn.seasonal_model import SEEDS  # noqa: E402
from climate.nn.torch_model import (  # noqa: E402
    FitConfig,
    Standardiser,
    collapse_seeds,
    configure_device,
    crps_gaussian_torch,
    fit_gaussian_net,
    load_net,
    make_net,
    predict_gaussian,
    save_net,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _biased_problem(n: int, seed: int):
    """Three inputs; the truth is x0 - 0.3 + N(0, 0.1) and the raw forecast says x0, sd 0.3."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 3))
    y = X[:, 0] - 0.3 + rng.normal(0.0, 0.1, size=n)
    return X, y, X[:, 0].copy(), np.full(n, 0.3)


def _same_weights(a, b) -> bool:
    return all(torch.equal(p, q) for p, q in zip(a.state_dict().values(), b.state_dict().values()))


def test_torch_crps_matches_the_closed_form_numpy_version():
    rng = np.random.default_rng(0)
    y, mu, s = rng.normal(size=200), rng.normal(size=200), rng.uniform(0.05, 1, 200)
    got = crps_gaussian_torch(torch.tensor(y), torch.tensor(mu), torch.tensor(s)).numpy()
    np.testing.assert_allclose(got, crps_gaussian(y, mu, s), atol=1e-6)


def test_configure_device_is_cpu_without_cuda(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert configure_device(8.0) == "cpu"


def test_fit_beats_the_raw_ensemble_on_a_biased_synthetic_problem():
    X, y, raw_mu, raw_sd = _biased_problem(n=600, seed=1)  # truth = x0 - 0.3 + N(0, 0.1)
    net, hist = fit_gaussian_net(X[:400], y[:400], X[400:500], y[400:500], FitConfig(seed=0))
    mu, sigma = predict_gaussian(net, X[500:], "cpu")
    fitted = crps_gaussian(y[500:], mu, sigma).mean()
    raw = crps_gaussian(y[500:], raw_mu[500:], raw_sd[500:]).mean()
    assert fitted < 0.6 * raw
    assert 1 <= hist["best_epoch"] <= 500


def test_the_same_seed_gives_the_same_weights():
    X, y, _, _ = _biased_problem(n=300, seed=2)
    a, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], FitConfig(seed=3, max_epochs=20))
    b, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], FitConfig(seed=3, max_epochs=20))
    assert all(torch.equal(p, q) for p, q in zip(a.state_dict().values(), b.state_dict().values()))


def test_collapse_seeds_adds_the_spread_of_the_means():
    mus, sigmas = [np.array([0.0]), np.array([2.0])], [np.array([1.0]), np.array([1.0])]
    mu, sigma = collapse_seeds(mus, sigmas)
    assert mu[0] == 1.0 and sigma[0] == pytest.approx(np.sqrt(1.0 + 1.0))


def test_save_and_load_round_trip(tmp_path):
    X, y, _, _ = _biased_problem(n=300, seed=4)
    net, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], FitConfig(seed=0, max_epochs=10))
    save_net(net, tmp_path / "w.pt")
    again = load_net(tmp_path / "w.pt", in_dim=X.shape[1], hidden=32)
    before, after = predict_gaussian(net, X, "cpu"), predict_gaussian(again, X, "cpu")
    np.testing.assert_allclose(after[0], before[0])
    np.testing.assert_allclose(after[1], before[1])


def test_standardiser_round_trip():
    s = Standardiser().fit(np.array([[1.0, 10.0], [3.0, 10.0]]))
    again = Standardiser.from_dict(s.to_dict())
    assert again.transform(np.array([[2.0, 10.0]])).tolist() == [[0.0, 0.0]]


def _in_fresh_interpreter(code: str) -> subprocess.CompletedProcess:
    """Run code in a new process whose import path starts at this checkout."""
    code = "import sys; sys.path.insert(0, sys.argv[1])\n" + code
    return subprocess.run(
        [sys.executable, "-c", code, str(REPO_ROOT)], capture_output=True, text=True, timeout=120
    )


def test_the_module_imports_without_torch():
    done = _in_fresh_interpreter("sys.modules['torch'] = None\nimport climate.nn.torch_model")
    assert done.returncode == 0, done.stderr


def test_configure_device_applies_the_budget_and_returns_cuda_when_cuda_is_there(monkeypatch):
    budgets = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch_model, "apply_gpu_budget", budgets.append)
    assert configure_device(8.0) == "cuda"
    assert budgets == [8.0]


def test_torch_crps_floors_sigma_like_the_numpy_version():
    y, mu, sigma = torch.tensor([1.0]), torch.tensor([0.0]), torch.tensor([0.0])
    got = crps_gaussian_torch(y, mu, sigma).numpy()
    np.testing.assert_allclose(got, crps_gaussian(1.0, 0.0, 0.0), atol=1e-6)


def test_the_network_is_two_relu_layers_then_a_mean_and_a_floored_softplus_sigma():
    net = make_net(3)
    assert type(net).__name__ == "SmallGaussianNet" and isinstance(net, torch.nn.Module)
    weights = [tuple(v.shape) for k, v in net.state_dict().items() if k.endswith("weight")]
    assert weights == [(32, 3), (32, 32), (2, 32)]
    assert sum(isinstance(m, torch.nn.ReLU) for m in net.modules()) == 2
    head_weight, head_bias = list(net.parameters())[-2:]
    with torch.no_grad():
        head_weight.zero_()
        head_bias.copy_(torch.tensor([0.7, 0.3]))
        mu, sigma = net(torch.randn(5, 3))
        head_bias.copy_(torch.tensor([0.0, -100.0]))
        floored = net(torch.zeros(1, 3))[1]
    assert mu.shape == sigma.shape == (5,)
    np.testing.assert_allclose(mu.numpy(), 0.7, rtol=1e-6)
    np.testing.assert_allclose(sigma.numpy(), np.log1p(np.exp(0.3)) + 1e-4, rtol=1e-6)
    assert floored.item() == pytest.approx(1e-4, rel=1e-3)


def test_early_stopping_restores_the_best_validation_weights():
    X, y, _, _ = _biased_problem(n=120, seed=5)
    cfg = FitConfig(seed=0, lr=0.01, max_epochs=400, patience=10)
    net, hist = fit_gaussian_net(X[:20], y[:20], X[20:], y[20:], cfg)  # 20 rows overfit quickly
    epochs = len(hist["train_crps"])
    assert set(hist) == {"best_epoch", "best_val_crps", "train_crps"}
    assert hist["best_epoch"] < epochs and epochs - hist["best_epoch"] == cfg.patience
    mu, sigma = predict_gaussian(net, X[20:], "cpu")
    assert mu.dtype == sigma.dtype == np.float64 and mu.shape == (100,)
    assert crps_gaussian(y[20:], mu, sigma).mean() == pytest.approx(hist["best_val_crps"], rel=1e-4)
    assert json.loads(json.dumps(hist)) == hist
    # the restored weights are those after exactly best_epoch epochs
    short = replace(cfg, max_epochs=hist["best_epoch"])
    again, again_hist = fit_gaussian_net(X[:20], y[:20], X[20:], y[20:], short)
    assert _same_weights(net, again)
    assert again_hist["train_crps"] == hist["train_crps"][: hist["best_epoch"]]


def test_max_epochs_bounds_the_run():
    X, y, _, _ = _biased_problem(n=60, seed=7)
    _, hist = fit_gaussian_net(X[:40], y[:40], X[40:], y[40:], FitConfig(max_epochs=5))
    assert len(hist["train_crps"]) == 5 and 1 <= hist["best_epoch"] <= 5


def test_different_seeds_give_different_weights():
    X, y, _, _ = _biased_problem(n=300, seed=2)
    a, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], FitConfig(seed=3, max_epochs=20))
    b, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], FitConfig(seed=4, max_epochs=20))
    assert not _same_weights(a, b)


@pytest.mark.parametrize("change", [{"lr": 0.01}, {"weight_decay": 0.5}, {"hidden": 8}])
def test_a_changed_fit_setting_changes_the_weights(change):
    X, y, _, _ = _biased_problem(n=300, seed=2)
    base = FitConfig(seed=3, max_epochs=20)
    a, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], base)
    b, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], replace(base, **change))
    assert not _same_weights(a, b)


def test_a_fit_switches_on_deterministic_algorithms():
    X, y, _, _ = _biased_problem(n=60, seed=7)
    torch.use_deterministic_algorithms(False)
    fit_gaussian_net(X[:40], y[:40], X[40:], y[40:], FitConfig(max_epochs=2))
    assert torch.are_deterministic_algorithms_enabled()


def test_fit_refuses_non_finite_or_misshapen_arrays():
    X, y, _, _ = _biased_problem(n=60, seed=6)
    cfg = FitConfig(max_epochs=2)
    holed = X.copy()
    holed[0, 0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        fit_gaussian_net(holed[:40], y[:40], X[40:], y[40:], cfg)
    with pytest.raises(ValueError, match="shape"):
        fit_gaussian_net(X[:40], y[:40, None], X[40:], y[40:], cfg)
    with pytest.raises(ValueError, match="shape"):
        fit_gaussian_net(X[:40], y[:40], X[:0], y[:0], cfg)


@pytest.mark.parametrize("max_epochs", [0, -1])
def test_fewer_than_one_epoch_is_refused(max_epochs):
    X, y, _, _ = _biased_problem(n=60, seed=6)
    with pytest.raises(ValueError, match=f"max_epochs must be at least 1, got {max_epochs}"):
        fit_gaussian_net(X[:40], y[:40], X[40:], y[40:], FitConfig(max_epochs=max_epochs))


def test_a_validation_set_with_other_features_than_training_is_refused():
    X, y, _, _ = _biased_problem(n=60, seed=6)
    with pytest.raises(ValueError, match="validation has 2 features, training has 3"):
        fit_gaussian_net(X[:40], y[:40], X[40:, :2], y[40:], FitConfig(max_epochs=2))


def test_the_fit_defaults_are_the_protocol_constants():
    assert asdict(FitConfig()) == {
        "seed": 0,
        "hidden": 32,
        "lr": 1e-3,
        "weight_decay": 1e-4,
        "max_epochs": 500,
        "patience": 50,
        "device": "cpu",
    }
    assert SEEDS == (0, 1, 2, 3, 4)
    hidden_layers = [m for m in make_net(3).modules() if isinstance(m, torch.nn.ReLU)]
    assert len(hidden_layers) == 2


def test_a_loaded_net_is_in_eval_mode_on_the_cpu(tmp_path):
    save_net(make_net(3), tmp_path / "w.pt")
    again = load_net(tmp_path / "w.pt", in_dim=3, hidden=32)
    assert not again.training and all(p.device.type == "cpu" for p in again.parameters())


def test_collapse_of_one_seed_is_that_seed():
    mu, sigma = collapse_seeds([np.array([0.5, -1.0, 2.0])], [np.array([0.1, 0.2, 0.3])])
    np.testing.assert_allclose(mu, [0.5, -1.0, 2.0])
    np.testing.assert_allclose(sigma, [0.1, 0.2, 0.3])


def test_collapse_seeds_averages_the_variances_not_the_sds():
    same = [np.array([1.0, 1.0]), np.array([1.0, 1.0])]
    _, sigma = collapse_seeds(same, [np.array([1.0, 2.0]), np.array([3.0, 2.0])])
    np.testing.assert_allclose(sigma, [np.sqrt(5.0), 2.0])


def test_standardiser_uses_the_population_sd_gives_a_constant_column_scale_one_and_writes_json():
    s = Standardiser().fit(np.array([[1.0, 10.0], [5.0, 10.0]]))
    assert s.mean.tolist() == [3.0, 10.0] and s.sd.tolist() == [2.0, 1.0]
    assert s.transform(np.array([[5.0, 10.0]])).tolist() == [[1.0, 0.0]]
    assert s.transform(np.array([[5.0, 11.0]])).tolist() == [[1.0, 1.0]]  # a later value is small
    assert json.loads(json.dumps(s.to_dict())) == s.to_dict()


def test_a_column_constant_up_to_rounding_noise_also_gets_scale_one():
    s = Standardiser().fit(np.full((3, 1), 0.1))  # the computed sd of this column is 1e-17, not 0
    assert s.sd.tolist() == [1.0]
    assert s.transform(np.array([[0.2]]))[0, 0] == pytest.approx(0.1)


# the GPU budget applies once per process, before any allocation, so the job gets a fresh one
CUDA_JOB = """
import tempfile
from pathlib import Path
import numpy as np
import torch
from climate.nn import torch_model as tm

assert tm.configure_device(1.0) == "cuda"
rng = np.random.default_rng(7)
X = rng.normal(size=(200, 3))
y = X[:, 0] - 0.3 + rng.normal(0.0, 0.1, size=200)
cfg = tm.FitConfig(seed=0, max_epochs=10, device="cuda")
a, _ = tm.fit_gaussian_net(X[:150], y[:150], X[150:], y[150:], cfg)
b, _ = tm.fit_gaussian_net(X[:150], y[:150], X[150:], y[150:], cfg)
assert all(torch.equal(p, q) for p, q in zip(a.state_dict().values(), b.state_dict().values()))
mu_cuda = tm.predict_gaussian(a, X, "cuda")[0]
with tempfile.TemporaryDirectory() as tmp:
    tm.save_net(a, Path(tmp) / "w.pt")
    cpu_net = tm.load_net(Path(tmp) / "w.pt", in_dim=3, hidden=32)
np.testing.assert_allclose(tm.predict_gaussian(cpu_net, X, "cpu")[0], mu_cuda, atol=1e-4)
"""


@pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a CUDA device")
def test_a_fit_on_cuda_is_deterministic_and_its_weights_load_on_the_cpu():
    done = _in_fresh_interpreter(CUDA_JOB)
    assert done.returncode == 0, done.stderr
