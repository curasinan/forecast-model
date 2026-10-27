"""Scoring shared by the GTM benchmarks: row CRPS, skill, PIT histograms and the summary by key."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from climate.emos import crps_gaussian, crps_skill_score, interval_coverage, pit, pit_histogram

PIT_BINS: int = 10


def summary_columns(key: str, references: Mapping[str, str]) -> list[str]:
    """The summary's columns: model, the key, n, crps, one skill per reference, coverage."""
    return ["model", key, "n", "crps", *references, "cover50", "cover90"]


def row_crps(part: pd.DataFrame, model: str) -> np.ndarray:
    """Per-row CRPS of a model; NaN where the label, mu or sigma is not finite."""
    y = part["y_C"].to_numpy(dtype=float)
    mu = part[f"mu_{model}"].to_numpy(dtype=float)
    sigma = part[f"sigma_{model}"].to_numpy(dtype=float)
    scorable = np.isfinite(y) & np.isfinite(mu) & np.isfinite(sigma)
    crps = np.full(len(part), np.nan)
    crps[scorable] = crps_gaussian(y[scorable], mu[scorable], sigma[scorable])
    return crps


def skill(crps: np.ndarray, reference: np.ndarray | None) -> float:
    """CRPS skill over the rows both have scored; NaN without a reference."""
    return float("nan") if reference is None else crps_skill_score(crps, reference)


def summarise(
    rows: pd.DataFrame,
    models: Sequence[str],
    *,
    key: str,
    key_values: Sequence,
    references: Mapping[str, str],
) -> pd.DataFrame:
    """One row per (model, key value): n, mean CRPS, a skill per reference model, coverage."""
    parts = {value: rows[rows[key] == value] for value in key_values}
    crps = {(name, value): row_crps(parts[value], name) for name in models for value in key_values}
    records = []
    for name in models:
        for value in key_values:
            part, own = parts[value], crps[name, value]
            scored = own[np.isfinite(own)]
            cover = interval_coverage(part["y_C"], part[f"mu_{name}"], part[f"sigma_{name}"])
            record = {
                "model": name,
                key: value,
                "n": len(scored),
                "crps": float(scored.mean()) if len(scored) else float("nan"),
            }
            for column, reference in references.items():
                record[column] = skill(own, crps.get((reference, value)))
            records.append({**record, "cover50": cover[0.5], "cover90": cover[0.9]})
    return pd.DataFrame(records, columns=summary_columns(key, references))


def pit_histograms(rows: pd.DataFrame, models: Sequence[str]) -> dict[str, list[float]]:
    """Per model the PIT histogram frequencies over all rows; NaN where nothing was scorable."""
    histograms = {}
    for name in models:
        values = pit(rows["y_C"], rows[f"mu_{name}"], rows[f"sigma_{name}"])
        histograms[name] = pit_histogram(values, PIT_BINS)["frac"].tolist()
    return histograms
