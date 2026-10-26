"""The torch core of the GTM research models: device, CRPS loss, a small Gaussian network."""

from __future__ import annotations

import copy
import functools
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from analysis.gpu_budget import apply_gpu_budget

if TYPE_CHECKING:
    import torch

NET_SIGMA_FLOOR = 1e-4
LOSS_SIGMA_FLOOR = 1e-6  # the floor climate.emos.crps_gaussian uses
_SQRT2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)
_INV_SQRT_PI = 1.0 / math.sqrt(math.pi)


@dataclass(eq=False)
class Standardiser:
    """Per-column mean and sd of the training rows; a constant column gets scale 1.0."""

    mean: np.ndarray | None = None
    sd: np.ndarray | None = None

    def fit(self, X: np.ndarray) -> Standardiser:
        X = np.asarray(X, dtype=float)
        self.mean = X.mean(axis=0)
        self.sd = np.where(np.ptp(X, axis=0) > 0.0, X.std(axis=0), 1.0)
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        return (np.asarray(X, dtype=float) - self.mean) / self.sd

    def to_dict(self) -> dict:
        return {"mean": self.mean.tolist(), "sd": self.sd.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> Standardiser:
        return cls(np.asarray(d["mean"], dtype=float), np.asarray(d["sd"], dtype=float))


@dataclass
class FitConfig:
    """Settings of one network fit."""

    seed: int = 0
    hidden: int = 32
    lr: float = 1e-3
    weight_decay: float = 1e-4
    max_epochs: int = 500
    patience: int = 50
    device: str = "cpu"


def configure_device(budget_gib: float | None = None) -> str:
    """'cuda' under the GPU memory budget when CUDA is there, else 'cpu'."""
    import torch

    if not torch.cuda.is_available():
        return "cpu"
    apply_gpu_budget(budget_gib)
    return "cuda"


def crps_gaussian_torch(y: torch.Tensor, mu: torch.Tensor, sigma: torch.Tensor) -> torch.Tensor:
    """Closed-form CRPS of N(mu, sigma^2) against y, elementwise."""
    import torch

    sigma = torch.clamp(sigma, min=LOSS_SIGMA_FLOOR)
    z = (y - mu) / sigma
    pdf = torch.exp(-0.5 * z * z) / _SQRT_2PI
    return sigma * (z * torch.erf(z / _SQRT2) + 2.0 * pdf - _INV_SQRT_PI)


@functools.cache
def _net_class() -> type:
    import torch

    class SmallGaussianNet(torch.nn.Module):
        """Two ReLU hidden layers; forward gives the mean and a positive sigma."""

        def __init__(self, in_dim: int, hidden: int = 32) -> None:
            super().__init__()
            self.layers = torch.nn.Sequential(
                torch.nn.Linear(in_dim, hidden),
                torch.nn.ReLU(),
                torch.nn.Linear(hidden, hidden),
                torch.nn.ReLU(),
                torch.nn.Linear(hidden, 2),
            )

        def forward(self, x):
            out = self.layers(x)
            return out[:, 0], torch.nn.functional.softplus(out[:, 1]) + NET_SIGMA_FLOOR

    return SmallGaussianNet


def make_net(in_dim: int, hidden: int = 32) -> torch.nn.Module:
    """A SmallGaussianNet; the class is built here so that this module loads without torch."""
    return _net_class()(in_dim, hidden)


def _checked(name: str, X, y) -> tuple[np.ndarray, np.ndarray]:
    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
    if X.ndim != 2 or len(X) == 0 or y.shape != (len(X),):
        raise ValueError(f"{name}: X must have shape (n, d) and y shape (n,), n > 0")
    if not (np.isfinite(X).all() and np.isfinite(y).all()):
        raise ValueError(f"{name}: X and y must be finite")
    return X, y


def _tensor(a: np.ndarray, device: str) -> torch.Tensor:
    import torch

    return torch.as_tensor(np.array(a, dtype=np.float32), device=device)


def fit_gaussian_net(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    cfg: FitConfig,
) -> tuple[torch.nn.Module, dict]:
    """Full-batch Adam on the training CRPS; the best validation epoch's weights come back."""
    import torch

    X_train, y_train = _checked("train", X_train, y_train)
    X_val, y_val = _checked("validation", X_val, y_val)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(cfg.seed)
    net = make_net(X_train.shape[1], cfg.hidden).to(cfg.device)
    Xt, yt = _tensor(X_train, cfg.device), _tensor(y_train, cfg.device)
    Xv, yv = _tensor(X_val, cfg.device), _tensor(y_val, cfg.device)
    optimiser = torch.optim.Adam(net.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    train_crps: list[float] = []
    best_val, best_epoch, best_state = math.inf, 0, None
    for epoch in range(1, cfg.max_epochs + 1):
        optimiser.zero_grad()
        loss = crps_gaussian_torch(yt, *net(Xt)).mean()
        loss.backward()
        optimiser.step()
        train_crps.append(loss.item())
        with torch.no_grad():
            val = crps_gaussian_torch(yv, *net(Xv)).mean().item()
        if val < best_val:
            best_val, best_epoch, best_state = val, epoch, copy.deepcopy(net.state_dict())
        elif epoch - best_epoch >= cfg.patience:
            break
    if best_state is None:
        raise RuntimeError("the validation CRPS was never finite")
    net.load_state_dict(best_state)
    net.eval()
    return net, {"best_epoch": best_epoch, "best_val_crps": best_val, "train_crps": train_crps}


def predict_gaussian(
    net: torch.nn.Module, X: np.ndarray, device: str
) -> tuple[np.ndarray, np.ndarray]:
    """The network's mu and sigma for the rows of X, as float64 arrays."""
    import torch

    net = net.to(device).eval()
    with torch.no_grad():
        mu, sigma = net(_tensor(X, device))
    return mu.cpu().numpy().astype(float), sigma.cpu().numpy().astype(float)


def collapse_seeds(
    mus: list[np.ndarray], sigmas: list[np.ndarray]
) -> tuple[np.ndarray, np.ndarray]:
    """One Gaussian from the seeds: the mean of the means, the spread of the means added."""
    mu_s, sigma_s = np.asarray(mus, dtype=float), np.asarray(sigmas, dtype=float)
    return mu_s.mean(axis=0), np.sqrt((sigma_s**2).mean(axis=0) + mu_s.var(axis=0))


def save_net(net: torch.nn.Module, path: Path) -> None:
    """Write the state dict of the network to path."""
    import torch

    torch.save(net.state_dict(), path)


def load_net(path: Path, in_dim: int, hidden: int) -> torch.nn.Module:
    """A network with the saved weights, on the CPU and in eval mode."""
    import torch

    net = make_net(in_dim, hidden)
    net.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
    return net.eval()
