"""Exploratory outcome-prediction experiment on resolved political markets.

*** EXPLORATORY — tiny dataset (~78 markets), high variance. ***
*** Diagnostic only; results must not drive trading decisions. ***

For each resolved political market with ground-truth resolution and >=100
snapshots, build a feature vector at a reference point **48 hours before its
final snapshot** — i.e. well before the market pinned to its outcome — and
ask whether a classifier on those features can beat the market's own price
at predicting the resolution.

The evaluation centers on the *edge check*: when the model's out-of-fold
probability disagrees with the 48h-out market price by more than 10 points,
who was closer to the truth? That number — not AUC — is the question that
matters for the project's thesis.

Anti-leakage mechanism: ``compute_features_batch`` is strictly causal
(only snapshots with ``timestamp <= T_ref`` are visible), so features cannot
see the final pin.

Run:
    python -m model.train_outcome
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import brier_score_loss, confusion_matrix, roc_auc_score
from sklearn.model_selection import StratifiedKFold

from features.correlation_features import compute_correlation_features_batch
from features.labels import generate_outcome_labels
from features.market_features import compute_features_batch
from model.train import POLITICAL_REGEX
from src.utils.dates import parse_iso8601

# --- Paths ----------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "train_outcome.log"
CLUSTER_CONFIG_PATH = REPO_ROOT / "config" / "market_clusters.json"
RESULTS_PATH = REPO_ROOT / "model" / "results" / "outcome_experiment.json"

# --- Experiment constants -------------------------------------------------- #

MIN_SNAPSHOTS = 100
REFERENCE_HOURS_BEFORE = 48
N_FOLDS = 5
DISAGREEMENT_THRESHOLD = 0.10
CALIBRATION_BINS = 4

# price_now is deliberately included: the market's own price 48h out is a
# strong prior on the outcome, and the experiment asks whether the OTHER
# features add anything beyond it.
FEATURE_COLS: tuple[str, ...] = (
    "price_now",
    "momentum_24h",
    "momentum_72h",
    "time_to_resolution",
    "cluster_id",
    "cluster_size",
    "cluster_avg_price",
    "cluster_price_dispersion",
    "cluster_avg_momentum_1h",
    "own_vs_cluster_momentum",
    "cluster_lead_signal",
)

# Fixed, closed category set — a global mapping adds no leakage risk and
# per-fold re-encoding on 78 rows would add noise, not rigor.
CLUSTER_ID_MAPPING: dict[str, int] = {
    "__none__": 0,
    "iran": 1,
    "russia_ukraine": 2,
    "trump_presidency": 3,
}

# Conservative regularization carried over from the regression model;
# min_child_samples drops 50 -> 5 because each training fold has only ~62
# rows (50 would forbid nearly every split).
LGBM_PARAMS: dict = dict(
    n_estimators=50,
    learning_rate=0.05,
    max_depth=3,
    min_child_samples=5,
    reg_alpha=0.1,
    reg_lambda=1.0,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    class_weight="balanced",
    random_state=42,
    verbose=-1,
)

EXPLORATORY_WARNING = (
    "EXPLORATORY EXPERIMENT — ~78 samples, high variance; results are "
    "diagnostic only and must NOT drive trading decisions."
)

log = logging.getLogger("model.train_outcome")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    log.addHandler(console)


# --------------------------------------------------------------------------- #
# Dataset assembly
# --------------------------------------------------------------------------- #


def load_candidate_market_ids(
    db_path: Path | str = DB_PATH,
    min_snapshots: int = MIN_SNAPSHOTS,
) -> list[str]:
    """Political, resolved, resolution-backfilled markets with enough history."""
    conn = sqlite3.connect(db_path)
    try:
        conn.create_function(
            "PYREGEXP", 1,
            lambda s: 1 if (s and POLITICAL_REGEX.search(s)) else 0,
        )
        rows = conn.execute(
            """
            SELECT m.id
              FROM markets m JOIN snapshots s ON s.market_id = m.id
             WHERE m.status = 'resolved_or_removed'
               AND m.resolution IS NOT NULL
               AND m.question IS NOT NULL AND PYREGEXP(m.question) = 1
             GROUP BY m.id
            HAVING COUNT(s.id) >= ?
            """,
            (min_snapshots,),
        ).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def compute_reference_timestamps(
    db_path: Path | str,
    market_ids: list[str],
    hours_before: int = REFERENCE_HOURS_BEFORE,
) -> tuple[dict[str, datetime], int]:
    """Per market: T_ref = last_snapshot - hours_before.

    Markets whose snapshot span is shorter than ``hours_before`` (i.e.
    ``first_snapshot > T_ref`` — no causal data would exist at the reference
    point) are dropped. Returns ``(refs, n_dropped)``.
    """
    if not market_ids:
        return {}, 0
    placeholders = ",".join("?" * len(market_ids))
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT market_id, MIN(timestamp), MAX(timestamp)
              FROM snapshots
             WHERE market_id IN ({placeholders})
             GROUP BY market_id
            """,
            market_ids,
        ).fetchall()
    finally:
        conn.close()

    refs: dict[str, datetime] = {}
    n_dropped = 0
    for mid, first_str, last_str in rows:
        first = parse_iso8601(first_str)
        last = parse_iso8601(last_str)
        if first is None or last is None:
            n_dropped += 1
            continue
        t_ref = last - timedelta(hours=hours_before)
        if first > t_ref:
            n_dropped += 1
            continue
        refs[str(mid)] = t_ref
    return refs, n_dropped


