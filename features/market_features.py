"""Time-series features for Polymarket markets."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src.utils.dates import parse_iso8601

DEFAULT_DB_PATH = Path("data/polymarket.db")

# Lookback windows for momentum / price-history features. Hours.
LOOKBACKS_HOURS: dict[str, int] = {
    "1h": 1,
    "6h": 6,
    "24h": 24,
    "72h": 72,
}

ROUND_LEVELS: tuple[float, ...] = tuple(round(0.1 * k, 2) for k in range(1, 10))

IN_CLAUSE_CHUNK = 500

FEATURE_KEYS: tuple[str, ...] = (
    "market_id",
    "price_now",
    "price_1h_ago",
    "price_6h_ago",
    "price_24h_ago",
    "price_72h_ago",
    "momentum_1h",
    "momentum_24h",
    "momentum_72h",
    "volume_24h",
    "spread_now",
    "spread_avg_24h",
    "spread_widening",
    "bid_depth_now",
    "ask_depth_now",
    "depth_imbalance",
    "time_to_resolution",
    "distance_from_round",
    "snapshot_count",
)


# Public API


def compute_features(
    market_id: str,
    timestamp: datetime,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Compute the feature dict for a single market at ``timestamp``."""
    df = compute_features_batch([market_id], timestamp, db_path)
    if df.empty:
        return {k: (market_id if k == "market_id" else None) for k in FEATURE_KEYS}
    return _row_to_pydict(df.iloc[0])


