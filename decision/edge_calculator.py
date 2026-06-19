"""Convert model predictions into maker-adjusted edge signals.

For each market the latest snapshot's best_bid / best_ask / midpoint / spread
is joined to the model's ``predicted_change`` value. The maker-adjusted edge
is ``|predicted_change| - spread/2`` — half-spread is treated as the
adverse-selection cost a resting maker faces in expectation. Rows whose
``adjusted_edge`` falls below the configured floor (default 0.005) are
marked ``tradeable=False`` but still returned so downstream stages and
diagnostics can see what was dropped and why.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "polymarket.db"

DEFAULT_MIN_EDGE_THRESHOLD = 0.005
IN_CLAUSE_CHUNK = 500  # same pattern as features/labels.py

EDGE_COLUMNS: tuple[str, ...] = (
    "market_id",
    "direction",
    "predicted_change",
    "adjusted_edge",
    "current_midpoint",
    "best_bid",
    "best_ask",
    "spread",
    "tradeable",
)

log = logging.getLogger("decision.edge_calculator")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def compute_edges(
    predictions_df: pd.DataFrame,
    db_path: Path | str = DEFAULT_DB_PATH,
    min_edge_threshold: float = DEFAULT_MIN_EDGE_THRESHOLD,
) -> pd.DataFrame:
    """Compute the per-market maker-adjusted edge from model predictions.

    Parameters
    ----------
    predictions_df : DataFrame
        Must contain columns ``market_id`` (str-coerceable) and
        ``predicted_change`` (float — the 6h price-change prediction).
    db_path : Path or str
        SQLite path; the latest snapshot per market_id is read here.
    min_edge_threshold : float
        Rows with ``adjusted_edge < min_edge_threshold`` get ``tradeable=False``.

    Returns
    -------
    DataFrame with columns: ``market_id, direction, predicted_change,
    adjusted_edge, current_midpoint, best_bid, best_ask, spread, tradeable``.
    Empty-but-typed if ``predictions_df`` is empty.
    """
    if predictions_df.empty:
        return _empty_edges_df()

    required = {"market_id", "predicted_change"}
    missing = required - set(predictions_df.columns)
    if missing:
        raise RuntimeError(
            f"predictions_df missing required columns: {sorted(missing)}"
        )

    df = predictions_df.copy()
    df["market_id"] = df["market_id"].astype(str)
    market_ids = df["market_id"].tolist()

    snaps = _fetch_latest_snapshots(db_path, market_ids)
    df = df.merge(snaps, on="market_id", how="left")

    # adjusted_edge = |predicted_change| - spread/2 (half-spread = maker adverse-selection proxy)
    pred = df["predicted_change"].astype(float)
    spread = df["spread"].astype(float)
    df["adjusted_edge"] = pred.abs() - (spread / 2.0)

    # Direction: positive prediction -> buy; negative -> sell; exact zero -> None
    direction = np.where(pred > 0, "buy", np.where(pred < 0, "sell", None))
    df["direction"] = direction

    # Tradeable iff: edge crosses threshold AND we have a valid book quote AND direction not None
    has_book = df["best_bid"].notna() & df["best_ask"].notna() & df["spread"].notna()
    has_direction = df["direction"].notna()
    edge_ok = df["adjusted_edge"] >= min_edge_threshold
    df["tradeable"] = (has_book & has_direction & edge_ok).astype(bool)

    df = df.rename(columns={"midpoint": "current_midpoint"})

    # Keep only the canonical columns in canonical order.
    result = df[list(EDGE_COLUMNS)].reset_index(drop=True)
    log.info(
        "edge filter: %d markets in, %d tradeable (>= %.4f edge)",
        len(result), int(result["tradeable"].sum()), min_edge_threshold,
    )
    return result


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _empty_edges_df() -> pd.DataFrame:
    dtypes: dict[str, str] = {
        "market_id": "object",
        "direction": "object",
        "predicted_change": "float64",
        "adjusted_edge": "float64",
        "current_midpoint": "float64",
        "best_bid": "float64",
        "best_ask": "float64",
        "spread": "float64",
        "tradeable": "bool",
    }
    return pd.DataFrame({c: pd.Series(dtype=dt) for c, dt in dtypes.items()})


def _chunked(seq: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _fetch_latest_snapshots(
    db_path: Path | str,
    market_ids: list[str],
) -> pd.DataFrame:
    """Return one row per market_id with its most-recent snapshot fields.

    Chunks the IN clause to stay under SQLite's variable-count limit.
    Markets without any snapshot row are simply absent from the result —
    the caller's left-join leaves their book columns NaN.
    """
    frames: list[pd.DataFrame] = []
    conn = sqlite3.connect(db_path)
    try:
        for chunk in _chunked(market_ids, IN_CLAUSE_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            query = (
                "SELECT s.market_id, s.best_bid, s.best_ask, s.midpoint, s.spread "
                "FROM snapshots s "
                "JOIN ( "
                "    SELECT market_id, MAX(timestamp) AS ts "
                f"      FROM snapshots WHERE market_id IN ({placeholders}) "
                "    GROUP BY market_id "
                ") latest "
                "  ON latest.market_id = s.market_id AND latest.ts = s.timestamp"
            )
            frames.append(pd.read_sql_query(query, conn, params=list(chunk)))
    finally:
        conn.close()
    if not frames:
        return pd.DataFrame(columns=["market_id", "best_bid", "best_ask", "midpoint", "spread"])
    out = pd.concat(frames, ignore_index=True)
    out["market_id"] = out["market_id"].astype(str)
    return out
