"""Cluster / correlation features for prediction markets."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

import numpy as np
import pandas as pd

from features.market_features import compute_features_batch

DEFAULT_DB_PATH = Path("data/polymarket.db")
DEFAULT_CONFIG_PATH = Path("config/market_clusters.json")

CORRELATION_FEATURE_KEYS: tuple[str, ...] = (
    "market_id",
    "cluster_id",
    "cluster_size",
    "cluster_avg_price",
    "cluster_price_dispersion",
    "cluster_avg_momentum_1h",
    "own_vs_cluster_momentum",
    "cluster_lead_signal",
)


# Public API


def load_clusters(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, list[str]]:
    """Load cluster definitions from JSON."""
    p = Path(config_path)
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(
            f"{p}: expected a JSON object mapping cluster_name -> [market_ids]"
        )
    out: dict[str, list[str]] = {}
    for name, members in raw.items():
        if not isinstance(members, list):
            continue
        out[str(name)] = [str(mid) for mid in members]
    return out


def compute_correlation_features(
    market_id: str,
    timestamp: datetime,
    db_path: Path | str = DEFAULT_DB_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Single-market wrapper over :func:`compute_correlation_features_batch`."""
    df = compute_correlation_features_batch(
        [market_id], timestamp, db_path, config_path
    )
    if df.empty:
        return {
            k: (market_id if k == "market_id" else None)
            for k in CORRELATION_FEATURE_KEYS
        }
    return _row_to_pydict(df.iloc[0])


def compute_correlation_features_batch(
    market_ids: Iterable[str],
    timestamp: datetime,
    db_path: Path | str = DEFAULT_DB_PATH,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
) -> pd.DataFrame:
    """Compute cluster features for a list of markets at a single timestamp."""
    ids = [str(m) for m in market_ids]
    if not ids:
        return pd.DataFrame(columns=list(CORRELATION_FEATURE_KEYS))

    clusters = load_clusters(config_path)
    market_to_cluster = _build_market_to_cluster(clusters)

    # Build the union of market_ids whose features we need.
    needed: set[str] = set(ids)
    for mid in ids:
        cname = market_to_cluster.get(mid)
        if cname is not None:
            needed.update(clusters[cname])

    features_df = compute_features_batch(sorted(needed), timestamp, db_path)
    # Index for O(1) lookup. compute_features_batch returns one row per id.
    features_indexed = features_df.set_index("market_id")

    rows: list[dict[str, Any]] = []
    for mid in ids:
        rows.append(
            _build_correlation_row(
                market_id=mid,
                cluster_name=market_to_cluster.get(mid),
                clusters=clusters,
                features=features_indexed,
            )
        )
    return pd.DataFrame(rows, columns=list(CORRELATION_FEATURE_KEYS))


# Internals


def _build_market_to_cluster(
    clusters: dict[str, list[str]],
) -> dict[str, str]:
    """Build a market_id -> cluster_name reverse lookup."""
    rev: dict[str, str] = {}
    for cname, members in clusters.items():
        for mid in members:
            rev[str(mid)] = cname
    return rev


def _build_correlation_row(
    *,
    market_id: str,
    cluster_name: str | None,
    clusters: dict[str, list[str]],
    features: pd.DataFrame,
) -> dict[str, Any]:
    base: dict[str, Any] = {k: None for k in CORRELATION_FEATURE_KEYS}
    base["market_id"] = market_id

    if cluster_name is None:
        return base

    members = clusters[cluster_name]
    base["cluster_id"] = cluster_name
    base["cluster_size"] = len(members)

    peer_ids = [m for m in members if m != market_id]
    if not peer_ids:
        return base

    peer_prices = _collect_floats(features, peer_ids, "price_now")
    peer_momenta = _collect_floats(features, peer_ids, "momentum_1h")

    if peer_prices:
        base["cluster_avg_price"] = float(fmean(peer_prices))
        if len(peer_prices) == 1:
            base["cluster_price_dispersion"] = 0.0
        else:
            # Sample std (ddof=1). Numpy handles single-value via 0 above.
            base["cluster_price_dispersion"] = float(
                np.std(peer_prices, ddof=1)
            )

    if peer_momenta:
        avg_mom = float(fmean(peer_momenta))
        base["cluster_avg_momentum_1h"] = avg_mom
        own_mom = _safe_lookup(features, market_id, "momentum_1h")
        if own_mom is not None:
            base["own_vs_cluster_momentum"] = own_mom - avg_mom
        base["cluster_lead_signal"] = float(max(abs(m) for m in peer_momenta))

    return base


def _collect_floats(
    features: pd.DataFrame,
    market_ids: Iterable[str],
    column: str,
) -> list[float]:
    """Return non-null float values for ``column`` across the given ids."""
    out: list[float] = []
    for mid in market_ids:
        v = _safe_lookup(features, mid, column)
        if v is not None:
            out.append(v)
    return out


def _safe_lookup(features: pd.DataFrame, market_id: str, column: str) -> float | None:
    """Pull ``features.loc[market_id, column]`` as a plain float or None."""
    try:
        v = features.at[market_id, column]
    except KeyError:
        return None
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row_to_pydict(series: pd.Series) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in series.items():
        if k == "market_id" or k == "cluster_id":
            if isinstance(v, float) and pd.isna(v):
                out[k] = None
            else:
                out[k] = v
            continue
        if k == "cluster_size":
            if v is None or (isinstance(v, float) and pd.isna(v)):
                out[k] = None
            else:
                out[k] = int(v)
            continue
        if v is None:
            out[k] = None
            continue
        try:
            if pd.isna(v):
                out[k] = None
                continue
        except (TypeError, ValueError):
            pass
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = None
    return out
