"""Continuous-regression price-change prediction pipeline with overfitting checks."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from scipy.stats import pearsonr
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from features.labels import build_training_dataset

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "training.log"
ARTIFACTS_DIR = REPO_ROOT / "model" / "artifacts"
RESULTS_DIR = REPO_ROOT / "model" / "results"
CLUSTER_CONFIG_PATH = REPO_ROOT / "config" / "market_clusters.json"

MODEL_ARTIFACT_PATH = ARTIFACTS_DIR / "lgbm_regression_v1.pkl"
METRICS_PATH = RESULTS_DIR / "metrics.json"
FEATURE_IMPORTANCES_PATH = RESULTS_DIR / "feature_importances.csv"
PREDICTIONS_VS_ACTUAL_PATH = RESULTS_DIR / "predictions_vs_actual.csv"
LEARNING_CURVE_PATH = RESULTS_DIR / "learning_curve.csv"
FEATURE_STABILITY_PATH = RESULTS_DIR / "feature_stability.csv"

LGBM_PARAMS: dict = dict(
    learning_rate=0.05,
    max_depth=3,
    min_child_samples=50,
    reg_alpha=0.1,
    reg_lambda=1.0,
    subsample=0.8,
    subsample_freq=1,           # required to actually activate row subsampling
    colsample_bytree=0.8,
    random_state=42,
    verbose=-1,
)

TIMESTAMP_INTERVAL_HOURS = 6
LOOKBACK_BUFFER_HOURS = 72
LABEL_BUFFER_HOURS = 24
MIN_SNAPSHOTS = 50
TRAIN_VAL_HOLDOUT_FRACTIONS: tuple[float, float] = (0.6, 0.8)

LEARNING_CURVE_N_ESTIMATORS: tuple[int, ...] = (10, 15, 25, 50, 100, 200)
CONFIDENCE_ENSEMBLE_SEEDS: tuple[int, ...] = (42, 43, 44, 45, 46)
OVERFITTING_R2_GAP_THRESHOLD: float = 0.3

BUY_PRICE_MOVE_THRESHOLD = 0.02
SELL_PRICE_MOVE_THRESHOLD = -0.02

TOP_FEATURES_TO_SAVE = 20
TOP_FEATURES_FOR_STABILITY = 10
CLUSTER_NULL_SENTINEL = "__none__"

STABLE_FEATURES: tuple[str, ...] = (
    "momentum_24h",
    "momentum_72h",
    "price_now",
    "time_to_resolution",
)

SUPPORTED_HORIZONS: tuple[int, ...] = (1, 6, 24)

POLITICAL_PATTERNS = [
    # Elections / institutions
    r"\belection(s|al)?\b", r"\belect(ed|ing)?\b", r"\bpresident(ial|cy|s)?\b",
    r"\bgovernor(s|ship)?\b", r"\bsenat(e|or|ors)\b", r"\bcongress(ional)?\b",
    r"\brepublican(s)?\b", r"\bdemocrat(s|ic)?\b", r"\bgop\b",
    r"\bvot(e|es|er|ers|ed|ing)\b", r"\bballot(s)?\b", r"\bprimary\b",
    r"\bcaucus(es)?\b", r"\bimpeach(ed|ment|ing)?\b", r"\bpardon(s|ed|ing)?\b",
    r"\bcabinet\b", r"\bsecretary\b", r"\battorney general\b",
    r"\bsupreme court\b", r"\bjustice\b", r"\blegislation\b",
    # US political figures
    r"\btrump\b", r"\bbiden\b", r"\bvance\b", r"\bharris\b",
    r"\bpelosi\b", r"\bmccarthy\b",
    # Foreign leaders
    r"\bputin\b", r"\bxi jinping\b", r"\bnetanyahu\b", r"\bkhamenei\b",
    r"\berdogan\b", r"\bmodi\b", r"\bzelensky\b", r"\bmacron\b",
    r"\bstarmer\b", r"\bmeloni\b",
    # Geopolitical / military
    r"\bwar\b", r"\bmilitary\b", r"\binvasion\b", r"\bsanctions?\b",
    r"\btreaty\b", r"\bpeace deal\b", r"\bnuclear deal\b",
    r"\bdiplomatic\b", r"\bsummit\b", r"\bceasefire\b", r"\btroops\b",
    # Countries / regions (with demonyms)
    r"\biran(ian|ians)?\b", r"\biraq(i|is)?\b", r"\bsyria(n|ns)?\b",
    r"\bukrain(e|ian|ians)\b", r"\bruss(ia|ian|ians)\b", r"\bgaza\b",
    r"\bisrael(i|is)?\b", r"\bchin(a|ese)\b", r"\btaiwan(ese)?\b",
    r"\bnorth korea(n|ns)?\b", r"\bvenezuela(n|ns)?\b", r"\bcuba(n|ns)?\b",
    r"\bkremlin\b", r"\bpentagon\b", r"\bmayor(al|s)?\b",
    r"\btariffs?\b", r"\bembassy\b",
]
POLITICAL_REGEX = re.compile("|".join(POLITICAL_PATTERNS), flags=re.IGNORECASE)

log = logging.getLogger("model.train")


# Logging


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


# Step 1: dataset assembly


def load_political_market_ids(
    db_path: Path | str = DB_PATH,
    min_snapshots: int = MIN_SNAPSHOTS,
) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        conn.create_function(
            "PYREGEXP", 1,
            lambda s: 1 if (s and POLITICAL_REGEX.search(s)) else 0,
        )
        rows = conn.execute(
            """
            SELECT s.market_id
              FROM snapshots s JOIN markets m ON m.id = s.market_id
             WHERE m.question IS NOT NULL AND PYREGEXP(m.question) = 1
             GROUP BY s.market_id
            HAVING COUNT(*) >= ?
            """,
            (min_snapshots,),
        ).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def generate_timestamps(
    db_path: Path | str = DB_PATH,
    interval_hours: int = TIMESTAMP_INTERVAL_HOURS,
    lookback_buffer: int = LOOKBACK_BUFFER_HOURS,
    label_buffer: int = LABEL_BUFFER_HOURS,
) -> list[datetime]:
    conn = sqlite3.connect(db_path)
    try:
        earliest_str, latest_str = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM snapshots"
        ).fetchone()
    finally:
        conn.close()
    if earliest_str is None or latest_str is None:
        raise RuntimeError("snapshots table is empty")
    earliest = datetime.fromisoformat(earliest_str.replace("Z", "+00:00"))
    latest = datetime.fromisoformat(latest_str.replace("Z", "+00:00"))
    start = earliest + timedelta(hours=lookback_buffer)
    end = latest - timedelta(hours=label_buffer)
    if start >= end:
        raise RuntimeError(
            f"snapshot history too short: start={start} end={end}"
        )
    out: list[datetime] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(hours=interval_hours)
    return out


# Step 2: target + feature selection


def prepare_target_and_columns(
    df: pd.DataFrame,
    target_col: str = "price_change_24h",
    feature_subset: Iterable[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Drop rows with no label; identify feature columns."""
    if target_col not in df.columns:
        raise RuntimeError(
            f"{target_col} column missing — was the corresponding horizon "
            "passed to build_training_dataset?"
        )
    df = df[df[target_col].notna()].copy()

    label_prefixes = ("price_at", "price_change", "direction")
    id_cols = {"market_id", "timestamp"}
    all_features = [
        c for c in df.columns
        if c not in id_cols and not any(c.startswith(p) for p in label_prefixes)
    ]
    if feature_subset is None:
        return df, all_features

    subset = set(feature_subset)
    feature_cols = [c for c in all_features if c in subset]
    missing = subset - set(feature_cols)
    if missing:
        raise RuntimeError(
            f"Requested feature subset references unknown columns: {sorted(missing)}"
        )
    return df, feature_cols


