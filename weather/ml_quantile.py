"""LightGBM quantile regression upgrade over EMOS (P6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from weather.buckets import Bucket, p_bucket_from_quantiles

DEFAULT_QUANTILES = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)


@dataclass
class QuantileModelBundle:
    """One LightGBM model per quantile level."""

    models: dict[float, Any] = field(default_factory=dict)
    feature_names: list[str] = field(default_factory=list)
    model_version: str = "lgbm_quantile_v1"

    def predict_quantiles(self, x: np.ndarray) -> dict[float, float]:
        out: dict[float, float] = {}
        row = np.asarray(x, dtype=float).reshape(1, -1)
        if self.feature_names and len(self.feature_names) == row.shape[1]:
            row_in: Any = pd.DataFrame(row, columns=self.feature_names)
        else:
            row_in = row
        for q, model in self.models.items():
            pred = model.predict(row_in)
            out[q] = float(pred[0])
        return out

    def p_bucket(self, x: np.ndarray, bucket: Bucket) -> float:
        qmap = self.predict_quantiles(x)
        levels = sorted(qmap.keys())
        values = [qmap[t] for t in levels]
        return p_bucket_from_quantiles(levels, values, bucket)


def fit_quantile_models(
    X: np.ndarray,
    y: np.ndarray,
    *,
    quantiles: tuple[float, ...] = DEFAULT_QUANTILES,
    feature_names: list[str] | None = None,
    num_leaves: int = 15,
    learning_rate: float = 0.05,
    n_estimators: int = 100,
) -> QuantileModelBundle:
    import lightgbm as lgb

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    bundle = QuantileModelBundle()
    names = feature_names or [f"f{i}" for i in range(X.shape[1])]
    X_df = pd.DataFrame(X, columns=names)
    bundle.feature_names = list(names)
    for q in quantiles:
        model = lgb.LGBMRegressor(
            objective="quantile",
            alpha=q,
            num_leaves=num_leaves,
            learning_rate=learning_rate,
            n_estimators=n_estimators,
            verbosity=-1,
        )
        model.fit(X_df, y)
        bundle.models[q] = model
    return bundle


def default_feature_row(
    *,
    ens_mean: float,
    ens_std: float,
    lead_hours: float,
    month: int,
    coastal: float = 0.0,
    model_disagreement: float = 0.0,
) -> np.ndarray:
    return np.array(
        [ens_mean, ens_std, lead_hours, month, coastal, model_disagreement],
        dtype=float,
    )


DEFAULT_FEATURE_NAMES = [
    "ens_mean",
    "ens_std",
    "lead_hours",
    "month",
    "coastal",
    "model_disagreement",
]
