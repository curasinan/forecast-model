"""Gaussian EMOS for the GTM lane (NOAA monthly global anomaly, SEAS5 ensemble)."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import norm

log = logging.getLogger("climate.emos")

TREND_ORIGIN = 2010.0
SIGMA_FLOOR = 1e-6
VAR_FLOOR = SIGMA_FLOOR**2
MIN_ROWS_PER_LEAD = 8

FRAME_COLUMNS = (
    "fc_mean_C",
    "fc_sd_C",
    "y_C",
    "lead_month",
    "target_year_frac",
    "month_of_year",
)

_SQRT_PI = math.sqrt(math.pi)

# Scoring utilities


def _as_float(*arrays) -> tuple[np.ndarray, ...]:
    return tuple(np.asarray(a, dtype=float) for a in arrays)


def crps_gaussian(y, mu, sigma) -> np.ndarray:
    """Closed-form CRPS of N(mu, sigma^2) against outcome y (vectorised, broadcasting)."""
    y, mu, sigma = np.broadcast_arrays(*_as_float(y, mu, sigma))
    sigma = np.maximum(sigma, SIGMA_FLOOR)
    z = (y - mu) / sigma
    return sigma * (z * (2.0 * norm.cdf(z) - 1.0) + 2.0 * norm.pdf(z) - 1.0 / _SQRT_PI)


def pit(y, mu, sigma) -> np.ndarray:
    """Probability integral transform Phi((y - mu) / sigma); uniform iff calibrated."""
    y, mu, sigma = np.broadcast_arrays(*_as_float(y, mu, sigma))
    return norm.cdf((y - mu) / np.maximum(sigma, SIGMA_FLOOR))


def pit_histogram(pit_values, bins: int = 10) -> pd.DataFrame:
    """Equal-width PIT histogram on [0, 1]; NaNs dropped."""
    p = np.asarray(pit_values, dtype=float).ravel()
    p = p[np.isfinite(p)]
    edges = np.linspace(0.0, 1.0, bins + 1)
    counts, _ = np.histogram(np.clip(p, 0.0, 1.0), bins=edges)
    n = counts.sum()
    frac = counts / n if n else np.full(bins, np.nan)
    return pd.DataFrame(
        {
            "lo": edges[:-1],
            "hi": edges[1:],
            "count": counts.astype(int),
            "frac": frac,
            "expected_frac": np.full(bins, 1.0 / bins),
        }
    )


def interval_coverage(y, mu, sigma, levels: Iterable[float] = (0.5, 0.9)) -> dict[float, float]:
    """Empirical coverage of central predictive intervals at the given levels."""
    y, mu, sigma = np.broadcast_arrays(*_as_float(y, mu, sigma))
    ok = np.isfinite(y) & np.isfinite(mu) & np.isfinite(sigma)
    out: dict[float, float] = {}
    for level in levels:
        level = float(level)
        if not ok.any():
            out[level] = float("nan")
            continue
        half = norm.ppf(0.5 + level / 2.0) * np.maximum(sigma[ok], SIGMA_FLOOR)
        out[level] = float(np.mean(np.abs(y[ok] - mu[ok]) <= half))
    return out


def crps_skill_score(crps_model, crps_baseline) -> float:
    """1 - mean(CRPS_model) / mean(CRPS_baseline); 1 = perfect, 0 = no better, <0 worse."""
    m, b = _as_float(crps_model, crps_baseline)
    m, b = np.broadcast_arrays(m, b)
    ok = np.isfinite(m) & np.isfinite(b)
    if not ok.any():
        return float("nan")
    denom = float(np.mean(b[ok]))
    if denom <= 0.0:
        return float("nan")
    return 1.0 - float(np.mean(m[ok])) / denom


# Frame helpers


def _require_columns(frame: pd.DataFrame, cols: Iterable[str]) -> None:
    missing = [c for c in cols if c not in frame.columns]
    if missing:
        raise KeyError(f"frame is missing required columns: {missing}")


def target_year(frame: pd.DataFrame) -> np.ndarray:
    """Calendar year of each row's target month, from target_year_frac (mid-month)."""
    tyf = np.asarray(frame["target_year_frac"], dtype=float)
    return np.floor(tyf).astype(int)