def assemble_dataset(
    db_path: Path | str = DB_PATH,
    cluster_config_path: Path | str = CLUSTER_CONFIG_PATH,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Build the (features, label) frame: one row per surviving market.

    Returns (df, counts) where counts holds the funnel numbers for the report.
    """
    candidate_ids = load_candidate_market_ids(db_path)
    labels_df = generate_outcome_labels(candidate_ids, db_path)
    labels = dict(zip(labels_df["market_id"], labels_df["outcome"]))

    refs, n_dropped_span = compute_reference_timestamps(db_path, candidate_ids)
    log.info(
        "Dataset funnel: candidates=%d, with-parseable-label=%d, "
        "survive-%dh-before=%d (dropped %d for short span)",
        len(candidate_ids), len(labels), REFERENCE_HOURS_BEFORE,
        len(refs), n_dropped_span,
    )

    rows: list[dict] = []
    n_null_price = 0
    for mid in sorted(refs):
        if mid not in labels:
            continue
        t_ref = refs[mid]
        feats = compute_features_batch([mid], t_ref, db_path).iloc[0].to_dict()
        corr = compute_correlation_features_batch(
            [mid], t_ref, db_path, cluster_config_path,
        ).iloc[0].to_dict()

        price_48h = feats.get("price_now")
        if price_48h is None or (isinstance(price_48h, float) and np.isnan(price_48h)):
            # No two-sided book at the reference point -> no market prior to
            # compare against; the edge check would be undefined.
            n_null_price += 1
            continue

        cluster_name = corr.get("cluster_id") or "__none__"
        row = {
            "market_id": mid,
            "t_ref": t_ref.isoformat(),
            "outcome": int(labels[mid]),
            "price_now": float(price_48h),
            "momentum_24h": feats.get("momentum_24h"),
            "momentum_72h": feats.get("momentum_72h"),
            "time_to_resolution": feats.get("time_to_resolution"),
            "cluster_id": CLUSTER_ID_MAPPING.get(str(cluster_name), -1),
            "cluster_size": corr.get("cluster_size"),
            "cluster_avg_price": corr.get("cluster_avg_price"),
            "cluster_price_dispersion": corr.get("cluster_price_dispersion"),
            "cluster_avg_momentum_1h": corr.get("cluster_avg_momentum_1h"),
            "own_vs_cluster_momentum": corr.get("own_vs_cluster_momentum"),
            "cluster_lead_signal": corr.get("cluster_lead_signal"),
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    counts = {
        "n_candidates": len(candidate_ids),
        "n_labeled": len(labels),
        "n_survived_48h": len(refs),
        "n_dropped_short_span": n_dropped_span,
        "n_dropped_null_price": n_null_price,
        "n_final": len(df),
    }
    return df, counts


# --------------------------------------------------------------------------- #
# Evaluation helpers
# --------------------------------------------------------------------------- #


def edge_check(
    y_true: np.ndarray,
    p_model: np.ndarray,
    price: np.ndarray,
    threshold: float = DISAGREEMENT_THRESHOLD,
) -> dict:
    """When model and market disagree by > threshold, who was closer to truth?"""
    diff = np.abs(p_model - price)
    disagree = diff > threshold
    n_disagree = int(disagree.sum())

    model_err = np.abs(p_model - y_true)
    market_err = np.abs(price - y_true)

    model_wins_all = int((model_err < market_err).sum())
    if n_disagree:
        model_wins_disagree = int(
            (model_err[disagree] < market_err[disagree]).sum()
        )
        win_rate = model_wins_disagree / n_disagree
    else:
        model_wins_disagree = 0
        win_rate = None

    return {
        "disagreement_threshold": threshold,
        "n_total": int(len(y_true)),
        "n_disagreements": n_disagree,
        "model_wins_among_disagreements": model_wins_disagree,
        "model_win_rate_among_disagreements": win_rate,
        "model_wins_full_set": model_wins_all,
        "model_win_rate_full_set": model_wins_all / len(y_true) if len(y_true) else None,
    }


def calibration_bins(
    y_true: np.ndarray,
    p_model: np.ndarray,
    n_bins: int = CALIBRATION_BINS,
) -> list[dict]:
    """Quartile-based calibration summary (78 points can't support 10 bins)."""
    order = np.argsort(p_model)
    bins = np.array_split(order, n_bins)
    out = []
    for i, idx in enumerate(bins):
        if len(idx) == 0:
            continue
        out.append({
            "bin": i + 1,
            "n": int(len(idx)),
            "mean_predicted": float(p_model[idx].mean()),
            "actual_yes_rate": float(y_true[idx].mean()),
        })
    return out


# --------------------------------------------------------------------------- #
# Main experiment
# --------------------------------------------------------------------------- #


def run_experiment(
    db_path: Path | str = DB_PATH,
    cluster_config_path: Path | str = CLUSTER_CONFIG_PATH,
    results_path: Path | str = RESULTS_PATH,
) -> dict:
    log.warning(EXPLORATORY_WARNING)

    df, counts = assemble_dataset(db_path, cluster_config_path)
    n = len(df)
    n_yes = int(df["outcome"].sum())
    log.info(
        "Final dataset: %d markets (YES=%d, NO=%d)", n, n_yes, n - n_yes,
    )
    log.info("Features (%d): %s", len(FEATURE_COLS), list(FEATURE_COLS))
    if n < 30 or n_yes < N_FOLDS or (n - n_yes) < N_FOLDS:
        raise RuntimeError(
            f"Too little data for {N_FOLDS}-fold stratified CV "
            f"(n={n}, yes={n_yes}, no={n - n_yes})"
        )

    X = df[list(FEATURE_COLS)].apply(pd.to_numeric, errors="coerce")
    y = df["outcome"].to_numpy(dtype=int)
    price = df["price_now"].to_numpy(dtype=float)

    # ---- Stratified k-fold, out-of-fold probabilities --------------------- #
    skf = StratifiedKFold(n_splits=N_FOLDS, shuffle=True, random_state=42)
    p_oof = np.full(n, np.nan)
    for fold, (train_idx, test_idx) in enumerate(skf.split(X, y), start=1):
        model = LGBMClassifier(**LGBM_PARAMS)
        model.fit(X.iloc[train_idx], y[train_idx])
        p_oof[test_idx] = model.predict_proba(X.iloc[test_idx])[:, 1]
        log.info(
            "fold %d/%d: train=%d test=%d (yes in test=%d)",
            fold, N_FOLDS, len(train_idx), len(test_idx), int(y[test_idx].sum()),
        )
    assert not np.isnan(p_oof).any(), "every market must receive an OOF prediction"

    # ---- Metrics ----------------------------------------------------------- #
    auc_model = float(roc_auc_score(y, p_oof))
    auc_price = float(roc_auc_score(y, price))  # honest baseline: the market itself
    brier_model = float(brier_score_loss(y, p_oof))
    brier_price = float(brier_score_loss(y, np.clip(price, 0.0, 1.0)))
    cm = confusion_matrix(y, (p_oof > 0.5).astype(int), labels=[0, 1])
    tn, fp, fn, tp = (int(v) for v in cm.ravel())
    calib = calibration_bins(y, p_oof)
    edge = edge_check(y, p_oof, price)

    log.info("ROC AUC:  model=%.4f  price-only baseline=%.4f", auc_model, auc_price)
    log.info("Brier:    model=%.4f  price-only baseline=%.4f", brier_model, brier_price)
    log.info("Confusion @0.5 (reference only): TN=%d FP=%d FN=%d TP=%d", tn, fp, fn, tp)
    for b in calib:
        log.info(
            "  calibration bin %d: n=%d mean_pred=%.3f actual_yes=%.3f",
            b["bin"], b["n"], b["mean_predicted"], b["actual_yes_rate"],
        )
    log.info(
        "EDGE CHECK: %d/%d markets disagree >%.0f pts; model closer on %s "
        "(win rate=%s)",
        edge["n_disagreements"], edge["n_total"],
        DISAGREEMENT_THRESHOLD * 100,
        edge["model_wins_among_disagreements"],
        f"{edge['model_win_rate_among_disagreements']:.2%}"
        if edge["model_win_rate_among_disagreements"] is not None else "n/a",
    )

    # ---- Feature importances (full-data fit, diagnostics only) ------------ #
    full_model = LGBMClassifier(**LGBM_PARAMS)
    full_model.fit(X, y)
    importances = sorted(
        zip(FEATURE_COLS, (int(v) for v in full_model.feature_importances_)),
        key=lambda t: -t[1],
    )
    for feat, imp in importances:
        log.info("  importance %s: %d", feat, imp)

    # ---- Per-market records + question text ------------------------------- #
    conn = sqlite3.connect(db_path)
    try:
        q_by_id = dict(conn.execute(
            f"SELECT id, question FROM markets WHERE id IN "
            f"({','.join('?' * n)})",
            df["market_id"].tolist(),
        ).fetchall())
    finally:
        conn.close()
    records = []
    for i, row in df.iterrows():
        q = (q_by_id.get(row["market_id"]) or "")[:80]
        records.append({
            "market_id": row["market_id"],
            "question": q,
            "price_48h": round(float(row["price_now"]), 4),
            "p_model_oof": round(float(p_oof[i]), 4),
            "outcome": int(row["outcome"]),
        })

    results = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "warning": EXPLORATORY_WARNING,
        "config": {
            "reference_hours_before": REFERENCE_HOURS_BEFORE,
            "min_snapshots": MIN_SNAPSHOTS,
            "n_folds": N_FOLDS,
            "features": list(FEATURE_COLS),
            "lgbm_params": {k: v for k, v in LGBM_PARAMS.items() if k != "verbose"},
        },
        "dataset": {**counts, "n_yes": n_yes, "n_no": n - n_yes},
        "metrics": {
            "roc_auc_model": auc_model,
            "roc_auc_price_baseline": auc_price,
            "brier_model": brier_model,
            "brier_price_baseline": brier_price,
            "confusion_at_0p5_reference_only": {
                "tn": tn, "fp": fp, "fn": fn, "tp": tp,
            },
            "calibration_bins": calib,
        },
        "edge_check": edge,
        "feature_importances": [
            {"feature": f, "importance": imp} for f, imp in importances
        ],
        "per_market": records,
    }

    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.info("Saved results -> %s", results_path)

    log.warning(EXPLORATORY_WARNING)
    return results


def main() -> int:
    setup_logging()
    try:
        run_experiment()
    except Exception as exc:  # noqa: BLE001
        log.critical("Experiment FAILED: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
