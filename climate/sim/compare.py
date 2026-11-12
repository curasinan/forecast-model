"""S1: the model's distribution for a print against the market's, snapshot by snapshot."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
from scipy.stats import norm

from climate.emos import crps_gaussian
from climate.sim.book import PRINT_RESOLUTION
from climate.sim.implied import brier, fit_implied, usable_quotes
from climate.storage import VINTAGE_LIVE

COMPARE_COLUMNS = [
    "snapshot_utc", "target_month", "init", "lead_months", "running_month",
    "model_mu", "model_sigma",
    "market_mu", "market_sigma", "market_ceiling", "market_rmse",
    "n_quotes", "n_interior", "identified",
    "mu_gap", "sigma_ratio", "pivot_strike", "p_model_pivot", "p_market_pivot",
    "label", "label_vintage",
    "crps_model", "crps_market", "crps_diff",
    "brier_model", "brier_market", "brier_market_raw", "n_brier",
]


def first_prints(ledger: pd.DataFrame, series: str = "gtm") -> dict[str, tuple[float, str]]:
    """period -> (settling value, vintage): the first non-revision row, a live first print first."""
    rows = ledger[(ledger["series"] == series) & (~ledger["is_revision"].astype(bool))]
    out: dict[str, tuple[float, str]] = {}
    for period, group in rows.groupby("period", sort=False):
        live = group[group["vintage"] == VINTAGE_LIVE]
        pick = (live if not live.empty else group).iloc[0]
        out[str(period)] = (float(pick["value"]), str(pick["vintage"]))
    return out


def _lead_months(snapshot: pd.Timestamp, target_month: str) -> int:
    t = pd.Period(target_month, freq="M")
    s = pd.Period(snapshot.tz_convert("UTC").tz_localize(None), freq="M")
    return int((t - s).n)


def compare_snapshots(ladders: pd.DataFrame,
                      labels: Mapping[str, tuple[float, str]] | None = None, *,
                      resolution: float = PRINT_RESOLUTION) -> pd.DataFrame:
    """One row per (snapshot, target month) with a usable ladder and a model forecast.

    The market side is the ceiling-removed implied Gaussian; Brier is scored
    on the usable strikes only, with the market's mids divided by the ceiling
    (brier_market) and as they stand (brier_market_raw). A label is the
    settling value of the month (`first_prints`), when it is known.
    """
    labels = labels or {}
    rows = []
    for (snapshot, target), g in ladders.groupby(["snapshot_utc", "target_month"], sort=True):
        mu_m, sd_m = float(g["model_mu"].iloc[0]), float(g["model_sigma"].iloc[0])
        if not (np.isfinite(mu_m) and np.isfinite(sd_m) and sd_m > 0):
            continue
        fit = fit_implied(g["strike"], g["bid"], g["ask"])
        if fit is None:
            continue
        lead = _lead_months(snapshot, target)
        usable = g[usable_quotes(g["bid"], g["ask"])]
        pivot = float(usable["strike"].iloc[np.argmin(np.abs(usable["strike"] - mu_m))])
        row = {
            "snapshot_utc": snapshot, "target_month": target, "init": g["init"].iloc[0],
            "lead_months": lead, "running_month": lead == 0,
            "model_mu": mu_m, "model_sigma": sd_m,
            "market_mu": fit.mu, "market_sigma": fit.sigma, "market_ceiling": fit.ceiling,
            "market_rmse": fit.rmse, "n_quotes": fit.n_quotes, "n_interior": fit.n_interior,
            "identified": fit.identified,
            "mu_gap": mu_m - fit.mu, "sigma_ratio": sd_m / fit.sigma,
            "pivot_strike": pivot,
            "p_model_pivot": float(_p_over(pivot, mu_m, sd_m, resolution)),
            "p_market_pivot": float(_p_over(pivot, fit.mu, fit.sigma, resolution)),
            "label": np.nan, "label_vintage": None,
            "crps_model": np.nan, "crps_market": np.nan, "crps_diff": np.nan,
            "brier_model": np.nan, "brier_market": np.nan, "brier_market_raw": np.nan,
            "n_brier": 0,
        }
        if target in labels:
            y, vintage = labels[target]
            k = usable["strike"].to_numpy(dtype=float)
            won = (y > k + 1e-12).astype(float)
            mid = ((usable["bid"] + usable["ask"]) / 2.0).to_numpy(dtype=float)
            row.update({
                "label": y, "label_vintage": vintage,
                "crps_model": float(crps_gaussian(y, mu_m, sd_m)),
                "crps_market": float(crps_gaussian(y, fit.mu, fit.sigma)),
                "brier_model": brier(_p_over(k, mu_m, sd_m, resolution), won),
                "brier_market": brier(np.clip(mid / fit.ceiling, 0.0, 1.0), won),
                "brier_market_raw": brier(mid, won),
                "n_brier": int(len(k)),
            })
            row["crps_diff"] = row["crps_model"] - row["crps_market"]
        rows.append(row)
    return pd.DataFrame(rows, columns=COMPARE_COLUMNS)


def _p_over(strike, mu: float, sigma: float, resolution: float):
    """P(print > strike) under N(mu, sigma) with prints rounded to `resolution`."""
    return norm.sf((np.asarray(strike, dtype=float) + resolution / 2.0 - mu) / sigma)


def print_level_summary(compared: pd.DataFrame) -> pd.DataFrame:
    """Per settled target month: mean CRPS of each side over its snapshots, and who won."""
    scored = compared[np.isfinite(compared["crps_diff"]) & compared["identified"]]
    if scored.empty:
        return pd.DataFrame(columns=["target_month", "n_snapshots", "crps_model", "crps_market",
                                     "crps_diff", "model_better"])
    out = scored.groupby("target_month").agg(
        n_snapshots=("crps_diff", "size"), crps_model=("crps_model", "mean"),
        crps_market=("crps_market", "mean"), crps_diff=("crps_diff", "mean"),
    ).reset_index()
    out["model_better"] = out["crps_diff"] < 0
    return out


__all__ = ["COMPARE_COLUMNS", "compare_snapshots", "first_prints", "print_level_summary"]