def _unique_targets(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per target month (the frame repeats each target across leads)."""
    _require_columns(frame, ("y_C", "target_year_frac", "month_of_year"))
    y = np.asarray(frame["y_C"], dtype=float)
    ok = np.isfinite(y)
    sub = pd.DataFrame(
        {
            "year": target_year(frame)[ok],
            "month_of_year": np.asarray(frame["month_of_year"], dtype=int)[ok],
            "target_year_frac": np.asarray(frame["target_year_frac"], dtype=float)[ok],
            "y_C": y[ok],
        }
    )
    return sub.drop_duplicates(subset=["year", "month_of_year"], keep="first").reset_index(
        drop=True
    )


# Gaussian EMOS


def _softplus(x: np.ndarray | float) -> np.ndarray | float:
    return np.logaddexp(0.0, x)


def _softplus_inv(v: float) -> float:
    v = max(float(v), 1e-12)
    return float(np.log(np.expm1(v))) if v < 30.0 else v


def _unpack(theta: np.ndarray, trend: bool) -> tuple[float, float, float, float, float]:
    a, b, raw_c, raw_d = (float(t) for t in theta[:4])
    c_t = float(theta[4]) if trend else 0.0
    return a, b, float(_softplus(raw_c)), raw_d * raw_d, c_t


def _emos_objective(
    theta: np.ndarray,
    x: np.ndarray,
    s2: np.ndarray,
    y: np.ndarray,
    t: np.ndarray,
    trend: bool,
) -> tuple[float, np.ndarray]:
    """Mean Gaussian CRPS and its gradient w.r.t. theta = (a, b, raw_c, raw_d[, c_t])."""
    a, b, raw_c, raw_d = theta[0], theta[1], theta[2], theta[3]
    c_t = theta[4] if trend else 0.0
    c = _softplus(raw_c)
    d = raw_d * raw_d
    mu = a + b * x + c_t * t
    var = np.maximum(c + d * s2, VAR_FLOOR)
    sigma = np.sqrt(var)
    z = (y - mu) / sigma
    cdf = norm.cdf(z)
    pdf = norm.pdf(z)
    crps = sigma * (z * (2.0 * cdf - 1.0) + 2.0 * pdf - 1.0 / _SQRT_PI)
    n = float(len(y))

    dmu = 1.0 - 2.0 * cdf
    dsig = 2.0 * pdf - 1.0 / _SQRT_PI
    dvar = dsig / (2.0 * sigma)  # dsigma/dvar = 1/(2 sigma)
    grad = np.empty(5 if trend else 4)
    grad[0] = dmu.sum() / n
    grad[1] = (dmu * x).sum() / n
    grad[2] = (dvar.sum() / n) * (1.0 / (1.0 + np.exp(-raw_c)))  # softplus' = sigmoid
    grad[3] = ((dvar * s2).sum() / n) * (2.0 * raw_d)
    if trend:
        grad[4] = (dmu * t).sum() / n
    return float(crps.mean()), grad


@dataclass
class LeadParams:
    """Fitted EMOS coefficients for one lead."""

    lead_month: int
    a: float
    b: float
    c: float
    d: float
    c_t: float = 0.0
    trend: bool = False
    n_train: int = 0
    train_crps: float = float("nan")
    converged: bool = True

    def mu_sigma(self, fc_mean, fc_sd, target_year_frac=None, origin: float = TREND_ORIGIN):
        m, s = _as_float(fc_mean, fc_sd)
        mu = self.a + self.b * m
        if self.trend and self.c_t != 0.0:
            if target_year_frac is None:
                raise ValueError("trend model needs target_year_frac")
            mu = mu + self.c_t * (np.asarray(target_year_frac, dtype=float) - origin)
        sigma = np.sqrt(np.maximum(self.c + self.d * s * s, VAR_FLOOR))
        return mu, sigma


class EmosModel:
    """Per-lead Gaussian EMOS fit by minimum CRPS."""

    VERSION = 1

    def __init__(self, trend: bool = False, trend_origin: float = TREND_ORIGIN) -> None:
        self.trend = bool(trend)
        self.trend_origin = float(trend_origin)
        self.params: dict[int, LeadParams] = {}
        self.skipped: dict[int, str] = {}

    # -- fitting ---------------------------------------------------------- #

    def fit(
        self,
        frame: pd.DataFrame,
        leads: Iterable[int] = range(1, 7),
        trend: bool | None = None,
        min_rows: int = MIN_ROWS_PER_LEAD,
    ) -> "EmosModel":
        _require_columns(frame, FRAME_COLUMNS)
        if trend is not None:
            self.trend = bool(trend)
        self.params = {}
        self.skipped = {}
        lead_col = np.asarray(frame["lead_month"], dtype=float)
        x_all = np.asarray(frame["fc_mean_C"], dtype=float)
        s_all = np.asarray(frame["fc_sd_C"], dtype=float)
        y_all = np.asarray(frame["y_C"], dtype=float)
        t_all = np.asarray(frame["target_year_frac"], dtype=float) - self.trend_origin
        finite = np.isfinite(x_all) & np.isfinite(s_all) & np.isfinite(y_all)
        if self.trend:
            finite &= np.isfinite(t_all)

        for lead in leads:
            lead = int(lead)
            sel = finite & (lead_col == lead)
            n = int(sel.sum())
            if n < min_rows:
                self.skipped[lead] = f"{n} usable rows < min_rows={min_rows}"
                log.warning("EMOS lead %d skipped: %s", lead, self.skipped[lead])
                continue
            self.params[lead] = self._fit_lead(
                lead, x_all[sel], s_all[sel], y_all[sel], t_all[sel]
            )
        if not self.params:
            raise ValueError(f"no lead could be fit; skipped={self.skipped}")
        return self

    def _fit_lead(self, lead: int, x, s, y, t) -> LeadParams:
        s2 = s * s
        design = np.column_stack([np.ones_like(x), x] + ([t] if self.trend else []))
        beta, *_ = np.linalg.lstsq(design, y, rcond=None)
        resid_var = float(np.var(y - design @ beta, ddof=min(len(beta), len(y) - 1)))
        resid_var = max(resid_var, 1e-8)
        mean_s2 = float(np.mean(s2))
        if mean_s2 > 0.0:
            c0, d0 = 0.5 * resid_var, 0.5 * resid_var / mean_s2
        else:
            c0, d0 = resid_var, 0.0
        theta0 = [beta[0], beta[1], _softplus_inv(c0), math.sqrt(d0)]
        if self.trend:
            theta0.append(beta[2])
        theta0 = np.asarray(theta0, dtype=float)

        args = (x, s2, y, t, self.trend)
        res = minimize(
            _emos_objective,
            theta0,
            args=args,
            jac=True,
            method="L-BFGS-B",
            options={"maxiter": 2000, "ftol": 1e-12, "gtol": 1e-9},
        )
        best_x, best_f, converged = res.x, float(res.fun), bool(res.success)
        if not converged:
            res2 = minimize(
                lambda th: _emos_objective(th, *args)[0],
                best_x,
                method="Nelder-Mead",
                options={"maxiter": 4000, "xatol": 1e-9, "fatol": 1e-12},
            )
            if float(res2.fun) <= best_f:
                best_x, best_f, converged = res2.x, float(res2.fun), bool(res2.success)
        a, b, c, d, c_t = _unpack(best_x, self.trend)
        return LeadParams(
            lead_month=lead,
            a=a,
            b=b,
            c=c,
            d=d,
            c_t=c_t,
            trend=self.trend,
            n_train=int(len(y)),
            train_crps=best_f,
            converged=converged,
        )

    # -- prediction ------------------------------------------------------- #

    def predict(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Predictive (mu, sigma) per row; NaN where the lead is unfit or inputs missing."""
        cols = ["fc_mean_C", "fc_sd_C", "lead_month"] + (["target_year_frac"] if self.trend else [])
        _require_columns(frame, cols)
        n = len(frame)
        mu = np.full(n, np.nan)
        sigma = np.full(n, np.nan)
        lead_col = np.asarray(frame["lead_month"], dtype=float)
        x = np.asarray(frame["fc_mean_C"], dtype=float)
        s = np.asarray(frame["fc_sd_C"], dtype=float)
        tyf = np.asarray(frame["target_year_frac"], dtype=float) if self.trend else None
        for lead, p in self.params.items():
            sel = lead_col == lead
            if not sel.any():
                continue
            m_l, s_l = p.mu_sigma(
                x[sel], s[sel], None if tyf is None else tyf[sel], origin=self.trend_origin
            )
            mu[sel] = m_l
            sigma[sel] = s_l
        return pd.DataFrame({"mu": mu, "sigma": sigma}, index=frame.index)

    def crps(self, frame: pd.DataFrame) -> np.ndarray:
        """Per-row CRPS of the model against y_C (NaN where either side is missing)."""
        pred = self.predict(frame)
        return crps_gaussian(np.asarray(frame["y_C"], dtype=float), pred["mu"], pred["sigma"])

    # -- inspection / persistence ---------------------------------------- #

    def params_frame(self) -> pd.DataFrame:
        rows = [asdict(self.params[k]) for k in sorted(self.params)]
        return pd.DataFrame(rows)

    def to_dict(self) -> dict:
        return {
            "version": self.VERSION,
            "trend": self.trend,
            "trend_origin": self.trend_origin,
            "leads": {str(k): asdict(v) for k, v in sorted(self.params.items())},
            "skipped": {str(k): v for k, v in self.skipped.items()},
        }

    def to_json(self, path: str | Path | None = None, indent: int = 2) -> str:
        text = json.dumps(self.to_dict(), indent=indent, sort_keys=True)
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    @classmethod
    def from_dict(cls, payload: Mapping) -> "EmosModel":
        model = cls(trend=bool(payload.get("trend", False)),
                    trend_origin=float(payload.get("trend_origin", TREND_ORIGIN)))
        for key, raw in payload.get("leads", {}).items():
            p = LeadParams(**raw)
            model.params[int(key)] = p
        model.skipped = {int(k): str(v) for k, v in payload.get("skipped", {}).items()}
        return model

    @classmethod
    def from_json(cls, text_or_path: str | Path) -> "EmosModel":
        if isinstance(text_or_path, Path) or (
            isinstance(text_or_path, str)
            and not text_or_path.lstrip().startswith("{")
            and Path(text_or_path).exists()
        ):
            text = Path(text_or_path).read_text(encoding="utf-8")
        else:
            text = str(text_or_path)
        return cls.from_dict(json.loads(text))


# Baselines


@dataclass
class MonthlyClimatology:
    """Per-month-of-year Gaussian N(mean, sd) of y_C over the training targets."""

    mu_by_month: dict[int, float] = field(default_factory=dict)
    sd_by_month: dict[int, float] = field(default_factory=dict)
    pooled_mu: float = float("nan")
    pooled_sd: float = float("nan")
    n_train: int = 0

    def fit(self, frame: pd.DataFrame) -> "MonthlyClimatology":
        u = _unique_targets(frame)
        if len(u) < 2:
            raise ValueError("climatology needs at least two labelled target months")
        self.n_train = int(len(u))
        self.pooled_mu = float(u["y_C"].mean())
        self.pooled_sd = float(u["y_C"].std(ddof=1))
        self.mu_by_month, self.sd_by_month = {}, {}
        for m, g in u.groupby("month_of_year"):
            m = int(m)
            self.mu_by_month[m] = float(g["y_C"].mean())
            sd = float(g["y_C"].std(ddof=1)) if len(g) >= 2 else float("nan")
            self.sd_by_month[m] = sd if np.isfinite(sd) and sd > 0 else self.pooled_sd
        return self

    def predict(self, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        _require_columns(frame, ("month_of_year",))
        months = np.asarray(frame["month_of_year"], dtype=int)
        mu = np.array([self.mu_by_month.get(int(m), self.pooled_mu) for m in months], dtype=float)
        sd = np.array([self.sd_by_month.get(int(m), self.pooled_sd) for m in months], dtype=float)
        return mu, np.maximum(sd, SIGMA_FLOOR)


@dataclass
class TrendClimatology:
    """Per-month-of-year linear trend in target_year_frac + residual sd (training only)."""

    origin: float = TREND_ORIGIN
    alpha_by_month: dict[int, float] = field(default_factory=dict)
    beta_by_month: dict[int, float] = field(default_factory=dict)
    sd_by_month: dict[int, float] = field(default_factory=dict)
    pooled_alpha: float = float("nan")
    pooled_beta: float = float("nan")
    pooled_sd: float = float("nan")
    n_train: int = 0

    def fit(self, frame: pd.DataFrame) -> "TrendClimatology":
        u = _unique_targets(frame)
        if len(u) < 3:
            raise ValueError("trend climatology needs at least three labelled target months")
        self.n_train = int(len(u))
        t = u["target_year_frac"].to_numpy(dtype=float) - self.origin
        y = u["y_C"].to_numpy(dtype=float)
        months = u["month_of_year"].to_numpy(dtype=int)

        # Pooled fallback: shared slope, per-month intercepts (month dummies).
        uniq = sorted(set(months.tolist()))
        dummies = np.column_stack([(months == m).astype(float) for m in uniq] + [t])
        coef, *_ = np.linalg.lstsq(dummies, y, rcond=None)
        resid = y - dummies @ coef
        dof = max(len(y) - dummies.shape[1], 1)
        self.pooled_beta = float(coef[-1])
        self.pooled_alpha = float(np.mean(coef[:-1]))
        self.pooled_sd = float(np.sqrt(np.sum(resid**2) / dof))
        pooled_alpha_by_month = {m: float(coef[i]) for i, m in enumerate(uniq)}

        self.alpha_by_month, self.beta_by_month, self.sd_by_month = {}, {}, {}
        for m in uniq:
            sel = months == m
            if sel.sum() >= 3 and np.ptp(t[sel]) > 0:
                d = np.column_stack([np.ones(sel.sum()), t[sel]])
                cf, *_ = np.linalg.lstsq(d, y[sel], rcond=None)
                r = y[sel] - d @ cf
                sd = float(np.sqrt(np.sum(r**2) / max(sel.sum() - 2, 1)))
                self.alpha_by_month[m] = float(cf[0])
                self.beta_by_month[m] = float(cf[1])
                self.sd_by_month[m] = sd if sd > 0 else self.pooled_sd
            else:
                self.alpha_by_month[m] = pooled_alpha_by_month[m]
                self.beta_by_month[m] = self.pooled_beta
                self.sd_by_month[m] = self.pooled_sd
        return self

    def predict(self, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        _require_columns(frame, ("month_of_year", "target_year_frac"))
        months = np.asarray(frame["month_of_year"], dtype=int)
        t = np.asarray(frame["target_year_frac"], dtype=float) - self.origin
        alpha = np.array([self.alpha_by_month.get(int(m), self.pooled_alpha) for m in months])
        beta = np.array([self.beta_by_month.get(int(m), self.pooled_beta) for m in months])
        sd = np.array([self.sd_by_month.get(int(m), self.pooled_sd) for m in months])
        return alpha + beta * t, np.maximum(sd.astype(float), SIGMA_FLOOR)


def climatology_baseline(train: pd.DataFrame, frame: pd.DataFrame):
    """(mu, sigma) arrays for ``frame`` from monthly climatology fit on ``train``."""
    return MonthlyClimatology().fit(train).predict(frame)


def trend_climatology_baseline(train: pd.DataFrame, frame: pd.DataFrame):
    """(mu, sigma) arrays for ``frame`` from per-month trend climatology fit on ``train``."""
    return TrendClimatology().fit(train).predict(frame)


# Validation helpers


def time_split(frame: pd.DataFrame, train_through_year: int) -> tuple[pd.Series, pd.Series]:
    """Boolean masks (train, test) by TARGET year: train <= year < test."""
    years = target_year(frame)
    train = pd.Series(years <= int(train_through_year), index=frame.index)
    test = pd.Series(years > int(train_through_year), index=frame.index)
    return train, test


def expanding_window_predictions(
    frame: pd.DataFrame,
    first_test_year: int,
    last_test_year: int,
    trend: bool = False,
    leads: Iterable[int] = range(1, 7),
    min_train_targets: int = 24,
) -> pd.DataFrame:
    """Per-row out-of-sample predictions for every test year in [first, last]."""
    _require_columns(frame, FRAME_COLUMNS)
    leads = [int(lead) for lead in leads]
    years = target_year(frame)
    y_all = np.asarray(frame["y_C"], dtype=float)
    lead_col = np.asarray(frame["lead_month"], dtype=float)
    out: list[pd.DataFrame] = []
    for test_year in range(int(first_test_year), int(last_test_year) + 1):
        train_mask = (years < test_year) & np.isfinite(y_all)
        test_mask = (years == test_year) & np.isin(lead_col, leads)
        train = frame.loc[train_mask]
        test = frame.loc[test_mask]
        if len(test) == 0:
            continue
        if len(_unique_targets(train)) < min_train_targets:
            log.warning("expanding window: test year %d skipped (too few labelled targets)",
                        test_year)
            continue
        model = EmosModel(trend=trend).fit(train, leads=leads, trend=trend)
        clim = MonthlyClimatology().fit(train)
        tclim = TrendClimatology().fit(train)

        pred = model.predict(test)
        mu_c, sd_c = clim.predict(test)
        mu_t, sd_t = tclim.predict(test)
        y = np.asarray(test["y_C"], dtype=float)
        block = pd.DataFrame(
            {
                "row": test.index.to_numpy(),
                "test_year": test_year,
                "lead_month": np.asarray(test["lead_month"], dtype=int),
                "month_of_year": np.asarray(test["month_of_year"], dtype=int),
                "y_C": y,
                "mu_emos": pred["mu"].to_numpy(),
                "sigma_emos": pred["sigma"].to_numpy(),
                "mu_clim": mu_c,
                "sigma_clim": sd_c,
                "mu_trend": mu_t,
                "sigma_trend": sd_t,
            }
        )
        block["crps_emos"] = crps_gaussian(y, block["mu_emos"], block["sigma_emos"])
        block["crps_clim"] = crps_gaussian(y, mu_c, sd_c)
        block["crps_trend"] = crps_gaussian(y, mu_t, sd_t)
        out.append(block)
    if not out:
        return pd.DataFrame(
            columns=[
                "row", "test_year", "lead_month", "month_of_year", "y_C",
                "mu_emos", "sigma_emos", "mu_clim", "sigma_clim", "mu_trend", "sigma_trend",
                "crps_emos", "crps_clim", "crps_trend",
            ]
        )
    return pd.concat(out, ignore_index=True)


def expanding_window_scores(
    frame: pd.DataFrame,
    first_test_year: int,
    last_test_year: int,
    trend: bool = False,
    leads: Iterable[int] = range(1, 7),
    min_train_targets: int = 24,
) -> pd.DataFrame:
    """Per (test_year, lead_month) mean CRPS of EMOS vs both baselines, out of sample."""
    rows = expanding_window_predictions(
        frame, first_test_year, last_test_year, trend=trend, leads=leads,
        min_train_targets=min_train_targets,
    )
    cols = ["test_year", "lead_month", "n", "crps_emos", "crps_clim", "crps_trend",
            "skill_vs_clim", "skill_vs_trend", "cover50_emos", "cover90_emos"]
    if rows.empty:
        return pd.DataFrame(columns=cols)
    scored = rows[np.isfinite(rows["y_C"]) & np.isfinite(rows["crps_emos"])]
    recs = []
    for (ty, lead), g in scored.groupby(["test_year", "lead_month"], sort=True):
        cov = interval_coverage(g["y_C"], g["mu_emos"], g["sigma_emos"], levels=(0.5, 0.9))
        recs.append(
            {
                "test_year": int(ty),
                "lead_month": int(lead),
                "n": int(len(g)),
                "crps_emos": float(g["crps_emos"].mean()),
                "crps_clim": float(g["crps_clim"].mean()),
                "crps_trend": float(g["crps_trend"].mean()),
                "skill_vs_clim": crps_skill_score(g["crps_emos"], g["crps_clim"]),
                "skill_vs_trend": crps_skill_score(g["crps_emos"], g["crps_trend"]),
                "cover50_emos": cov[0.5],
                "cover90_emos": cov[0.9],
            }
        )
    return pd.DataFrame(recs, columns=cols)


def summarise_scores(scores: pd.DataFrame) -> pd.DataFrame:
    """Collapse expanding-window scores to one row per lead (row-weighted means)."""
    if scores.empty:
        return scores
    recs = []
    for lead, g in scores.groupby("lead_month", sort=True):
        w = g["n"].to_numpy(dtype=float)
        tot = w.sum()

        def wmean(col: str) -> float:
            return float(np.sum(g[col].to_numpy(dtype=float) * w) / tot) if tot else float("nan")

        ce, cc, ct = wmean("crps_emos"), wmean("crps_clim"), wmean("crps_trend")
        recs.append(
            {
                "lead_month": int(lead),
                "n": int(tot),
                "years": int(g["test_year"].nunique()),
                "crps_emos": ce,
                "crps_clim": cc,
                "crps_trend": ct,
                "skill_vs_clim": 1.0 - ce / cc if cc > 0 else float("nan"),
                "skill_vs_trend": 1.0 - ce / ct if ct > 0 else float("nan"),
                "cover50_emos": wmean("cover50_emos"),
                "cover90_emos": wmean("cover90_emos"),
            }
        )
    return pd.DataFrame(recs)


__all__ = [
    "TREND_ORIGIN",
    "FRAME_COLUMNS",
    "crps_gaussian",
    "pit",
    "pit_histogram",
    "interval_coverage",
    "crps_skill_score",
    "LeadParams",
    "EmosModel",
    "MonthlyClimatology",
    "TrendClimatology",
    "climatology_baseline",
    "trend_climatology_baseline",
    "target_year",
    "time_split",
    "expanding_window_predictions",
    "expanding_window_scores",
    "summarise_scores",
]
