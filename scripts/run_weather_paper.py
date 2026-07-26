"""Paper loop: xmodel_prevruns_v1 EMOS → weather_edge → go-live check.

Loads persisted EMOS params only when metadata.feature_set matches
``xmodel_prevruns_v1``. Serve features come from Previous Runs history
(same definition as training), not live ensemble-member stats.

Does NOT call decision/edge_calculator.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from weather.buckets import Bucket, p_bucket_gaussian
from weather.calibrate import evaluate_binary_probs
from weather.edge import evaluate_weather_edge
from weather.emos import predict_emos
from weather.market_quotes import DEFAULT_POLYMARKET_DB, resolve_quote
from weather.open_meteo import OpenMeteoClient, OpenMeteoLicenseError
from weather.paper_criteria import GoLiveCriteria, evaluate_go_live
from weather.paths import resolve_weather_data_root, warn_if_onedrive
from weather.serve_features import (
    XModelFeatures,
    build_xmodel_features,
    load_emos_for_feature_set,
)
from weather.stations import load_station_registry
from weather.storage import FEATURE_SCHEMA_VERSION, init_weather_db, utc_now_iso
from weather.training import FEATURE_SET, label_priority_rank_sql

log = logging.getLogger("run_weather_paper")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def load_active_weather_markets(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM weather_markets").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def persist_probability(
    db_path: Path,
    market_id: str,
    model_version: str,
    model_p: float,
    mu: float | None,
    sigma: float | None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO model_probabilities (
                market_id, decision_ts_utc, model_version, model_p,
                mu, sigma, feature_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET
                model_p=excluded.model_p,
                mu=excluded.mu,
                sigma=excluded.sigma
            """,
            (
                market_id,
                utc_now_iso(),
                model_version,
                model_p,
                mu,
                sigma,
                FEATURE_SCHEMA_VERSION,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _maybe_open_meteo_client(allow_fetch: bool) -> OpenMeteoClient | None:
    if not allow_fetch:
        return None
    try:
        return OpenMeteoClient(for_trading=True)
    except OpenMeteoLicenseError as exc:
        log.warning("Open-Meteo fetch disabled: %s", exc)
        return None


def run_paper(
    *,
    min_edge: float = 0.05,
    market_mid_default: float = 0.5,
    spread_default: float = 0.04,
    paper_days: int = 0,
    polymarket_db: Path | str = DEFAULT_POLYMARKET_DB,
    require_live_quote: bool = False,
    fetch_features: bool = True,
    allow_noncommercial: bool = False,
    allow_live_clob: bool = True,
    skip_fixtures: bool = True,
    horizon_past_days: int = 0,
    horizon_future_days: int = 7,
) -> dict:
    if allow_noncommercial:
        from src.config import settings

        settings.open_meteo_allow_noncommercial = True

    root = resolve_weather_data_root()
    warning = warn_if_onedrive(root)
    if warning:
        log.warning("%s", warning)
    db_path = init_weather_db()

    emos_models = load_emos_for_feature_set(root, required_feature_set=FEATURE_SET)
    client = _maybe_open_meteo_client(fetch_features)
    registry = load_station_registry()

    markets = load_active_weather_markets(db_path)
    signals = []
    model_ps, outcomes, market_ps = [], [], []
    quotes_used = 0
    quotes_missing = 0
    features_ok = 0
    features_missing = 0
    emos_hits = 0
    skipped_stale = 0
    # Cache features per (station, date, metric) — many buckets share one day.
    feat_cache: dict[tuple[str, str, str], XModelFeatures | None] = {}
    today_utc = date.today()

    for mkt in markets:
        mid = str(mkt.get("market_id") or "")
        if skip_fixtures and mid.lower().startswith("wx-"):
            skipped_stale += 1
            continue
        metric = mkt.get("metric") or "high"
        valid = str(mkt["valid_local_date"])
        try:
            valid_d = date.fromisoformat(valid)
        except ValueError:
            features_missing += 1
            continue
        # Default: today..+7d. Optional lookback for offline / post-mortem paper.
        if valid_d < today_utc - timedelta(days=horizon_past_days):
            skipped_stale += 1
            continue
        if valid_d > today_utc + timedelta(days=horizon_future_days):
            skipped_stale += 1
            continue

        cache_key = (mkt["station_icao"], valid, metric)
        if cache_key not in feat_cache:
            station = registry.get(mkt["station_icao"])
            if station is None:
                feat_cache[cache_key] = None
            else:
                feat_cache[cache_key] = build_xmodel_features(
                    db_path,
                    station_icao=mkt["station_icao"],
                    valid_local_date=valid,
                    metric=metric,
                    client=client,
                    fetch_if_missing=fetch_features and client is not None,
                    registry=registry,
                )
        feats = feat_cache[cache_key]
        if feats is None:
            features_missing += 1
            continue
        features_ok += 1
        key = (feats.station_icao, feats.metric, feats.lead_day)
        bucket = Bucket(
            mkt.get("bucket_lo"), mkt.get("bucket_hi"), mkt.get("bucket_label") or ""
        )
        mu = sigma = None
        if key in emos_models:
            params, meta = emos_models[key]
            # Hard guard (load already filtered; belt-and-suspenders)
            if meta.get("feature_set") != FEATURE_SET:
                continue
            pred = predict_emos(params, feats.ens_mean, feats.ens_var)
            model_p = pred.p_bucket(bucket)
            mu, sigma = pred.mu, pred.sigma
            model_version = params.model_version
            emos_hits += 1
        else:
            # Cold start with the SAME feature definition (ens_mean / ens_var)
            mu = feats.ens_mean
            sigma = max(feats.ens_var**0.5, 2.0)
            model_p = p_bucket_gaussian(mu, sigma, bucket)
            model_version = f"raw_{FEATURE_SET}"

        if model_p != model_p:  # NaN
            continue

        quote = resolve_quote(
            mkt["market_id"],
            clob_token_ids=mkt.get("clob_token_ids"),
            db_path=polymarket_db,
            allow_live=allow_live_clob,
        )
        if quote is not None and quote.usable:
            market_mid = float(quote.midpoint)  # type: ignore[arg-type]
            spread = float(quote.spread) if quote.spread is not None else spread_default
            quote_source = quote.source
            quotes_used += 1
        else:
            quotes_missing += 1
            if require_live_quote:
                continue
            market_mid = market_mid_default
            spread = spread_default
            quote_source = "default_placeholder"

        sig = evaluate_weather_edge(model_p, market_mid, spread, min_edge=min_edge)
        persist_probability(db_path, mkt["market_id"], model_version, model_p, mu, sigma)
        signals.append(
            {
                "market_id": mkt["market_id"],
                "question": mkt["question"],
                "station_icao": mkt["station_icao"],
                "bucket_label": mkt["bucket_label"],
                "model_p": model_p,
                "model_version": model_version,
                "feature_set": feats.feature_set,
                "feature_source": feats.source,
                "lead_day": feats.lead_day,
                "ens_mean": feats.ens_mean,
                "ens_var": feats.ens_var,
                "n_models": feats.n_models,
                "market_mid": market_mid,
                "spread": spread,
                "quote_source": quote_source,
                "quote_ts": quote.timestamp if quote else None,
                "edge": sig.edge,
                "half_kelly": sig.half_kelly,
                "tradeable": sig.tradeable,
                "reason": sig.reason,
            }
        )

    conn = sqlite3.connect(db_path)
    try:
        # A station-date-metric can carry labels from several oracles
        # (nws_cli, iem_cli, wunderground_api, ...). Keep only the
        # highest-priority one so each prediction contributes exactly one
        # calibration row instead of fanning out across sources.
        cal_rows = conn.execute(
            f"""
            SELECT mp.model_p, l.label_value, wm.bucket_lo, wm.bucket_hi, wm.metric
              FROM model_probabilities mp
              JOIN weather_markets wm ON wm.market_id = mp.market_id
              JOIN resolution_labels l
                ON l.station_icao = wm.station_icao
               AND l.valid_local_date = wm.valid_local_date
               AND l.metric = wm.metric
               AND l.usable_for_training = 1
             WHERE NOT EXISTS (
                   SELECT 1 FROM resolution_labels b
                    WHERE b.station_icao = l.station_icao
                      AND b.valid_local_date = l.valid_local_date
                      AND b.metric = l.metric
                      AND b.usable_for_training = 1
                      AND ({label_priority_rank_sql('b')}, b.label_source)
                        < ({label_priority_rank_sql('l')}, l.label_source)
             )
            """
        ).fetchall()
    finally:
        conn.close()

    for model_p, label_value, lo, hi, _metric in cal_rows:
        b = Bucket(lo, hi)
        outcome = 1 if b.contains_truncated(float(label_value)) else 0
        model_ps.append(float(model_p))
        outcomes.append(outcome)
        market_ps.append(market_mid_default)

    report = (
        evaluate_binary_probs(model_ps, outcomes, market_ps=market_ps)
        if outcomes
        else evaluate_binary_probs([0.5], [0], market_ps=[0.5])
    )
    if not cal_rows:
        report.n = 0
    verdict = evaluate_go_live(report, paper_days=paper_days, criteria=GoLiveCriteria())

    out = {
        "generated_at": utc_now_iso(),
        "data_root": str(root),
        "feature_set": FEATURE_SET,
        "n_signals": len(signals),
        "tradeable": sum(1 for s in signals if s["tradeable"]),
        "quotes_used": quotes_used,
        "quotes_missing": quotes_missing,
        "features_ok": features_ok,
        "features_missing": features_missing,
        "skipped_stale": skipped_stale,
        "emos_loaded": len(emos_models),
        "emos_hits": emos_hits,
        "signals": signals,
        "calibration": report.to_dict(),
        "go_live": verdict.to_dict(),
    }
    out_path = root / "models" / f"paper_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    log.info(
        "signals=%s tradeable=%s emos_hits=%s/%s quotes=%s go_live=%s path=%s",
        len(signals),
        out["tradeable"],
        emos_hits,
        len(emos_models),
        quotes_used,
        verdict.passed,
        out_path,
    )
    return out


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Weather paper trading evaluation")
    parser.add_argument("--min-edge", type=float, default=0.05)
    parser.add_argument("--paper-days", type=int, default=0)
    parser.add_argument(
        "--require-live-quote",
        action="store_true",
        help="Skip markets without a polymarket.db snapshot mid",
    )
    parser.add_argument(
        "--no-fetch-features",
        action="store_true",
        help="Do not call Previous Runs API; use forecast_history_daily only",
    )
    parser.add_argument(
        "--allow-noncommercial",
        action="store_true",
        help="Allow public Open-Meteo host for live feature fetch",
    )
    parser.add_argument(
        "--polymarket-db",
        type=Path,
        default=DEFAULT_POLYMARKET_DB,
        help="Path to polymarket.db with snapshots",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Refresh weather_markets from Gamma / local polymarket.db first",
    )
    parser.add_argument(
        "--skip-gamma",
        action="store_true",
        help="With --discover, only scan local polymarket.db",
    )
    parser.add_argument(
        "--no-live-clob",
        action="store_true",
        help="Do not fall back to live CLOB when snapshots are missing",
    )
    parser.add_argument(
        "--include-fixtures",
        action="store_true",
        help="Include wx-* seed markets in the paper loop",
    )
    parser.add_argument(
        "--horizon-past-days",
        type=int,
        default=0,
        help="Include markets with valid_local_date up to N days before today",
    )
    parser.add_argument(
        "--horizon-future-days",
        type=int,
        default=7,
        help="Include markets with valid_local_date up to N days after today",
    )
    args = parser.parse_args(argv)
    if args.discover:
        from collectors.weather.discover_live_markets import discover_and_upsert

        discover_and_upsert(
            polymarket_db=args.polymarket_db,
            skip_gamma=args.skip_gamma,
        )
    run_paper(
        min_edge=args.min_edge,
        paper_days=args.paper_days,
        polymarket_db=args.polymarket_db,
        require_live_quote=args.require_live_quote,
        fetch_features=not args.no_fetch_features,
        allow_noncommercial=args.allow_noncommercial,
        allow_live_clob=not args.no_live_clob,
        skip_fixtures=not args.include_fixtures,
        horizon_past_days=args.horizon_past_days,
        horizon_future_days=args.horizon_future_days,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
