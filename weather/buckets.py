"""Truncation-aware temperature bucket probabilities."""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm


def truncate_whole_degree(value: float) -> int:
    """Polymarket weather: resolve to whole degrees (truncation toward -inf for positives)."""
    return math.floor(value)


@dataclass(frozen=True)
class Bucket:
    lo: float | None  # inclusive continuous lower (None = -inf)
    hi: float | None  # exclusive continuous upper (None = +inf)
    label: str = ""

    def contains_truncated(self, temp: float) -> bool:
        t = float(truncate_whole_degree(temp))
        if self.lo is not None and t < self.lo:
            return False
        if self.hi is not None and t >= self.hi:
            return False
        if self.hi is not None and self.lo is not None:
            return self.lo <= t < self.hi
        if self.lo is not None and self.hi is None:
            return t >= self.lo
        if self.lo is None and self.hi is not None:
            return t < self.hi
        return True


def p_bucket_gaussian(mu: float, sigma: float, bucket: Bucket) -> float:
    """P(T in bucket) under Normal(mu, sigma^2) in continuous space matching bucket edges."""
    if sigma <= 0 or not math.isfinite(sigma):
        # Degenerate: point mass at mu
        return 1.0 if bucket.contains_truncated(mu) else 0.0
    lo = -math.inf if bucket.lo is None else float(bucket.lo)
    hi = math.inf if bucket.hi is None else float(bucket.hi)
    return float(max(0.0, min(1.0, norm.cdf(hi, loc=mu, scale=sigma) - norm.cdf(lo, loc=mu, scale=sigma))))


def p_above_gaussian(mu: float, sigma: float, threshold: float) -> float:
    """P(trunc(T) >= threshold) ≈ P(T >= threshold) for continuous Normal."""
    if sigma <= 0:
        return 1.0 if truncate_whole_degree(mu) >= threshold else 0.0
    return float(1.0 - norm.cdf(threshold, loc=mu, scale=sigma))


def p_bucket_from_members(members: list[float], bucket: Bucket) -> float:
    if not members:
        return float("nan")
    hits = sum(1 for m in members if bucket.contains_truncated(m))
    return hits / len(members)


def p_bucket_from_quantiles(
    quantile_levels: list[float],
    quantile_values: list[float],
    bucket: Bucket,
) -> float:
    """Linear CDF interpolation from quantile pairs (τ, qτ)."""
    if len(quantile_levels) != len(quantile_values) or len(quantile_levels) < 2:
        return float("nan")
    pairs = sorted(zip(quantile_values, quantile_levels), key=lambda x: x[0])
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    def cdf(x: float) -> float:
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        for i in range(1, len(xs)):
            if x <= xs[i]:
                w = (x - xs[i - 1]) / (xs[i] - xs[i - 1] + 1e-12)
                return ys[i - 1] + w * (ys[i] - ys[i - 1])
        return ys[-1]

    lo = -1e9 if bucket.lo is None else float(bucket.lo)
    hi = 1e9 if bucket.hi is None else float(bucket.hi)
    return float(max(0.0, min(1.0, cdf(hi) - cdf(lo))))
