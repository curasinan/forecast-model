"""The seasonal GTM network behind the EMOS-shaped fit and predict interface."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from climate.emos import crps_gaussian, target_year
from climate.nn.features import SEASONAL_FEATURES
from climate.nn.torch_model import (
    FitConfig,
    Standardiser,
    collapse_seeds,
    fit_gaussian_net,
    load_net,
    predict_gaussian,
    save_net,
)

SEEDS: tuple[int, ...] = (0, 1, 2, 3, 4)
VALIDATION_YEARS: int = 3


class GtmNnModel:
    """One small Gaussian network per seed on the seasonal features, collapsed to one Gaussian."""

    def __init__(
        self, seeds: tuple[int, ...] = SEEDS, hidden: int = 32, device: str = "cpu"
    ) -> None:
        if len(seeds) == 0:
            raise ValueError("seeds must name at least one seed")
        self.seeds = tuple(int(seed) for seed in seeds)
        self.hidden = int(hidden)
        self.device = device  # fitting only; prediction always runs on the CPU
        self.features: list[str] = list(SEASONAL_FEATURES)
        self.standardiser: Standardiser | None = None
        self.nets: dict[int, Any] = {}  # torch modules, typed Any to keep torch out of this file
        self.validation_years: list[int] = []
        self.history: dict[int, dict] = {}

    def fit(self, frame: pd.DataFrame) -> GtmNnModel:
        """Fit a network per seed on the usable rows; the last three target years validate."""
        X = frame[self.features].to_numpy(dtype=float)
        y = frame["y_C"].to_numpy(dtype=float)
        usable = np.isfinite(X).all(axis=1) & np.isfinite(y)
        X, y = X[usable], y[usable]
        years = target_year(frame[usable])
        distinct = np.unique(years)
        if len(distinct) <= VALIDATION_YEARS:
            raise ValueError(
                f"fit needs at least {VALIDATION_YEARS + 1} distinct target years among the rows "
                f"with a label and every feature, got {len(distinct)}"
            )
        validation_years = [int(year) for year in distinct[-VALIDATION_YEARS:]]
        held_out = np.isin(years, validation_years)
        standardiser = Standardiser().fit(X[~held_out])
        X_train, y_train = standardiser.transform(X[~held_out]), y[~held_out]
        X_val, y_val = standardiser.transform(X[held_out]), y[held_out]
        nets: dict[int, Any] = {}
        history: dict[int, dict] = {}
        for seed in self.seeds:
            cfg = FitConfig(seed=seed, hidden=self.hidden, device=self.device)
            nets[seed], history[seed] = fit_gaussian_net(X_train, y_train, X_val, y_val, cfg)
        self.standardiser, self.nets = standardiser, nets
        self.validation_years, self.history = validation_years, history
        return self

    def predict(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Predictive (mu, sigma) per row of the frame; NaN where a feature is not finite."""
        standardiser = self._fitted()
        X = frame[self.features].to_numpy(dtype=float)
        finite = np.isfinite(X).all(axis=1)
        mu = np.full(len(frame), np.nan)
        sigma = np.full(len(frame), np.nan)
        if finite.any():
            Z = standardiser.transform(X[finite])
            per_seed = [predict_gaussian(self.nets[seed], Z, "cpu") for seed in self.seeds]
            mu[finite], sigma[finite] = collapse_seeds(
                [m for m, _ in per_seed], [s for _, s in per_seed]
            )
        return pd.DataFrame({"mu": mu, "sigma": sigma}, index=frame.index)

    def crps(self, frame: pd.DataFrame) -> np.ndarray:
        """Per-row CRPS against y_C; NaN where the prediction or the label is missing."""
        pred = self.predict(frame)
        return crps_gaussian(frame["y_C"].to_numpy(dtype=float), pred["mu"], pred["sigma"])

    def _fitted(self) -> Standardiser:
        """The standardiser of the fit; a model that was neither fitted nor loaded has none."""
        if self.standardiser is None:
            raise RuntimeError("the model is not fitted: call fit or load first")
        return self.standardiser

    def _manifest(self) -> dict:
        """What save writes to manifest.json."""
        return {
            "features": list(self.features),
            "standardiser": self._fitted().to_dict(),
            "seeds": list(self.seeds),
            "hidden": self.hidden,
            "validation_years": list(self.validation_years),
            "history": self.history,
        }

    def save(self, dir_path: Path) -> None:
        """Write manifest.json and one state dict per seed under weights/."""
        manifest = self._manifest()
        dir_path = Path(dir_path)
        (dir_path / "weights").mkdir(parents=True, exist_ok=True)
        for seed, net in self.nets.items():
            save_net(net, dir_path / "weights" / f"seed_{seed}.pt")
        text = json.dumps(manifest, indent=2, sort_keys=True)
        (dir_path / "manifest.json").write_text(text, encoding="utf-8")

    @classmethod
    def load(cls, dir_path: Path) -> GtmNnModel:
        """Rebuild the model that save wrote to dir_path."""
        dir_path = Path(dir_path)
        manifest = json.loads((dir_path / "manifest.json").read_text(encoding="utf-8"))
        model = cls(seeds=tuple(manifest["seeds"]), hidden=manifest["hidden"])
        model.features = list(manifest["features"])
        model.standardiser = Standardiser.from_dict(manifest["standardiser"])
        model.validation_years = [int(year) for year in manifest["validation_years"]]
        model.history = {int(seed): record for seed, record in manifest["history"].items()}
        in_dim = len(model.features)
        model.nets = {
            seed: load_net(dir_path / "weights" / f"seed_{seed}.pt", in_dim, model.hidden)
            for seed in model.seeds
        }
        return model

    def fingerprint(self) -> str:
        """The first 16 hex characters of the sha256 of the manifest without the history."""
        manifest = {key: value for key, value in self._manifest().items() if key != "history"}
        text = json.dumps(manifest, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