def compute_features_batch(
    market_ids: Iterable[str],
    timestamp: datetime,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Compute features for many markets at a single timestamp."""
    ids = [str(m) for m in market_ids]
    if not ids:
        return pd.DataFrame(columns=list(FEATURE_KEYS))

    T = _ensure_utc(timestamp)
    max_lookback_h = max(LOOKBACKS_HOURS.values())
    window_start = T - timedelta(hours=max_lookback_h + 96)

    snaps_df = _fetch_snapshots(db_path, ids, window_start, T)
    counts = _fetch_snapshot_counts(db_path, ids, T)
    end_dates = _fetch_end_dates(db_path, ids)

    # Group snapshots by market_id once for O(N) lookup instead of O(N*M).
    by_id = dict(tuple(snaps_df.groupby("market_id"))) if not snaps_df.empty else {}

    rows = [
        _build_feature_row(
            market_id=mid,
            T=T,
            snaps=by_id.get(mid),
            snapshot_count=counts.get(mid, 0),
            end_date_str=end_dates.get(mid),
        )
        for mid in ids
    ]
    return pd.DataFrame(rows, columns=list(FEATURE_KEYS))


# Internals


def _ensure_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _chunked(seq: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _fetch_snapshots(
    db_path: Path | str,
    market_ids: list[str],
    window_start: datetime,
    T: datetime,
) -> pd.DataFrame:
    """Pull every relevant snapshot in (window_start, T] for the given ids."""
    frames: list[pd.DataFrame] = []
    t_iso = T.isoformat()
    ws_iso = window_start.isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for chunk in _chunked(market_ids, IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            query = (
                "SELECT market_id, timestamp, midpoint, spread, bid_depth, "
                "ask_depth, volume_24h "
                "FROM snapshots "
                f"WHERE market_id IN ({placeholders}) "
                "  AND timestamp <= ? "
                "  AND timestamp > ? "
                "ORDER BY market_id, timestamp"
            )
            params = [*chunk, t_iso, ws_iso]
            frames.append(pd.read_sql_query(query, conn, params=params))
    finally:
        conn.close()
    if not frames:
        return pd.DataFrame(
            columns=[
                "market_id", "timestamp", "midpoint", "spread",
                "bid_depth", "ask_depth", "volume_24h",
            ]
        )
    out = pd.concat(frames, ignore_index=True)
    # Vectorized ts parse: every snapshot timestamp is well-formed ISO 8601.
    out["ts"] = pd.to_datetime(out["timestamp"], utc=True, format="ISO8601")
    return out


def _fetch_snapshot_counts(
    db_path: Path | str,
    market_ids: list[str],
    T: datetime,
) -> dict[str, int]:
    """Causal snapshot count (timestamp <= T) per market_id."""
    counts: dict[str, int] = {}
    t_iso = T.isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for chunk in _chunked(market_ids, IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            query = (
                "SELECT market_id, COUNT(*) FROM snapshots "
                f"WHERE market_id IN ({placeholders}) AND timestamp <= ? "
                "GROUP BY market_id"
            )
            for mid, n in conn.execute(query, [*chunk, t_iso]).fetchall():
                counts[str(mid)] = int(n)
    finally:
        conn.close()
    return counts


def _fetch_end_dates(
    db_path: Path | str,
    market_ids: list[str],
) -> dict[str, str | None]:
    end_dates: dict[str, str | None] = {}
    conn = sqlite3.connect(db_path)
    try:
        for chunk in _chunked(market_ids, IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            query = (
                f"SELECT id, end_date FROM markets WHERE id IN ({placeholders})"
            )
            for mid, ed in conn.execute(query, chunk).fetchall():
                end_dates[str(mid)] = ed
    finally:
        conn.close()
    return end_dates


def _build_feature_row(
    *,
    market_id: str,
    T: datetime,
    snaps: pd.DataFrame,
    snapshot_count: int,
    end_date_str: str | None,
) -> dict[str, Any]:
    """Compute one feature dict from this market's slice of the snapshot frame."""
    row: dict[str, Any] = {k: None for k in FEATURE_KEYS}
    row["market_id"] = market_id
    row["snapshot_count"] = int(snapshot_count)

    if end_date_str:
        end_dt = parse_iso8601(end_date_str)
        if end_dt is not None:
            row["time_to_resolution"] = (end_dt - T).total_seconds() / 3600.0

    if snaps is None or len(snaps) == 0:
        return row

    g = snaps.sort_values("ts")
    le_T = g[g["ts"] <= T]
    if not le_T.empty:
        latest = le_T.iloc[-1]
        row["price_now"] = _maybe_float(latest["midpoint"])
        row["spread_now"] = _maybe_float(latest["spread"])
        row["bid_depth_now"] = _maybe_float(latest["bid_depth"])
        row["ask_depth_now"] = _maybe_float(latest["ask_depth"])
        row["volume_24h"] = _maybe_float(latest["volume_24h"])

    # Lookback prices.
    for label, hours in LOOKBACKS_HOURS.items():
        cutoff = T - timedelta(hours=hours)
        past = g[g["ts"] <= cutoff]
        if not past.empty:
            row[f"price_{label}_ago"] = _maybe_float(past.iloc[-1]["midpoint"])

    # Momentum (only the requested 1h / 24h / 72h, not 6h).
    for label in ("1h", "24h", "72h"):
        p_now = row["price_now"]
        p_past = row[f"price_{label}_ago"]
        if p_now is not None and p_past is not None:
            row[f"momentum_{label}"] = p_now - p_past

    # spread_avg_24h: mean over (T - 24h, T] ignoring nulls.
    twenty_four_ago = T - timedelta(hours=24)
    recent = g[(g["ts"] > twenty_four_ago) & (g["ts"] <= T)]
    spreads = recent["spread"].dropna()
    if not spreads.empty:
        row["spread_avg_24h"] = float(spreads.mean())

    if row["spread_now"] is not None and row["spread_avg_24h"] is not None:
        row["spread_widening"] = row["spread_now"] - row["spread_avg_24h"]

    # Depth imbalance.
    bid = row["bid_depth_now"]
    ask = row["ask_depth_now"]
    if bid is not None and ask is not None and (bid + ask) > 0:
        row["depth_imbalance"] = (bid - ask) / (bid + ask)

    # Distance to nearest round level (0.10, 0.20, ..., 0.90).
    if row["price_now"] is not None:
        row["distance_from_round"] = min(
            abs(row["price_now"] - level) for level in ROUND_LEVELS
        )

    return row


def _maybe_float(v: Any) -> float | None:
    """Convert pandas/numpy scalars to plain float; preserve null-ness."""
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
    """Convert a feature DataFrame row back to a clean Python dict."""
    out: dict[str, Any] = {}
    for k, v in series.items():
        if k == "market_id" or k == "snapshot_count":
            out[k] = v if not (isinstance(v, float) and pd.isna(v)) else None
            if k == "snapshot_count" and out[k] is not None:
                out[k] = int(out[k])
            continue
        out[k] = _maybe_float(v)
    return out
