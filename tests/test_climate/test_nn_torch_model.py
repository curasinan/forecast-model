"""The torch core of the GTM research models: device, CRPS loss, a small Gaussian network."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from climate.emos import crps_gaussian  # noqa: E402
from climate.nn import torch_model  # noqa: E402
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


def test_standardiser_round_trip():
    s = Standardiser().fit(np.array([[1.0, 10.0], [3.0, 10.0]]))
    again = Standardiser.from_dict(s.to_dict())
    assert again.transform(np.array([[2.0, 10.0]])).tolist() == [[0.0, 0.0]]


def test_the_module_imports_without_torch():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); sys.modules['torch'] = None; "
        "import climate.nn.torch_model"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(REPO_ROOT)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


def test_configure_device_applies_the_budget_and_returns_cuda_when_cuda_is_there(monkeypatch):
    budgets = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch_model, "apply_gpu_budget", budgets.append)
    assert configure_device(8.0) == "cuda"
    assert budgets == [8.0]


def test_the_network_has_two_relu_hidden_layers_and_a_positive_sigma():
    net = make_net(3)
    assert type(net).__name__ == "SmallGaussianNet" and isinstance(net, torch.nn.Module)
    weights = [tuple(v.shape) for k, v in net.state_dict().items() if k.endswith("weight")]
    assert weights == [(32, 3), (32, 32), (2, 32)]
    assert sum(isinstance(m, torch.nn.ReLU) for m in net.modules()) == 2
    mu, sigma = net(torch.randn(5, 3))
    assert mu.shape == sigma.shape == (5,)
    assert bool((sigma > 1e-4).all())


def test_early_stopping_restores_the_best_validation_weights():
    X, y, _, _ = _biased_problem(n=120, seed=5)
    cfg = FitConfig(seed=0, lr=0.01, max_epochs=400, patience=10)
    net, hist = fit_gaussian_net(X[:20], y[:20], X[20:], y[20:], cfg)
    epochs = len(hist["train_crps"])
    assert set(hist) == {"best_epoch", "best_val_crps", "train_crps"}
    assert hist["best_epoch"] < epochs and epochs - hist["best_epoch"] == cfg.patience
    mu, sigma = predict_gaussian(net, X[20:], "cpu")
    assert crps_gaussian(y[20:], mu, sigma).mean() == pytest.approx(hist["best_val_crps"], rel=1e-4)
    assert json.loads(json.dumps(hist)) == hist


def test_different_seeds_give_different_weights():
    X, y, _, _ = _biased_problem(n=300, seed=2)
    a, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], FitConfig(seed=3, max_epochs=20))
    b, _ = fit_gaussian_net(X[:200], y[:200], X[200:], y[200:], FitConfig(seed=4, max_epochs=20))
    assert not all(
        torch.equal(p, q) for p, q in zip(a.state_dict().values(), b.state_dict().values())
    )


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


def test_collapse_of_one_seed_is_that_seed():
    mu, sigma = collapse_seeds([np.array([0.5, -1.0, 2.0])], [np.array([0.1, 0.2, 0.3])])
    np.testing.assert_allclose(mu, [0.5, -1.0, 2.0])
    np.testing.assert_allclose(sigma, [0.1, 0.2, 0.3])


def test_standardiser_uses_the_population_sd_floors_a_constant_column_and_writes_json():
    s = Standardiser().fit(np.array([[1.0, 10.0], [3.0, 10.0]]))
    assert s.mean.tolist() == [2.0, 10.0] and s.sd.tolist() == [1.0, 1e-8]
    assert json.loads(json.dumps(s.to_dict())) == s.to_dict()
