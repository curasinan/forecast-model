"""ERA5 (absolute Kelvin) -> NOAA GTM anomaly (degC) bridge."""

from __future__ import annotations

import json
import re
from typing import Any

import numpy as np
import pandas as pd

_YM_RE = re.compile(r"^\d{4}-\d{2}$")
N_PARAMS = 13  # 12 intercepts + 1 slope


def to_year_month_index(index: pd.Index | pd.Series | list) -> pd.Index:
    """Normalise an index of months to 'YYYY-MM' strings."""
    idx = pd.Index(index)
    if isinstance(idx, pd.DatetimeIndex):
        return pd.Index(idx.to_period("M").astype(str))
    if isinstance(idx, pd.PeriodIndex):
        return pd.Index(idx.asfreq("M").astype(str))
    if pd.api.types.is_datetime64_any_dtype(idx.dtype):
        return pd.Index(pd.DatetimeIndex(idx).to_period("M").astype(str))
    out = pd.Index(idx.astype(str))
    bad = [v for v in out if not _YM_RE.match(v)]
    if bad:
        raise ValueError(f"expected 'YYYY-MM' month keys, got e.g. {bad[:3]!r}")
    return out


class Bridge:
    """Linear map ERA5 global-mean K -> NOAA GTM anomaly degC."""

    def __init__(
        self,
        alpha: dict[int, float] | None = None,
        beta: float | None = None,
        *,
        r2: float = float("nan"),
        resid_sd: float = float("nan"),
        n_obs: int = 0,
        fit_start: str | None = None,
        fit_end: str | None = None,
    ) -> None:
        self.alpha: dict[int, float] = {int(k): float(v) for k, v in (alpha or {}).items()}
        self.beta: float | None = None if beta is None else float(beta)
        self.r2 = float(r2)
        self.resid_sd = float(resid_sd)
        self.n_obs = int(n_obs)
        self.fit_start = fit_start
        self.fit_end = fit_end

    @property
    def is_fitted(self) -> bool:
        return self.beta is not None and len(self.alpha) == 12

    def fit(
        self,
        era5_K: pd.Series,
        noaa_anom_C: pd.Series,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> "Bridge":
        """Least-squares fit on the overlap of the two monthly series."""
        x = pd.Series(np.asarray(era5_K, dtype=float), index=to_year_month_index(era5_K.index))
        y = pd.Series(
            np.asarray(noaa_anom_C, dtype=float), index=to_year_month_index(noaa_anom_C.index)
        )
        x = x[~x.index.duplicated(keep="first")]
        y = y[~y.index.duplicated(keep="first")]
        joined = pd.concat({"x": x, "y": y}, axis=1, join="inner").dropna()
        if start is not None:
            joined = joined[joined.index >= start]
        if end is not None:
            joined = joined[joined.index <= end]
        joined = joined.sort_index()
        n = len(joined)
        if n <= N_PARAMS:
            raise ValueError(f"bridge fit needs > {N_PARAMS} overlapping months, got {n}")
        months = joined.index.str.slice(5, 7).astype(int).to_numpy()
        present = set(months.tolist())
        missing = sorted(set(range(1, 13)) - present)
        if missing:
            raise ValueError(f"bridge fit: no overlap for calendar month(s) {missing}")

        design = np.zeros((n, N_PARAMS))
        design[np.arange(n), months - 1] = 1.0
        design[:, 12] = joined["x"].to_numpy()
        yv = joined["y"].to_numpy()
        coef, *_ = np.linalg.lstsq(design, yv, rcond=None)
        fitted = design @ coef
        resid = yv - fitted
        ss_res = float(np.sum(resid**2))
        ss_tot = float(np.sum((yv - yv.mean()) ** 2))

        self.alpha = {m + 1: float(coef[m]) for m in range(12)}
        self.beta = float(coef[12])
        self.r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
        self.resid_sd = float(np.sqrt(ss_res / (n - N_PARAMS)))
        self.n_obs = n
        self.fit_start = str(joined.index[0])
        self.fit_end = str(joined.index[-1])
        return self

    def _require_fit(self) -> None:
        if not self.is_fitted:
            raise RuntimeError("Bridge is not fitted; call fit() or from_json() first")

    def predict_mean(self, era5_K, month_of_year):
        """alpha[month_of_year] + beta * era5_K, vectorised."""
        self._require_fit()
        x = np.asarray(era5_K, dtype=float)
        m = np.asarray(month_of_year)
        if np.ndim(m) == 0 and np.ndim(x) == 0:
            return float(self.alpha[int(m)] + self.beta * float(x))
        alpha_vec = np.array([self.alpha[k] for k in range(1, 13)])
        m_arr = np.asarray(m, dtype=int)
        if m_arr.min() < 1 or m_arr.max() > 12:
            raise ValueError("month_of_year must be within 1..12")
        out = alpha_vec[m_arr - 1] + self.beta * x
        if isinstance(era5_K, pd.Series):
            return pd.Series(out, index=era5_K.index, name="fc_mean_C")
        return out

    def predict_sd_scale(self) -> float:
        """Multiplier converting a Kelvin spread to a degC spread on the NOAA scale."""
        self._require_fit()
        return abs(float(self.beta))

    @property
    def params(self) -> dict[str, Any]:
        return {
            "model": "noaa_gtm_C ~ alpha[month_of_year] + beta * era5_global_K",
            "alpha": {str(k): self.alpha[k] for k in sorted(self.alpha)},
            "beta": self.beta,
            "r2": self.r2,
            "resid_sd": self.resid_sd,
            "n_obs": self.n_obs,
            "fit_start": self.fit_start,
            "fit_end": self.fit_end,
        }

    def to_json(self) -> str:
        return json.dumps(self.params, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Bridge":
        d = json.loads(text)
        return cls(
            alpha={int(k): float(v) for k, v in d["alpha"].items()},
            beta=d["beta"],
            r2=d.get("r2", float("nan")),
            resid_sd=d.get("resid_sd", float("nan")),
            n_obs=d.get("n_obs", 0),
            fit_start=d.get("fit_start"),
            fit_end=d.get("fit_end"),
        )

    def __repr__(self) -> str:
        if not self.is_fitted:
            return "Bridge(unfitted)"
        return (
            f"Bridge(beta={self.beta:.4f}, r2={self.r2:.4f}, resid_sd={self.resid_sd:.4f}, "
            f"n_obs={self.n_obs}, {self.fit_start}..{self.fit_end})"
        )
