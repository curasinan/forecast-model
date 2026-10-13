"""Research-only GTM paper loop: EMOS CDF vs latest ForecastEx delayed quotes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from climate.emos import EmosModel
from climate.fit import model_fingerprint
from climate.ladder import (
    ladder_table,
    latest_gtm_quotes,
    latest_sweep_path,
    sweep_ts_from_path,
)
from climate.paper_criteria import (
    ClimateEdgeGate,
    ClimateGoLiveCriteria,
    apply_edge_gate,
    evaluate_hindcast_go_live,
)
from climate.paths import data_root
from src.domain.fees import FeeModel, FlatCommissionFeeModel

PAPER_REL = ("paper", "gtm_signals.json")
HISTORY_REL = ("paper", "gtm_signals_history.jsonl")

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_ts_utc(dt: datetime) -> str:
    """UTC ``datetime`` -> the payload's ``TS_FORMAT`` string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts_utc(text: str) -> datetime:
    """Inverse of :func:`format_ts_utc`."""
    return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)


def predictive_by_target_month(
    frame: pd.DataFrame,
    model: EmosModel,
) -> dict[str, tuple[float, float]]:
    """One (mu, sigma) per target month: shortest available lead (freshest init)."""
    pred = model.predict(frame)
    work = frame[["target_month", "lead_month", "y_C"]].copy()
    work["mu"] = pred["mu"].to_numpy()
    work["sigma"] = pred["sigma"].to_numpy()
    work = work[np.isfinite(work["mu"]) & np.isfinite(work["sigma"])]
    if work.empty:
        return {}
    work = work.sort_values(["target_month", "lead_month"])
    best = work.drop_duplicates("target_month", keep="first")
    return {
        str(row.target_month): (float(row.mu), float(row.sigma))
        for row in best.itertuples(index=False)
    }


def freshest_init_by_target(
    frame: pd.DataFrame, fitted_leads: Iterable[int] | None = None
) -> dict[str, str]:
    """SEAS5 init month ('YYYY-MM') behind each target's predictive pick."""
    needed = ("target_month", "lead_month", "init_date", "fc_mean_C", "fc_sd_C")
    if any(col not in frame.columns for col in needed):
        return {}
    work = frame[list(needed)].copy()
    ok = np.isfinite(work["fc_mean_C"]) & np.isfinite(work["fc_sd_C"])
    if fitted_leads is not None:
        fitted = {int(lead) for lead in fitted_leads}
        ok &= work["lead_month"].astype(int).isin(fitted)
    work = work[ok]
    if work.empty:
        return {}
    work = work.sort_values(["target_month", "lead_month"])
    best = work.drop_duplicates("target_month", keep="first")
    return {
        str(row.target_month): pd.Timestamp(row.init_date).strftime("%Y-%m")
        for row in best.itertuples(index=False)
    }


UNKNOWN_INIT = "unknown"


def inits_for_priced_targets(
    inits: Mapping[str, str], ladder: pd.DataFrame
) -> dict[str, str]:
    """Restrict ``inits`` to the target months actually on the priced ladder."""
    if ladder is None or ladder.empty or "target_month" not in ladder.columns:
        return {}
    if not inits:
        return {}
    priced = sorted({str(t) for t in ladder["target_month"].dropna()})
    return {t: inits.get(t, UNKNOWN_INIT) for t in priced}


def run_paper(
    frame: pd.DataFrame,
    model: EmosModel,
    *,
    ticks_root: str | Path | None = None,
    quotes_df: pd.DataFrame | None = None,
    universe_json: str | Path | None = None,
    fee_model: FeeModel | None = None,
    gate: ClimateEdgeGate | None = None,
    summary: pd.DataFrame | None = None,
    paper_days: int = 0,
    resolved_months: int = 0,
    require_live_paper: bool = False,
) -> dict[str, Any]:
    """Score the current GTM ladder. Returns a JSON-ready payload; no orders."""
    fee_model = fee_model or FlatCommissionFeeModel(0.01)
    predictive = predictive_by_target_month(frame, model)
    quotes_injected = quotes_df is not None
    if not quotes_injected:
        if ticks_root is None:
            raise ValueError("run_paper needs ticks_root or quotes_df")
        quotes_df = latest_gtm_quotes(ticks_root, universe_json=universe_json)
    sweep_ts_utc = None
    if not quotes_injected:
        parsed = sweep_ts_from_path(latest_sweep_path(ticks_root))
        sweep_ts_utc = format_ts_utc(parsed) if parsed is not None else None
    ladder = ladder_table(quotes_df, predictive, fee_model=fee_model)
    gated = apply_edge_gate(ladder, gate=gate, fee_model=fee_model)
    go_live = evaluate_hindcast_go_live(
        summary if summary is not None else pd.DataFrame(),
        criteria=ClimateGoLiveCriteria(),
        paper_days=paper_days,
        resolved_months=resolved_months,
        require_live_paper=require_live_paper,
    )
    n_tradeable = int(gated["tradeable"].sum()) if not gated.empty and "tradeable" in gated.columns else 0
    return {
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sweep_ts_utc": sweep_ts_utc,
        "model_fingerprint": model_fingerprint(model),
        "inits": inits_for_priced_targets(
            freshest_init_by_target(frame, fitted_leads=model.params.keys()), gated
        ),
        "n_quotes": int(len(quotes_df)),
        "n_ladder": int(len(gated)),
        "n_tradeable": n_tradeable,
        "predictive_targets": {k: {"mu": v[0], "sigma": v[1]} for k, v in predictive.items()},
        "go_live": go_live.to_dict(),
        "signals": gated.to_dict(orient="records") if not gated.empty else [],
    }


def write_paper_payload(payload: Mapping[str, Any], *, root: str | Path | None = None) -> Path:
    path = data_root(root).joinpath(*PAPER_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def append_paper_history(payload: Mapping[str, Any], *, root: str | Path | None = None) -> Path:
    """Append ``payload`` as one JSON line to ``paper/gtm_signals_history.jsonl``."""
    path = data_root(root).joinpath(*HISTORY_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    return path
