"""Market-implied distributions from a GTM strike ladder."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.optimize import least_squares
from scipy.stats import norm

from climate.ladder import MAX_SANE_SPREAD

MIN_SIGMA = 0.01
MAX_SIGMA = 2.0
CEILING_BOUNDS = (0.80, 1.0)
PLATEAU_MID = 0.85  # a ceiling is fitted only when at least two mids reach this
INTERIOR = (0.10, 0.90)  # normalised mids inside this band pin mu and sigma
MIN_INTERIOR = 2
MIN_QUOTES = 3


@dataclass(frozen=True)
class ImpliedFit:
    """N(mu, sigma) scaled by `ceiling`: the YES mid at strike k is ceiling * P(Y > k).

    The ceiling absorbs the discount at which near-certain strikes trade (about
    0.93 for a contract three months out); dividing a mid by it gives the
    market's probability. `identified` is False when fewer than two quotes sit
    inside the informative band, where mu and sigma trade off against each other.
    """

    mu: float
    sigma: float
    ceiling: float
    ceiling_fitted: bool
    n_quotes: int
    n_interior: int
    rmse: float
    identified: bool

    def survival(self, strike) -> np.ndarray:
        """The market's P(Y > strike), the ceiling taken out."""
        return norm.sf((np.asarray(strike, dtype=float) - self.mu) / self.sigma)

    def to_dict(self) -> dict:
        return asdict(self)


def usable_quotes(bid, ask, max_spread: float = MAX_SANE_SPREAD) -> np.ndarray:
    """Two-sided, uncrossed quotes no wider than `max_spread` (the ladder's suspect rule)."""
    b = np.asarray(bid, dtype=float)
    a = np.asarray(ask, dtype=float)
    with np.errstate(invalid="ignore"):
        return np.isfinite(b) & np.isfinite(a) & (a >= b) & ((a - b) <= max_spread) & (a > 0)


def fit_implied(strikes, bid, ask, *, max_spread: float = MAX_SANE_SPREAD) -> ImpliedFit | None:
    """Fit the ladder's mids by weighted least squares; None with fewer than three usable quotes.

    The ceiling is fitted when at least two mids reach PLATEAU_MID, and is 1.0
    otherwise. Reading a plateau at 0.93 as probabilities with a ceiling of 1.0
    is not offered: the fit then explains the plateau with a huge sigma and
    looks well identified while saying nothing. Each quote weighs 1 / spread,
    floored at one tick.
    """
    k_all = np.asarray(strikes, dtype=float)
    ok = usable_quotes(bid, ask, max_spread) & np.isfinite(k_all)
    if int(ok.sum()) < MIN_QUOTES:
        return None
    k = k_all[ok]
    b = np.asarray(bid, dtype=float)[ok]
    a = np.asarray(ask, dtype=float)[ok]
    mid = (a + b) / 2.0
    w = 1.0 / np.maximum(a - b, 0.01)

    fit_ceiling = int((mid >= PLATEAU_MID).sum()) >= 2
    u0 = float(np.clip(mid.max(), *CEILING_BOUNDS)) if fit_ceiling else 1.0
    mu0 = float(k[np.argmin(np.abs(mid / u0 - 0.5))])
    sigma0 = 0.15

    def unpack(x):
        mu, log_sigma = x[0], x[1]
        return mu, np.exp(log_sigma), (x[2] if fit_ceiling else 1.0)

    def residual(x):
        mu, sigma, u = unpack(x)
        return w * (u * norm.sf((k - mu) / sigma) - mid)

    lo = [k.min() - 2.0, np.log(MIN_SIGMA)]
    hi = [k.max() + 2.0, np.log(MAX_SIGMA)]
    x0 = [mu0, np.log(sigma0)]
    if fit_ceiling:
        lo.append(CEILING_BOUNDS[0])
        hi.append(CEILING_BOUNDS[1])
        x0.append(u0)
    x0 = np.clip(x0, np.asarray(lo) + 1e-9, np.asarray(hi) - 1e-9)
    res = least_squares(residual, x0, bounds=(lo, hi), method="trf")
    mu, sigma, u = unpack(res.x)

    fitted = u * norm.sf((k - mu) / sigma)
    normalised = mid / u
    n_interior = int(((normalised > INTERIOR[0]) & (normalised < INTERIOR[1])).sum())
    return ImpliedFit(
        mu=float(mu), sigma=float(sigma), ceiling=float(u), ceiling_fitted=fit_ceiling,
        n_quotes=int(ok.sum()), n_interior=n_interior,
        rmse=float(np.sqrt(np.mean((fitted - mid) ** 2))),
        identified=n_interior >= MIN_INTERIOR,
    )


def brier(prob, outcome) -> float:
    """Mean squared error of probabilities against 0/1 outcomes, NaNs skipped."""
    p = np.asarray(prob, dtype=float)
    o = np.asarray(outcome, dtype=float)
    ok = np.isfinite(p) & np.isfinite(o)
    if not ok.any():
        return float("nan")
    return float(np.mean((p[ok] - o[ok]) ** 2))


__all__ = ["ImpliedFit", "brier", "fit_implied", "usable_quotes"]
