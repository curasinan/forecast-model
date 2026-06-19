"""Orchestrate the Phase 4 pipeline: features → model.predict →
edge_calculator → position_sizer → risk_manager. Returns the final approved
trade signals.

This module is **read-only** with respect to ``paper_positions`` — it
computes signals but does NOT write them anywhere. Phase 5's execution
layer will pick up the returned DataFrame and (optionally) insert the
filled rows.

CLI:
    python -m decision.signal_generator [--bankroll FLOAT] [--db PATH] [--model PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import joblib
import numpy as np
import pandas as pd

from src.utils.dates import parse_iso8601

from decision.edge_calculator import compute_edges
from decision.position_sizer import size_positions
from decision.risk_manager import (
    DEFAULT_RISK_CONFIG_PATH,
    init_db,
    load_risk_config,
    apply_risk_limits,
)
from features.correlation_features import compute_correlation_features_batch
from features.market_features import compute_features_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "polymarket.db"
DEFAULT_MODEL_PATH = REPO_ROOT / "model" / "artifacts" / "lgbm_regression_v1.pkl"
DEFAULT_CLUSTER_CONFIG_PATH = REPO_ROOT / "config" / "market_clusters.json"
DEFAULT_METRICS_PATH = REPO_ROOT / "model" / "results" / "metrics_6h_v2.json"
LOG_PATH = REPO_ROOT / "logs" / "signal_generator.log"

log = logging.getLogger("decision.signal_generator")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger("decision")
    root.setLevel(logging.INFO)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def generate_signals(
    bankroll: float | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
    model_artifact_path: Path | str = DEFAULT_MODEL_PATH,
    cluster_config_path: Path | str = DEFAULT_CLUSTER_CONFIG_PATH,
    risk_config_path: Path | str = DEFAULT_RISK_CONFIG_PATH,
    metrics_path: Path | str = DEFAULT_METRICS_PATH,
    timestamp: datetime | None = None,
) -> pd.DataFrame:
    """Compute the current set of approved trade signals.

    Returns a DataFrame whose ``tradeable=True`` rows are the approved
    signals. Non-approved rows are returned too so the caller can inspect
    why they were rejected (via ``rejected_reason``).

    If ``bankroll`` is None, derive it from the risk config + DB-stored
    realized P&L. ``timestamp`` defaults to ``datetime.now(timezone.utc)``.
    """
    risk_cfg = load_risk_config(risk_config_path)
    effective_bankroll = bankroll if bankroll is not None else _current_bankroll(
        db_path, float(risk_cfg["starting_bankroll"]),
    )
    log.info("generate_signals: bankroll=$%.2f", effective_bankroll)

    # ---- 1. Load model artifact + candidate cohort ----------------------- #
    artifact = joblib.load(model_artifact_path)
    model = artifact["model"]
    feature_cols: list[str] = artifact["feature_cols"]
    cluster_mapping: dict[str, int] = artifact.get("cluster_mapping") or {}
    log.info(
        "loaded model artifact: optimal_n_estimators=%s feature_cols=%s",
        artifact.get("optimal_n_estimators"), feature_cols,
    )

    # Re-import locally so model.train isn't a hard dep at module-import time
    # (keeps decision/ pickle-loadable in restricted environments).
    from model.train import load_political_market_ids
    market_ids = load_political_market_ids(db_path)
    log.info("political cohort: %d market_ids", len(market_ids))
    if not market_ids:
        log.warning("no candidate markets — returning empty signal set")
        return _empty_signals_df()

    # ---- 2. Freshness guard --------------------------------------------- #
    # Refuse to trade on stale order books (the host sleeps overnight, leaving
    # multi-hour snapshot gaps). Drop markets whose newest snapshot is older
    # than max_snapshot_age_minutes, and CRITICAL-warn if the WHOLE pipeline
    # looks down (newest snapshot anywhere is stale).
    T = timestamp if timestamp is not None else datetime.now(timezone.utc)
    max_age = float(risk_cfg["max_snapshot_age_minutes"])
    fresh_ids, n_excluded, global_stale = _filter_fresh_markets(
        db_path, market_ids, max_age, T,
    )
    if global_stale:
        log.critical(
            "Most recent snapshot across the DB is older than %.0f min — the "
            "collection pipeline may be DOWN; this entire signal run is suspect.",
            max_age,
        )
    log.info("Excluded %d markets for stale data (>%.0f min old)", n_excluded, max_age)
    if not fresh_ids:
        log.warning("no markets passed the freshness guard — returning empty signal set")
        return _empty_signals_df()
    market_ids = fresh_ids

    # ---- 3. Compute features at T_now ------------------------------------ #
    market_feats = compute_features_batch(market_ids, T, db_path)
    corr_feats = compute_correlation_features_batch(
        market_ids, T, db_path, cluster_config_path,
    )
    merged = market_feats.merge(corr_feats, on="market_id", how="left")

    # Encode cluster_id with the saved mapping (only if it's actually a feature).
    if "cluster_id" in feature_cols:
        SENTINEL = "__none__"
        cluster_vals = merged["cluster_id"].fillna(SENTINEL).astype(str)
        merged["cluster_id"] = cluster_vals.map(
            lambda c: cluster_mapping.get(c, -1)
        ).astype("int64")

    missing_cols = [c for c in feature_cols if c not in merged.columns]
    if missing_cols:
        raise KeyError(
            f"model expects feature_cols not produced by the feature pipelines: "
            f"{missing_cols}"
        )

    # Dtype coercion + restriction to feature_cols, same as model/train.py.
    X = merged[feature_cols].apply(pd.to_numeric, errors="coerce")
    y_pred = model.predict(X)
    predictions_df = pd.DataFrame({
        "market_id": merged["market_id"].astype(str).values,
        "predicted_change": y_pred,
    })
    log.info(
        "model predictions: mean=%.5f std=%.5f min=%.5f max=%.5f",
        float(np.mean(y_pred)), float(np.std(y_pred)),
        float(np.min(y_pred)), float(np.max(y_pred)),
    )

    # ---- 3. Pipeline: edges → sizing → risk ------------------------------ #
    edges = compute_edges(predictions_df, db_path=db_path)
    sized = size_positions(edges, effective_bankroll, db_path=db_path, metrics_path=metrics_path)
    final = apply_risk_limits(
        sized, effective_bankroll, db_path=db_path,
        cluster_config_path=cluster_config_path,
        risk_config_path=risk_config_path,
    )

    n_total = len(final)
    n_passed_edge = int(edges["tradeable"].sum())
    n_passed_sizing = int(sized["tradeable"].sum())
    n_approved = int(final["tradeable"].sum())
    log.info(
        "pipeline summary: total=%d edge=%d sizing=%d approved=%d",
        n_total, n_passed_edge, n_passed_sizing, n_approved,
    )
    return final


# --------------------------------------------------------------------------- #
# Internals + CLI
# --------------------------------------------------------------------------- #


def _empty_signals_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "market_id", "direction", "predicted_change", "adjusted_edge",
        "current_midpoint", "best_bid", "best_ask", "spread",
        "tradeable", "kelly_fraction", "position_dollars", "rejected_reason",
    ])


def _filter_fresh_markets(
    db_path: Path | str,
    market_ids: list[str],
    max_age_minutes: float,
    T: datetime,
) -> tuple[list[str], int, bool]:
    """Drop markets whose newest snapshot is older than ``max_age_minutes``.

    Returns ``(fresh_ids, n_excluded, global_stale)`` where ``global_stale`` is
    True when the single most-recent snapshot across the entire DB is itself
    older than the threshold (i.e. the whole collection pipeline looks down).
    """
    cutoff = T - timedelta(minutes=max_age_minutes)
    conn = sqlite3.connect(db_path)
    try:
        # Global freshness: newest snapshot anywhere.
        row = conn.execute("SELECT MAX(timestamp) FROM snapshots").fetchone()
        newest_overall = parse_iso8601(row[0]) if row and row[0] else None
        global_stale = newest_overall is None or newest_overall < cutoff

        # Per-market latest timestamp, chunked to respect SQLite's variable limit.
        latest_by_id: dict[str, datetime | None] = {}
        for chunk in _chunked(market_ids, 500):
            placeholders = ",".join("?" * len(chunk))
            q = (
                "SELECT market_id, MAX(timestamp) FROM snapshots "
                f"WHERE market_id IN ({placeholders}) GROUP BY market_id"
            )
            for mid, ts in conn.execute(q, list(chunk)).fetchall():
                latest_by_id[str(mid)] = parse_iso8601(ts) if ts else None
    finally:
        conn.close()

    fresh: list[str] = []
    for mid in market_ids:
        latest = latest_by_id.get(mid)
        if latest is not None and latest >= cutoff:
            fresh.append(mid)
    n_excluded = len(market_ids) - len(fresh)
    return fresh, n_excluded, global_stale


def _chunked(seq: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _current_bankroll(db_path: Path | str, starting_bankroll: float) -> float:
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM paper_positions WHERE status='closed'"
        ).fetchone()
    finally:
        conn.close()
    realized = float(row[0] if row else 0.0)
    return starting_bankroll + realized


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate (print-only) trade signals for the Phase 4 paper-trader.",
    )
    parser.add_argument(
        "--bankroll", type=float, default=None,
        help="Override bankroll dollars. If omitted, derived from risk_limits.json "
             "+ realized P&L in paper_positions.",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB_PATH,
        help=f"SQLite path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--model", type=Path, default=DEFAULT_MODEL_PATH,
        help=f"Path to joblib model artifact (default: {DEFAULT_MODEL_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = _parse_args(argv)
    log.info("=" * 72)
    log.info("Starting decision/signal_generator (Phase 4 — print-only)")
    log.info("=" * 72)
    try:
        signals = generate_signals(
            bankroll=args.bankroll, db_path=args.db, model_artifact_path=args.model,
        )
    except Exception as exc:  # noqa: BLE001
        log.critical("Unhandled error generating signals: %s", exc, exc_info=True)
        return 1

    approved = signals[signals["tradeable"]].copy() if "tradeable" in signals.columns else signals
    print()
    if approved.empty:
        print("No approved signals at this snapshot.")
    else:
        cols = [c for c in [
            "market_id", "direction", "predicted_change", "adjusted_edge",
            "current_midpoint", "best_bid", "best_ask", "spread",
            "kelly_fraction", "position_dollars",
        ] if c in approved.columns]
        approved_sorted = approved.sort_values("adjusted_edge", ascending=False)
        print(f"Approved signals ({len(approved_sorted)}):")
        print(approved_sorted[cols].to_string(index=False))
    print()
    log.info("=" * 72)
    log.info("signal_generator done")
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
