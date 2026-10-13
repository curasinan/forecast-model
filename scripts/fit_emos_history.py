"""Fit EMOS per station x metric x lead_day from historical training frames."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from weather.emos import crps_gaussian, fit_emos, predict_emos  # noqa: E402
from weather.model_store import save_emos_params  # noqa: E402
from weather.stations import StationRegistry, load_station_registry  # noqa: E402
from weather.storage import FEATURE_SCHEMA_VERSION, init_weather_db, utc_now_iso  # noqa: E402
from weather.training import (  # noqa: E402
    DEFAULT_MODELS,
    TrainingFrameSpec,
    build_training_frame,
)

log = logging.getLogger("fit_emos_history")

DEFAULT_STATIONS = ("KLGA", "KORD", "KLAX")
DEFAULT_MIN_SAMPLES = 25


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def parse_lead_days(value: str) -> list[int]:
    """Accept '0-6' ranges or '0,2,4' lists."""
    value = value.strip()
    if "-" in value and "," not in value:
        lo, hi = value.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(x) for x in value.split(",") if x.strip()]


def fit_group(
    db_path: Path | str,
    spec: TrainingFrameSpec,
    *,
    registry: StationRegistry,
    min_samples: int,
    model_root: Path | None,
    dry_run: bool,
) -> dict | None:
    """Fit one (station, metric, lead_day) group; returns a summary dict."""
    frame = build_training_frame(db_path, spec, registry=registry)
    n = len(frame)
    if n < min_samples:
        log.info(
            "SKIP %s %s lead%s: n=%s < min_samples=%s",
            spec.station_icao, spec.metric, spec.lead_day, n, min_samples,
        )
        return None
    obs = [float(v) for v in frame["obs_value"]]
    means = [float(v) for v in frame["ens_mean"]]
    variances = [float(v) for v in frame["ens_var"]]
    params = fit_emos(
        obs,
        means,
        variances,
        station_icao=spec.station_icao,
        lead_day=spec.lead_day,
        metric=spec.metric,
    )
    crps_values = []
    for y, m, v in zip(obs, means, variances):
        pred = predict_emos(params, m, v)
        crps_values.append(crps_gaussian(y, pred.mu, pred.sigma))
    mean_crps = sum(crps_values) / len(crps_values)
    source_counts = {
        str(k): int(v) for k, v in frame["label_source"].value_counts().items()
    }
    primary_source = max(source_counts, key=source_counts.get)
    metadata = {
        "n_samples": n,
        "train_start": str(frame["valid_local_date"].min()),
        "train_end": str(frame["valid_local_date"].max()),
        "mean_crps_insample": round(mean_crps, 4),
        "feature_set": spec.feature_set,
        "models": list(spec.models),
        "decision_hour_local": spec.decision_hour_local,
        "min_samples_gate": min_samples,
        "label_source": primary_source,
        "label_source_counts": source_counts,
        "fitted_at": utc_now_iso(),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
    }
    path = None
    if not dry_run:
        path = save_emos_params(params, metadata=metadata, root=model_root)
    return {
        "station_icao": spec.station_icao,
        "metric": spec.metric,
        "lead_day": spec.lead_day,
        "n": n,
        "a": params.a,
        "b": params.b,
        "c": params.c,
        "d": params.d,
        "mean_crps": mean_crps,
        "label_source": primary_source,
        "path": str(path) if path else "(dry-run)",
    }


def fit_all(
    db_path: Path | str,
    *,
    stations: list[str],
    metrics: list[str],
    lead_days: list[int],
    models: tuple[str, ...] = DEFAULT_MODELS,
    decision_hour: int = 12,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    model_root: Path | None = None,
    dry_run: bool = False,
    registry: StationRegistry | None = None,
) -> list[dict]:
    registry = registry or load_station_registry()
    if min_samples < 5:
        # fit_emos silently cold-starts below 5 samples; never persist that.
        log.warning("min_samples raised from %s to 5 (fit_emos cold-start threshold)", min_samples)
        min_samples = 5
    results: list[dict] = []
    for icao in stations:
        for metric in metrics:
            for lead in lead_days:
                spec = TrainingFrameSpec(
                    station_icao=icao,
                    metric=metric,
                    lead_day=lead,
                    models=models,
                    decision_hour_local=decision_hour,
                )
                summary = fit_group(
                    db_path,
                    spec,
                    registry=registry,
                    min_samples=min_samples,
                    model_root=model_root,
                    dry_run=dry_run,
                )
                if summary:
                    results.append(summary)
    return results


def print_summary(results: list[dict]) -> None:
    if not results:
        return
    header = (
        f"{'station':<8}{'metric':<7}{'lead':<5}{'n':>5}  {'a':>7} {'b':>6} {'c':>7} "
        f"{'d':>6}  {'crps':>6}  {'labels':<10}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['station_icao']:<8}{r['metric']:<7}{r['lead_day']:<5}{r['n']:>5}  "
            f"{r['a']:>7.2f} {r['b']:>6.3f} {r['c']:>7.2f} {r['d']:>6.3f}  "
            f"{r['mean_crps']:>6.3f}  {r['label_source']:<10}"
        )


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Fit and persist EMOS params from historical training data"
    )
    parser.add_argument("--station", action="append", help="ICAO; repeatable")
    parser.add_argument("--metric", default="both", choices=["high", "low", "both"])
    parser.add_argument("--lead-days", default="0-6", help="'0-6' range or '0,1,2' list")
    parser.add_argument("--models", action="append", help="Open-Meteo model id; repeatable")
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--decision-hour", type=int, default=12)
    parser.add_argument("--db-path", help="Override weather.db path")
    parser.add_argument("--model-root", help="Override data root for saved params")
    parser.add_argument("--dry-run", action="store_true", help="Fit and report, no save")
    args = parser.parse_args(argv)

    stations = [s.upper() for s in (args.station or DEFAULT_STATIONS)]
    metrics = ["high", "low"] if args.metric == "both" else [args.metric]
    lead_days = parse_lead_days(args.lead_days)
    models = tuple(args.models) if args.models else DEFAULT_MODELS
    db_path = Path(args.db_path) if args.db_path else init_weather_db()
    model_root = Path(args.model_root) if args.model_root else None

    results = fit_all(
        db_path,
        stations=stations,
        metrics=metrics,
        lead_days=lead_days,
        models=models,
        decision_hour=args.decision_hour,
        min_samples=args.min_samples,
        model_root=model_root,
        dry_run=args.dry_run,
    )
    print_summary(results)
    attempted = len(stations) * len(metrics) * len(lead_days)
    log.info("Fit %s/%s groups", len(results), attempted)
    if not results:
        log.error("No group reached min_samples=%s — nothing persisted", args.min_samples)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
