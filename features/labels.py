"""Label / target-variable generation for model training."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from features.correlation_features import (
    compute_correlation_features_batch,
)
from features.market_features import (
    compute_features_batch,
)

DEFAULT_DB_PATH = Path("data/polymarket.db")
DEFAULT_CLUSTER_CONFIG_PATH = Path("config/market_clusters.json")
DEFAULT_HORIZONS_HOURS: tuple[float, ...] = (1.0, 6.0, 24.0)
SNAPSHOT_WINDOW_MINUTES = 30  # half-window around T and T+h
DIRECTION_TOLERANCE = 0.005
NULL_FRACTION_DROP = 0.5      # rows with >50% null feature columns are skipped
IN_CLAUSE_CHUNK = 500


# Function 1: outcome labels


def generate_outcome_labels(
    market_ids: Iterable[str],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Return ground-truth YES/NO outcomes for resolved markets."""
    ids = [str(m) for m in market_ids]
    if not ids:
        return pd.DataFrame(columns=["market_id", "outcome"])

    rows: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path)
    try:
        for chunk in _chunked(ids, IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            query = (
                "SELECT id, resolution FROM markets "
                f"WHERE id IN ({placeholders}) "
                "  AND status = 'resolved_or_removed' "
                "  AND resolution IS NOT NULL "
                "  AND resolution != ''"
            )
            for mid, resolution in conn.execute(query, chunk).fetchall():
                outcome = _normalize_resolution(resolution)
                if outcome is None:
                    continue
                rows.append({"market_id": str(mid), "outcome": int(outcome)})
    finally:
        conn.close()

    return pd.DataFrame(rows, columns=["market_id", "outcome"])


# Function 2: price movement labels


def generate_price_movement_labels(
    market_ids: Iterable[str],
    timestamp: datetime,
    horizons: Sequence[float] = DEFAULT_HORIZONS_HOURS,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Forward-looking price-movement labels for training."""
    ids = [str(m) for m in market_ids]
    horizons_list = [float(h) for h in horizons]
    if not ids or not horizons_list:
        return pd.DataFrame(columns=[
            "market_id", "timestamp", "horizon_hours",
            "price_at_t", "price_at_t_plus_h", "price_change", "direction",
        ])

    T = _ensure_utc(timestamp)
    half_window = timedelta(minutes=SNAPSHOT_WINDOW_MINUTES)
    max_h = max(horizons_list)

    # Pull every snapshot in [T - 30min, T + max_h + 30min] for the markets.
    fetch_lo = T - half_window
    fetch_hi = T + timedelta(hours=max_h) + half_window
    snaps_df = _fetch_snapshots_window(db_path, ids, fetch_lo, fetch_hi)

    # Group once for O(1) lookup per market.
    by_id: dict[str, pd.DataFrame] = (
        dict(tuple(snaps_df.groupby("market_id"))) if not snaps_df.empty else {}
    )

    rows: list[dict[str, Any]] = []
    t_iso = T.isoformat()
    for mid in ids:
        g = by_id.get(mid)
        price_at_t = _closest_within_window(g, T, half_window)
        for h in horizons_list:
            target = T + timedelta(hours=h)
            price_at_t_plus_h = _closest_within_window(g, target, half_window)
            if price_at_t is None or price_at_t_plus_h is None:
                price_change: float | None = None
                direction: int | None = None
            else:
                price_change = price_at_t_plus_h - price_at_t
                if price_change > DIRECTION_TOLERANCE:
                    direction = 1
                elif price_change < -DIRECTION_TOLERANCE:
                    direction = -1
                else:
                    direction = 0
            rows.append({
                "market_id": mid,
                "timestamp": t_iso,
                "horizon_hours": h,
                "price_at_t": price_at_t,
                "price_at_t_plus_h": price_at_t_plus_h,
                "price_change": price_change,
                "direction": direction,
            })
    return pd.DataFrame(rows, columns=[
        "market_id", "timestamp", "horizon_hours",
        "price_at_t", "price_at_t_plus_h", "price_change", "direction",
    ])


# Function 3: build training dataset


def build_training_dataset(
    market_ids: Iterable[str],
    timestamps: Iterable[datetime],
    horizons: Sequence[float] = DEFAULT_HORIZONS_HOURS,
    db_path: Path | str = DEFAULT_DB_PATH,
    cluster_config_path: Path | str = DEFAULT_CLUSTER_CONFIG_PATH,
) -> pd.DataFrame:
    """Build a wide training-row DataFrame for the cross-product of (markets × timestamps)."""
    ids = [str(m) for m in market_ids]
    ts_list = [_ensure_utc(t) for t in timestamps]
    horizons_list = [float(h) for h in horizons]
    if not ids or not ts_list:
        return pd.DataFrame()

    pieces: list[pd.DataFrame] = []
    for T in ts_list:
        market_feats = compute_features_batch(ids, T, db_path)
        corr_feats = compute_correlation_features_batch(
            ids, T, db_path, cluster_config_path
        )
        labels_long = generate_price_movement_labels(
            ids, T, horizons_list, db_path
        )
        labels_wide = _pivot_labels_to_wide(labels_long, horizons_list)

        # Drop the duplicated 'market_id' columns on the right of each merge.
        merged = market_feats.merge(
            corr_feats.drop(columns=[]), on="market_id", how="left"
        ).merge(labels_wide, on="market_id", how="left")
        merged.insert(1, "timestamp", T.isoformat())
        pieces.append(merged)

    full = pd.concat(pieces, ignore_index=True)

    # Filter: drop rows whose FEATURE columns are >50% null.
    feature_cols = _feature_columns(full.columns, horizons_list)
    if feature_cols:
        null_fraction = full[feature_cols].isna().mean(axis=1)
        full = full[null_fraction <= NULL_FRACTION_DROP].reset_index(drop=True)
    return full


# Internals


def _ensure_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _chunked(seq: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _normalize_resolution(value: Any) -> int | None:
    """Convert a Polymarket-style resolution string to 0/1, or None."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in {"yes", "y", "1", "1.0", "true"}:
        return 1
    if s in {"no", "n", "0", "0.0", "false"}:
        return 0
    # Try numeric — Polymarket sometimes stores 0.0 / 1.0 as floats.
    try:
        f = float(s)
    except ValueError:
        return None
    if f == 1.0:
        return 1
    if f == 0.0:
        return 0
    return None


def _fetch_snapshots_window(
    db_path: Path | str,
    market_ids: list[str],
    lo: datetime,
    hi: datetime,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    lo_iso = lo.isoformat()
    hi_iso = hi.isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for chunk in _chunked(market_ids, IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            query = (
                "SELECT market_id, timestamp, midpoint "
                "FROM snapshots "
                f"WHERE market_id IN ({placeholders}) "
                "  AND timestamp >= ? AND timestamp <= ? "
                "  AND midpoint IS NOT NULL "
                "ORDER BY market_id, timestamp"
            )
            params = [*chunk, lo_iso, hi_iso]
            frames.append(pd.read_sql_query(query, conn, params=params))
    finally:
        conn.close()
    if not frames:
        return pd.DataFrame(columns=["market_id", "timestamp", "midpoint"])
    out = pd.concat(frames, ignore_index=True)
    out["ts"] = pd.to_datetime(out["timestamp"], utc=True, format="ISO8601")
    return out


def _closest_within_window(
    g: pd.DataFrame | None,
    target: datetime,
    half_window: timedelta,
) -> float | None:
    """Midpoint of the snapshot closest to ``target`` within ±half_window, else None."""
    if g is None or g.empty:
        return None
    deltas = (g["ts"] - target).abs()
    in_window = deltas <= half_window
    if not in_window.any():
        return None
    idx = deltas[in_window].idxmin()
    v = g.loc[idx, "midpoint"]
    if v is None or pd.isna(v):
        return None
    return float(v)


def _horizon_suffix(h: float) -> str:
    """Render 1.0 -> '1h', 1.5 -> '1.5h', 24.0 -> '24h'."""
    return f"{h:g}h"


def _pivot_labels_to_wide(
    long_df: pd.DataFrame,
    horizons: Sequence[float],
) -> pd.DataFrame:
    """Pivot the long-form labels frame into one column per horizon."""
    if long_df.empty:
        cols = ["market_id", "price_at_t"]
        for h in horizons:
            sfx = _horizon_suffix(h)
            cols.extend([f"price_at_t_plus_{sfx}", f"price_change_{sfx}", f"direction_{sfx}"])
        return pd.DataFrame(columns=cols)

    base = (
        long_df[["market_id", "price_at_t"]]
        .drop_duplicates(subset=["market_id"], keep="first")
        .reset_index(drop=True)
    )
    wide = base
    for h in horizons:
        sfx = _horizon_suffix(h)
        sub = (
            long_df[long_df["horizon_hours"] == float(h)]
            [["market_id", "price_at_t_plus_h", "price_change", "direction"]]
            .rename(columns={
                "price_at_t_plus_h": f"price_at_t_plus_{sfx}",
                "price_change": f"price_change_{sfx}",
                "direction": f"direction_{sfx}",
            })
        )
        wide = wide.merge(sub, on="market_id", how="left")
    return wide


def _feature_columns(
    columns: Iterable[str],
    horizons: Sequence[float],
) -> list[str]:
    """Return the subset of columns that count as 'features' for the null-fraction drop rule."""
    cols = list(columns)
    excluded: set[str] = {"market_id", "timestamp", "price_at_t"}
    for h in horizons:
        sfx = _horizon_suffix(h)
        excluded.add(f"price_at_t_plus_{sfx}")
        excluded.add(f"price_change_{sfx}")
        excluded.add(f"direction_{sfx}")
    return [c for c in cols if c not in excluded]
