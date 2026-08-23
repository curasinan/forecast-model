"""Research-only GTM paper loop: EMOS CDF vs latest ForecastEx delayed quotes.

Does not place orders, does not import a venue SDK, and does not touch
the Polymarket executor. Quotes come from the IBKR recorder's parquet
sweeps (or an injected DataFrame in tests). Signals are logged as JSON
under the climate data root.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from climate.emos import EmosModel
from climate.ladder import ladder_table, latest_gtm_quotes
from climate.paper_criteria import (
    ClimateEdgeGate,
    ClimateGoLiveCriteria,
    apply_edge_gate,
    evaluate_hindcast_go_live,
)
from climate.paths import data_root
from src.domain.fees import FeeModel, FlatCommissionFeeModel

PAPER_REL = ("paper", "gtm_signals.json")


def predictive_by_target_month(
    frame: pd.DataFrame,
    model: EmosModel,
) -> dict[str, tuple[float, float]]:
    """One (mu, sigma) per target month: shortest available lead (freshest init).

    Unresolved months (y_C NaN) are included — those are the live contracts.
    Rows whose lead was not fit, or whose inputs are non-finite, are dropped.
    """
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
    if quotes_df is None:
        if ticks_root is None:
            raise ValueError("run_paper needs ticks_root or quotes_df")
        quotes_df = latest_gtm_quotes(ticks_root, universe_json=universe_json)
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