def encode_cluster_id(
    train: pd.DataFrame,
    *others: pd.DataFrame,
) -> tuple:
    """Label-encode ``cluster_id`` fitted on ``train``, applied to ``others``."""
    train_filled = train["cluster_id"].fillna(CLUSTER_NULL_SENTINEL).astype(str)
    categories = sorted(train_filled.unique())
    mapping: dict[str, int] = {c: i for i, c in enumerate(categories)}

    train = train.copy()
    train["cluster_id"] = train_filled.map(mapping).astype("int64")

    encoded_others: list[pd.DataFrame] = []
    for other in others:
        other = other.copy()
        other_filled = other["cluster_id"].fillna(CLUSTER_NULL_SENTINEL).astype(str)
        other["cluster_id"] = other_filled.map(
            lambda c: mapping.get(c, -1)
        ).astype("int64")
        encoded_others.append(other)

    return (train, *encoded_others, mapping)


# Step 3: three-way temporal split


def temporal_split(
    df: pd.DataFrame,
    fractions: tuple[float, float] = TRAIN_VAL_HOLDOUT_FRACTIONS,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    """Split chronologically into train/val/holdout by row count (NOT date range)."""
    f1, f2 = fractions
    df = df.copy()
    df["__ts"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("__ts").reset_index(drop=True)
    n = len(df)
    i1 = int(round(n * f1))
    i2 = int(round(n * f2))
    # Last-timestamp-in-block markers, for logging / metrics output.
    t1 = df["__ts"].iloc[i1 - 1] if i1 > 0 else df["__ts"].min()
    t2 = df["__ts"].iloc[i2 - 1] if i2 > 0 else t1
    train = df.iloc[:i1].drop(columns=["__ts"]).reset_index(drop=True)
    val = df.iloc[i1:i2].drop(columns=["__ts"]).reset_index(drop=True)
    holdout = df.iloc[i2:].drop(columns=["__ts"]).reset_index(drop=True)
    return train, val, holdout, t1, t2


# Profit simulation (unchanged from the regression-only version)


def simulate_profit(
    test_df: pd.DataFrame,
    y_pred: np.ndarray,
    label_col: str = "price_change_24h",
    buy_threshold: float = BUY_PRICE_MOVE_THRESHOLD,
    sell_threshold: float = SELL_PRICE_MOVE_THRESHOLD,
) -> dict[str, int | float | None]:
    empty: dict[str, int | float | None] = {
        "n_buy_signals": 0,
        "n_sell_signals": 0,
        "avg_pnl_per_buy": None,
        "avg_pnl_per_sell": None,
        "total_pnl": 0.0,
        "pct_profitable_overall": None,
        "pct_profitable_buys": None,
        "pct_profitable_sells": None,
    }
    if len(test_df) == 0:
        return empty

    df = test_df.copy()
    df["y_pred"] = y_pred
    df = df[df[label_col].notna()].copy()
    if len(df) == 0:
        return empty

    buys = df[df["y_pred"] > buy_threshold].copy()
    sells = df[df["y_pred"] < sell_threshold].copy()

    buys["pnl"] = buys[label_col].astype(float)
    sells["pnl"] = -sells[label_col].astype(float)

    n_buys = int(len(buys))
    n_sells = int(len(sells))
    n_total = n_buys + n_sells

    avg_buy = float(buys["pnl"].mean()) if n_buys else None
    avg_sell = float(sells["pnl"].mean()) if n_sells else None
    total = float(buys["pnl"].sum() + sells["pnl"].sum())

    pct_buys = float((buys["pnl"] > 0).mean()) if n_buys else None
    pct_sells = float((sells["pnl"] > 0).mean()) if n_sells else None
    if n_total:
        all_pnl = pd.concat([buys["pnl"], sells["pnl"]], ignore_index=True)
        pct_overall: float | None = float((all_pnl > 0).mean())
    else:
        pct_overall = None

    return {
        "n_buy_signals": n_buys,
        "n_sell_signals": n_sells,
        "avg_pnl_per_buy": avg_buy,
        "avg_pnl_per_sell": avg_sell,
        "total_pnl": total,
        "pct_profitable_overall": pct_overall,
        "pct_profitable_buys": pct_buys,
        "pct_profitable_sells": pct_sells,
    }


def _log_sim(label: str, sim: dict) -> None:
    log.info(
        "    %s  buys=%s sells=%s avg_pnl_buy=%s avg_pnl_sell=%s total=%s",
        label,
        sim["n_buy_signals"],
        sim["n_sell_signals"],
        f"{sim['avg_pnl_per_buy']:+.5f}" if sim["avg_pnl_per_buy"] is not None else "n/a",
        f"{sim['avg_pnl_per_sell']:+.5f}" if sim["avg_pnl_per_sell"] is not None else "n/a",
        f"{sim['total_pnl']:+.5f}",
    )
    log.info(
        "    %s  %% profitable: overall=%s buys=%s sells=%s",
        label,
        f"{sim['pct_profitable_overall']*100:.1f}%" if sim["pct_profitable_overall"] is not None else "n/a",
        f"{sim['pct_profitable_buys']*100:.1f}%" if sim["pct_profitable_buys"] is not None else "n/a",
        f"{sim['pct_profitable_sells']*100:.1f}%" if sim["pct_profitable_sells"] is not None else "n/a",
    )


# Model fitting helper


def _fit_regressor(
    X: pd.DataFrame,
    y: np.ndarray,
    n_estimators: int,
    random_state: int = 42,
) -> LGBMRegressor:
    params = {**LGBM_PARAMS, "n_estimators": n_estimators, "random_state": random_state}
    model = LGBMRegressor(**params)
    model.fit(X, y)
    return model


def _top_n_features(model: LGBMRegressor, feature_cols: list[str], n: int) -> set[str]:
    df = pd.DataFrame({
        "feature": feature_cols,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)
    return set(df.head(n)["feature"].tolist())


# Main pipeline


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train regression model with overfitting checks.",
    )
    parser.add_argument(
        "--final",
        action="store_true",
        help="Also evaluate on the holdout set. Use ONLY when iteration is complete.",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=24,
        choices=list(SUPPORTED_HORIZONS),
        help="Forecast horizon in hours (target column: price_change_<h>h). Default 24.",
    )
    parser.add_argument(
        "--features",
        choices=("all", "stable"),
        default="all",
        help="Use all features (default) or only the 4 stable features from the "
             "2026-05-23 stability check.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=BUY_PRICE_MOVE_THRESHOLD,
        help=f"Absolute trade-trigger threshold in price-move units (default {BUY_PRICE_MOVE_THRESHOLD}).",
    )
    parser.add_argument(
        "--metrics-suffix",
        default="",
        help="Suffix to append to metrics filename (metrics<suffix>.json). "
             "Useful for parallel experiments (e.g. '_24h', '_6h', '_1h').",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = _parse_args(argv)

    target_col = f"price_change_{args.horizon}h"
    feature_subset = STABLE_FEATURES if args.features == "stable" else None
    buy_threshold = args.threshold
    sell_threshold = -args.threshold
    metrics_path = METRICS_PATH.parent / f"metrics{args.metrics_suffix}.json"

    log.info("=" * 72)
    log.info("Starting model training pipeline (regression w/ overfitting checks)")
    log.info("  horizon         : %dh (target column: %s)", args.horizon, target_col)
    log.info("  feature subset  : %s%s", args.features,
             f" -> {list(STABLE_FEATURES)}" if feature_subset is not None else "")
    log.info("  trade threshold : ±%.4f", buy_threshold)
    log.info("  metrics path    : %s", metrics_path)
    log.info("=" * 72)

    market_ids = load_political_market_ids()
    log.info(
        "Step 1a: loaded %d political market_ids (>= %d snapshots each)",
        len(market_ids), MIN_SNAPSHOTS,
    )
    if not market_ids:
        log.critical("No political markets meet the snapshot threshold. Aborting.")
        return 1

    timestamps = generate_timestamps()
    log.info(
        "Step 1b: generated %d timestamps at %dh intervals (%s -> %s)",
        len(timestamps), TIMESTAMP_INTERVAL_HOURS,
        timestamps[0].isoformat(), timestamps[-1].isoformat(),
    )

    log.info(
        "Step 1c: building training dataset (~%d markets x %d timestamps = %d rows pre-filter)",
        len(market_ids), len(timestamps), len(market_ids) * len(timestamps),
    )
    df = build_training_dataset(
        market_ids, timestamps,
        db_path=DB_PATH, cluster_config_path=CLUSTER_CONFIG_PATH,
    )
    log.info("Step 1d: build_training_dataset returned %d rows after null-fraction filter", len(df))

    df, feature_cols = prepare_target_and_columns(
        df, target_col=target_col, feature_subset=feature_subset,
    )
    log.info("Step 2: %d rows have non-null %s", len(df), target_col)
    log.info(
        "        target stats: mean=%.5f std=%.5f min=%.5f max=%.5f",
        float(df[target_col].mean()),
        float(df[target_col].std()),
        float(df[target_col].min()),
        float(df[target_col].max()),
    )
    log.info("        feature columns (%d): %s", len(feature_cols), feature_cols)
    if len(df) < 100:
        log.critical("Too few labeled rows (%d) — aborting.", len(df))
        return 1

    train_df, val_df, holdout_df, t1, t2 = temporal_split(df, TRAIN_VAL_HOLDOUT_FRACTIONS)
    log.info(
        "Step 3: split at %s and %s — train=%d val=%d holdout=%d",
        t1.isoformat(), t2.isoformat(),
        len(train_df), len(val_df), len(holdout_df),
    )
    if len(train_df) == 0 or len(val_df) == 0:
        log.critical("Train or validation slice is empty. Aborting.")
        return 1
    if len(holdout_df) == 0 and args.final:
        log.critical("--final was passed but the holdout slice is empty. Aborting.")
        return 1

    if "cluster_id" in feature_cols:
        train_df, val_df, holdout_df, cluster_mapping = encode_cluster_id(
            train_df, val_df, holdout_df
        )
        log.info(
            "Step 4: cluster_id label-encoded (%d categories: %s)",
            len(cluster_mapping), list(cluster_mapping.keys()),
        )
    else:
        cluster_mapping = {}
        log.info("Step 4: cluster_id not in feature subset — encoding skipped")

    X_train = train_df[feature_cols].apply(pd.to_numeric, errors="coerce")
    X_val = val_df[feature_cols].apply(pd.to_numeric, errors="coerce")
    X_holdout = holdout_df[feature_cols].apply(pd.to_numeric, errors="coerce")
    y_train = train_df[target_col].astype(float).values
    y_val = val_df[target_col].astype(float).values
    y_holdout = holdout_df[target_col].astype(float).values

    log.info(
        "Step 5: learning-curve sweep over n_estimators=%s",
        list(LEARNING_CURVE_N_ESTIMATORS),
    )
    sweep_results: dict[int, tuple[LGBMRegressor, float]] = {}
    for n in LEARNING_CURVE_N_ESTIMATORS:
        model = _fit_regressor(X_train, y_train, n_estimators=n, random_state=42)
        val_mae = float(mean_absolute_error(y_val, model.predict(X_val)))
        sweep_results[n] = (model, val_mae)
        log.info("    n_estimators=%-4d  val_mae=%.5f", n, val_mae)

    optimal_n = min(sweep_results, key=lambda n: sweep_results[n][1])
    log.info(
        "    -> optimal n_estimators=%d (val_mae=%.5f)",
        optimal_n, sweep_results[optimal_n][1],
    )
    if optimal_n in (min(LEARNING_CURVE_N_ESTIMATORS), max(LEARNING_CURVE_N_ESTIMATORS)):
        log.warning(
            "Optimal n_estimators=%d is at the sweep boundary — consider widening the sweep.",
            optimal_n,
        )

    pd.DataFrame(
        [{"n_estimators": n, "val_mae": v[1]} for n, v in sweep_results.items()]
    ).to_csv(LEARNING_CURVE_PATH, index=False)

    main_model = sweep_results[optimal_n][0]
    log.info(
        "Step 6: main model = sweep model at n_estimators=%d (reused, no extra fit)",
        optimal_n,
    )

    importances = pd.DataFrame({
        "feature": feature_cols,
        "importance": main_model.feature_importances_,
    }).sort_values("importance", ascending=False).reset_index(drop=True)
    top_importances = importances.head(TOP_FEATURES_TO_SAVE)
    log.info("        top %d features by importance:", TOP_FEATURES_TO_SAVE)
    for _, row in top_importances.iterrows():
        log.info("          %s: %s", row["feature"], int(row["importance"]))

    y_pred_train = main_model.predict(X_train)
    y_pred_val = main_model.predict(X_val)

    mae_train = float(mean_absolute_error(y_train, y_pred_train))
    r2_train = float(r2_score(y_train, y_pred_train))
    mae_val = float(mean_absolute_error(y_val, y_pred_val))
    r2_val = float(r2_score(y_val, y_pred_val))
    rmse_val = float(np.sqrt(mean_squared_error(y_val, y_pred_val)))
    pearson_val = float(pearsonr(y_pred_val, y_val)[0]) if len(y_val) > 1 else float("nan")

    log.info("Step 7: train vs validation:")
    log.info("                  TRAIN          VAL")
    log.info("    MAE        %.5f       %.5f", mae_train, mae_val)
    log.info("    R²        %+.5f      %+.5f", r2_train, r2_val)
    log.info("    RMSE                          %.5f", rmse_val)
    log.info("    Pearson r                     %+.5f", pearson_val)

    overfitting_warning = False
    if (r2_train - r2_val) > OVERFITTING_R2_GAP_THRESHOLD:
        log.warning(
            "OVERFITTING DETECTED — training performance significantly exceeds "
            "validation performance. train R²=%.4f, val R²=%.4f (gap=%.4f > %s). "
            "Consider reducing model complexity.",
            r2_train, r2_val, r2_train - r2_val, OVERFITTING_R2_GAP_THRESHOLD,
        )
        overfitting_warning = True
    elif r2_val < 0 < r2_train:
        log.warning(
            "OVERFITTING DETECTED — training R² is positive (%.4f) but "
            "validation R² is negative (%.4f). Model has not generalized.",
            r2_train, r2_val,
        )
        overfitting_warning = True

    half = len(train_df) // 2
    X_train_a, y_train_a = X_train.iloc[:half], y_train[:half]
    X_train_b, y_train_b = X_train.iloc[half:], y_train[half:]
    log.info(
        "Step 8: feature stability — half_a=%d rows, half_b=%d rows",
        len(X_train_a), len(X_train_b),
    )
    half_a_model = _fit_regressor(X_train_a, y_train_a, n_estimators=optimal_n, random_state=42)
    half_b_model = _fit_regressor(X_train_b, y_train_b, n_estimators=optimal_n, random_state=42)

    top_main = _top_n_features(main_model, feature_cols, TOP_FEATURES_FOR_STABILITY)
    top_a = _top_n_features(half_a_model, feature_cols, TOP_FEATURES_FOR_STABILITY)
    top_b = _top_n_features(half_b_model, feature_cols, TOP_FEATURES_FOR_STABILITY)

    stable = sorted(top_main & top_a & top_b)
    union = top_main | top_a | top_b
    unstable = sorted(union - set(stable))

    log.info("    stable (top-10 in all 3 models):  %s", stable)
    log.info("    unstable (top-10 in 1 or 2 only): %s", unstable)

    stability_rows: list[dict] = []
    for f in sorted(union):
        in_main = f in top_main
        in_a = f in top_a
        in_b = f in top_b
        status = "stable" if (in_main and in_a and in_b) else "unstable"
        stability_rows.append({
            "feature": f,
            "in_main": int(in_main),
            "in_half_a": int(in_a),
            "in_half_b": int(in_b),
            "status": status,
        })
    pd.DataFrame(stability_rows).to_csv(FEATURE_STABILITY_PATH, index=False)

    log.info(
        "Step 9: confidence ensemble — training 4 additional seeds (42 reuses main model)",
    )
    ensemble_models: dict[int, LGBMRegressor] = {42: main_model}
    for seed in CONFIDENCE_ENSEMBLE_SEEDS:
        if seed in ensemble_models:
            continue
        ensemble_models[seed] = _fit_regressor(
            X_train, y_train, n_estimators=optimal_n, random_state=seed,
        )

    ensemble_preds = np.column_stack(
        [ensemble_models[s].predict(X_val) for s in CONFIDENCE_ENSEMBLE_SEEDS]
    )
    pred_std = ensemble_preds.std(axis=1)
    log.info(
        "    pred_std stats: mean=%.5f median=%.5f min=%.5f max=%.5f",
        float(pred_std.mean()), float(np.median(pred_std)),
        float(pred_std.min()), float(pred_std.max()),
    )

    pd.DataFrame({
        "predicted": y_pred_val,
        "actual": y_val,
        "pred_std": pred_std,
    }).to_csv(PREDICTIONS_VS_ACTUAL_PATH, index=False)

    log.info("Step 10: profit simulation (threshold=±%.4f, label=%s)", buy_threshold, target_col)
    full_sim = simulate_profit(
        val_df, y_pred_val,
        label_col=target_col,
        buy_threshold=buy_threshold,
        sell_threshold=sell_threshold,
    )
    _log_sim("FULL      ", full_sim)

    candidate_mask = np.abs(y_pred_val) > buy_threshold
    n_candidates = int(candidate_mask.sum())
    if n_candidates >= 2:
        candidate_stds = pred_std[candidate_mask]
        candidate_std_median = float(np.median(candidate_stds))
        confident_in_candidates = candidate_stds < candidate_std_median
        confident_mask = np.zeros_like(candidate_mask, dtype=bool)
        confident_mask[candidate_mask] = confident_in_candidates
    elif n_candidates == 1:
        # Single candidate has no peer comparison; keep it.
        candidate_std_median = float(pred_std[candidate_mask][0])
        confident_mask = candidate_mask.copy()
    else:
        candidate_std_median = float("nan")
        confident_mask = candidate_mask.copy()  # all False

    n_confident = int(confident_mask.sum())
    val_df_confident = val_df.iloc[confident_mask.tolist()].reset_index(drop=True)
    y_pred_confident = y_pred_val[confident_mask]
    confident_sim = simulate_profit(
        val_df_confident, y_pred_confident,
        label_col=target_col,
        buy_threshold=buy_threshold,
        sell_threshold=sell_threshold,
    )
    confident_sim["n_candidates"] = n_candidates
    confident_sim["pred_std_median_cutoff"] = candidate_std_median
    confident_sim["n_rows_kept"] = n_confident
    log.info(
        "    confident-only filter: %d/%d trade candidates kept "
        "(pred_std among candidates < %s)",
        n_confident, n_candidates,
        f"{candidate_std_median:.5f}" if not np.isnan(candidate_std_median) else "n/a",
    )
    _log_sim("CONFIDENT ", confident_sim)

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    joblib.dump(
        {
            "model": main_model,
            "feature_cols": feature_cols,
            "cluster_mapping": cluster_mapping,
            "optimal_n_estimators": optimal_n,
            "lgbm_params": {**LGBM_PARAMS, "n_estimators": optimal_n},
            "trained_at": datetime.now(timezone.utc).isoformat(),
        },
        MODEL_ARTIFACT_PATH,
    )
    log.info("Saved model artifact -> %s", MODEL_ARTIFACT_PATH)

    top_importances.to_csv(FEATURE_IMPORTANCES_PATH, index=False)
    log.info("Saved feature importances -> %s", FEATURE_IMPORTANCES_PATH)
    log.info("Saved learning curve -> %s", LEARNING_CURVE_PATH)
    log.info("Saved feature stability -> %s", FEATURE_STABILITY_PATH)
    log.info("Saved predictions_vs_actual -> %s", PREDICTIONS_VS_ACTUAL_PATH)

    metrics = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "model_type": "regression_with_overfit_checks",
        "experiment_config": {
            "horizon_hours": args.horizon,
            "target_column": target_col,
            "feature_set": args.features,
            "feature_cols": feature_cols,
            "trade_threshold": buy_threshold,
        },
        "n_markets": len(market_ids),
        "n_timestamps": len(timestamps),
        "rows_after_label_filter": int(len(df)),
        "split": {
            "train_split_timestamp": t1.isoformat(),
            "val_split_timestamp": t2.isoformat(),
            "train_size": int(len(train_df)),
            "val_size": int(len(val_df)),
            "holdout_size": int(len(holdout_df)),
        },
        "learning_curve": [
            {"n_estimators": n, "val_mae": float(v[1])}
            for n, v in sweep_results.items()
        ],
        "optimal_n_estimators": optimal_n,
        "metrics": {
            "train": {"mae": mae_train, "r2": r2_train},
            "validation": {
                "mae": mae_val,
                "rmse": rmse_val,
                "r2": r2_val,
                "pearson_r": pearson_val,
                "n_rows": int(len(y_val)),
            },
            "overfitting_warning": overfitting_warning,
        },
        "feature_stability": {
            "stable": stable,
            "unstable": unstable,
        },
        "profit_simulation": {
            "full": full_sim,
            "confident_only": confident_sim,
        },
    }

    if args.final:
        log.warning(
            "HOLDOUT SET ACCESSED — do not iterate further after viewing these results."
        )
        y_pred_holdout = main_model.predict(X_holdout)
        mae_h = float(mean_absolute_error(y_holdout, y_pred_holdout))
        rmse_h = float(np.sqrt(mean_squared_error(y_holdout, y_pred_holdout)))
        r2_h = float(r2_score(y_holdout, y_pred_holdout))
        pearson_h = float(pearsonr(y_pred_holdout, y_holdout)[0]) if len(y_holdout) > 1 else float("nan")
        log.info(
            "Holdout: mae=%.5f rmse=%.5f r2=%.5f pearson_r=%.5f (n=%d)",
            mae_h, rmse_h, r2_h, pearson_h, len(y_holdout),
        )
        metrics["holdout_metrics"] = {
            "mae": mae_h,
            "rmse": rmse_h,
            "r2": r2_h,
            "pearson_r": pearson_h,
            "n_rows": int(len(y_holdout)),
        }

    metrics_path.write_text(
        json.dumps(metrics, indent=2, default=str), encoding="utf-8"
    )
    log.info("Saved metrics -> %s", metrics_path)
    log.info("=" * 72)
    log.info("Training pipeline complete")
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
