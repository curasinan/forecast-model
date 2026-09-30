"""Label / target-variable generation for model training.

Three public functions:

* ``generate_outcome_labels`` — final ground-truth YES/NO labels from
  resolved markets. Pure backward-looking lookup against ``markets.resolution``.
* ``generate_price_movement_labels`` — forward-looking price-change labels
  for short-horizon targets (1h / 6h / 24h by default). **Look ahead in time.
  TRAINING USE ONLY.**
* ``build_training_dataset`` — the full pipeline that joins market features,
  correlation features, and price-movement labels into one wide DataFrame
  ready for an ML trainer.

Forward-looking warning
-----------------------
``generate_price_movement_labels`` and ``build_training_dataset`` BOTH look
forward in time relative to the supplied timestamp. They are appropriate
exclusively for offline training-set construction. Calling them at inference
time leaks future prices into the prediction pipeline and will silently make
your model's apparent accuracy meaningless. A prominent runtime check is
deliberately not added — the docstring is the contract — but every call site
should be auditable as "training only".
"""

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
DIRECTION_TOLERANCE = 0.005   # |Δprice| under this counts as "no move" (direction=0)
NULL_FRACTION_DROP = 0.5      # rows with >50% null feature columns are skipped
IN_CLAUSE_CHUNK = 500


# --------------------------------------------------------------------------- #
# Function 1: outcome labels
# --------------------------------------------------------------------------- #


def generate_outcome_labels(
    market_ids: Iterable[str],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Return ground-truth YES/NO outcomes for resolved markets.

    Filters the input ``market_ids`` to those with ``status='resolved_or_removed'``
    AND a non-null ``resolution`` field. Resolution strings are normalized
    (case-insensitive) to a binary outcome:

    * 1 → resolution looks like "yes" / "y" / "1" / "1.0" / "true"
    * 0 → resolution looks like "no" / "n" / "0" / "0.0" / "false"

    Markets whose resolution is non-binary or unparseable (e.g. "invalid",
    free-text disputes, partial-resolution payouts) are silently dropped from
    the output — there's nothing useful to feed a binary classifier.

    Returns
    -------
    DataFrame with two columns:
      * ``market_id`` (str)
      * ``outcome``   (int: 0 or 1)
    """
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


# --------------------------------------------------------------------------- #
# Function 2: price movement labels
# --------------------------------------------------------------------------- #


def generate_price_movement_labels(
    market_ids: Iterable[str],
    timestamp: datetime,
    horizons: Sequence[float] = DEFAULT_HORIZONS_HOURS,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Forward-looking price-movement labels for training.

    *** TRAINING-ONLY — DO NOT CALL DURING LIVE PREDICTION ***

    Given a timestamp ``T`` and a list of horizons in hours, this function
    computes the change in midpoint between ``T`` and ``T + horizon`` for
    every (market_id, horizon) pair. **Both endpoints look at the closest
    snapshot within ±30 minutes of the target time** (so even ``price_at_t``
    can come from a snapshot slightly later than ``T`` if that's the
    closest available). This is intentional and consistent on both ends — it
    means ``price_change`` reflects the actual nearest-known prices around
    the two reference moments, regardless of collection cadence.

    Because at least one endpoint looks **forward in time**, this function
    must NEVER be called at inference time. It exists exclusively to build
    target variables for offline training-set construction.

    Parameters
    ----------
    market_ids : iterable of str
    timestamp : datetime
        Reference time T. Naive datetimes are coerced to UTC.
    horizons : sequence of float
        Hours to look forward, e.g. ``(1, 6, 24)``.
    db_path : Path or str

    Returns
    -------
    DataFrame with columns:
      * ``market_id``        (str)
      * ``timestamp``        (str, ISO 8601 UTC of T)
      * ``horizon_hours``    (float)
      * ``price_at_t``       (float | None)
      * ``price_at_t_plus_h``(float | None)
      * ``price_change``     (float | None)
      * ``direction``        (int | None: -1 / 0 / +1, with |Δ|<0.005 → 0)

    Long-form: one row per (market_id × horizon).
    """
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


# --------------------------------------------------------------------------- #
# Function 3: build training dataset
# --------------------------------------------------------------------------- #


def build_training_dataset(
    market_ids: Iterable[str],
    timestamps: Iterable[datetime],
    horizons: Sequence[float] = DEFAULT_HORIZONS_HOURS,
    db_path: Path | str = DEFAULT_DB_PATH,
    cluster_config_path: Path | str = DEFAULT_CLUSTER_CONFIG_PATH,
) -> pd.DataFrame:
    """Build a wide training-row DataFrame for the cross-product of (markets × timestamps).

    *** TRAINING-ONLY — uses generate_price_movement_labels under the hood,
    which looks forward in time. Do not call from a live-prediction path. ***

    For every (market_id, T) pair this computes:

    1. Market features at T (via :func:`compute_features_batch`)
    2. Correlation features at T (via :func:`compute_correlation_features_batch`)
    3. Price-movement labels at T for each horizon (via
       :func:`generate_price_movement_labels`), pivoted to wide form so each
       horizon contributes its own column suffix.

    The three frames are joined on ``market_id`` per timestamp, then stacked.
    Rows whose **feature** columns are >50% null are dropped (label columns
    are never counted toward the null fraction — labels can legitimately be
    missing for the most recent timestamps without invalidating the row's
    feature signal).

    Output columns (for default horizons):

    * Identifiers: ``market_id``, ``timestamp``
    * Market features: ``price_now``, ``price_1h_ago``, …, ``snapshot_count``
    * Correlation features: ``cluster_id``, ``cluster_size``, …, ``cluster_lead_signal``
    * Labels per horizon: ``price_at_t``,
      ``price_at_t_plus_<h>h``, ``price_change_<h>h``, ``direction_<h>h``
    """
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


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


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
    """Return the midpoint of the snapshot whose ``ts`` is closest to ``target``,
    provided it falls within ±half_window of target. Otherwise None.
    """
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
    """Pivot the long-form labels frame into one column per horizon.

    Output columns: market_id, price_at_t, plus three columns per horizon
    (price_at_t_plus_<H>, price_change_<H>, direction_<H>).
    """
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
    """Return the subset of columns that count as 'features' for the
    null-fraction drop rule.

    Excluded:
      * Identifiers (``market_id``, ``timestamp``)
      * Label columns (``price_at_t``, and all per-horizon label suffixes)
    """
    cols = list(columns)
    excluded: set[str] = {"market_id", "timestamp", "price_at_t"}
    for h in horizons:
        sfx = _horizon_suffix(h)
        excluded.add(f"price_at_t_plus_{sfx}")
        excluded.add(f"price_change_{sfx}")
        excluded.add(f"direction_{sfx}")
    return [c for c in cols if c not in excluded]
