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
#: Run-by-run series (L8, Task 3.8): PAPER_REL is overwritten every run, so
#: this append-only sibling is the only place to recover model_fingerprint
#: drift (audit finding M2a) or past gate inputs after the fact.
HISTORY_REL = ("paper", "gtm_signals_history.jsonl")

#: Shared string form for every UTC timestamp in the paper-signals payload
#: (ts_utc, sweep_ts_utc): human-readable and unambiguous to re-parse.
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_ts_utc(dt: datetime) -> str:
    """UTC ``datetime`` -> the payload's ``TS_FORMAT`` string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts_utc(text: str) -> datetime:
    """Inverse of :func:`format_ts_utc`; used by the paper entrypoints to
    re-parse ``ts_utc`` / ``sweep_ts_utc`` before staleness-checking them
    (:func:`climate.paper_criteria.check_staleness`)."""
    return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)


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


def freshest_init_by_target(
    frame: pd.DataFrame, fitted_leads: Iterable[int] | None = None
) -> dict[str, str]:
    """SEAS5 init month ('YYYY-MM') behind each target's predictive pick.

    Mirrors :func:`predictive_by_target_month`'s own selection -- shortest
    available lead per target month, ties broken by first occurrence.

    SAFETY ASYMMETRY (read this before touching the filter below): without
    ``fitted_leads``, the "usable row" test here (finite ``fc_mean_C`` /
    ``fc_sd_C``) is a SUPERSET of what ``predictive_by_target_month``
    actually uses (a *model-predicted* finite (mu, sigma), which also
    requires the model to have fit that lead at all -- see
    ``EmosModel.fit``'s ``skipped`` dict). Whenever a lead is skipped, this
    function -- called without ``fitted_leads`` -- can therefore only ever
    report an init that is FRESHER THAN OR EQUAL TO the one the model
    actually priced off, because it may select a shorter lead the model
    never scored. For a staleness gate that direction is the DANGEROUS
    one: ``check_staleness``'s ``require_fresh_init`` check could PASS on
    this function's answer while the price ``predictive_by_target_month``
    actually produced was priced off a genuinely stale init. Pass
    ``fitted_leads`` (``run_paper`` passes ``model.params.keys()``) to
    close that gap: rows whose lead is not in ``fitted_leads`` are
    excluded from the usable set, making the selection exactly match what
    ``predictive_by_target_month`` would pick. Omitting ``fitted_leads``
    is only safe when the caller independently knows every lead present
    in the frame was fit.

    ``predictive_by_target_month``'s own 2-tuple return is untouched by
    this function -- ``climate.ladder.ladder_table`` still unpacks it as
    (mu, sigma).

    Returns ``{}`` -- not an error -- when the frame lacks ``init_date``
    or the other selection columns, so a caller with an older/synthetic
    frame still gets a usable (if empty) ``inits`` mapping instead of a
    crash; :func:`climate.paper_criteria.check_staleness` treats an empty
    mapping as "no target to check" rather than "stale".
    """
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


#: Init reported for a PRICED target that ``freshest_init_by_target`` could
#: not attribute -- never equal to a real 'YYYY-MM', so the gate refuses it.
UNKNOWN_INIT = "unknown"


def inits_for_priced_targets(
    inits: Mapping[str, str], ladder: pd.DataFrame
) -> dict[str, str]:
    """Restrict ``inits`` to the target months actually on the priced ladder.

    Final-review finding C-1: ``run_paper`` used to hand the staleness gate
    the freshest init of EVERY target month in the training frame --
    including every resolved month back to 1993 -- and
    :func:`climate.paper_criteria.check_staleness` requires the expected
    latest init for each, so any real frame was refused on every run.
    Only a PRICED target's init can make a price stale, so only those are
    gated.

    Fail closed on the scoping itself: when ``inits`` is non-empty (the
    frame can report inits at all) but a priced target has no entry, that
    target is kept with :data:`UNKNOWN_INIT`, which the gate refuses --
    never silently dropped. (``freshest_init_by_target`` with
    ``fitted_leads`` covers every target ``predictive_by_target_month``
    can price, so this is a belt, not an expected path.) An empty
    ``inits`` -- a frame without ``init_date``, the documented
    synthetic-frame path -- stays empty, and an empty ladder prices
    nothing, so it gates nothing.
    """
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
    """Score the current GTM ladder. Returns a JSON-ready payload; no orders.

    The payload carries two staleness inputs for the entrypoints (audit
    finding H2, ``climate.paper_criteria.check_staleness``):
    ``sweep_ts_utc`` -- parsed from the sweep file's own partition date +
    ``quotes-HHMMSSZ`` name when ``quotes_df`` was loaded from disk (via
    ``ticks_root``); None when a caller injected ``quotes_df`` directly
    (tests -- there is no sweep file to be stale) -- and ``inits``, the
    freshest SEAS5 init month behind each target ON THE PRICED LADDER
    (:func:`freshest_init_by_target`, called with ``fitted_leads=
    model.params.keys()`` so a lead the model skipped can never be
    reported as the init behind a price -- see that function's docstring
    for why the unfiltered form is unsafe here -- then scoped by
    :func:`inits_for_priced_targets`, final-review finding C-1: resolved
    history in the training frame is never priced, so it is never gated).
    It also carries
    ``model_fingerprint`` (audit finding M2a) -- the short sha256 of
    ``model``'s canonical JSON (:func:`climate.fit.model_fingerprint`) --
    so every order ``scripts/run_ibkr_paper.py`` places from this payload
    can be traced back to the exact fit that priced it.
    """
    fee_model = fee_model or FlatCommissionFeeModel(0.01)
    predictive = predictive_by_target_month(frame, model)
    quotes_injected = quotes_df is not None
    if not quotes_injected:
        if ticks_root is None:
            raise ValueError("run_paper needs ticks_root or quotes_df")
        quotes_df = latest_gtm_quotes(ticks_root, universe_json=universe_json)
    sweep_ts_utc = None
    if not quotes_injected:
        # Resolve the sweep file directly rather than reading quotes_df's
        # own sweep_path column: a sweep with zero GTM rows still needs a
        # real freshness check (the column-based read would otherwise
        # silently leave sweep_ts_utc None whenever the sweep happened to
        # carry no GTM quotes, defeating the staleness gate for exactly
        # the kind of degenerate sweep an operator most needs it to catch).
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
    """Append ``payload`` as one JSON line to ``paper/gtm_signals_history.jsonl``.

    L8 (Task 3.8, test hygiene + minor findings bundle). ``gtm_signals.json``
    (:func:`write_paper_payload`) is overwritten every run, so it can only
    ever answer "what does the model say right now" -- this file is the
    run-by-run series, one full payload per call in call order, letting an
    operator reconstruct model_fingerprint drift (audit finding M2a) or past
    gate inputs without a separate ledger.

    Callers must call this AFTER ``write_paper_payload`` has written the
    latest-snapshot file for the same run (``scripts/run_gtm_paper.py``
    does), so a run that refuses before ever producing a signals file (e.g.
    the staleness gate) never appends a history line for a run that wrote
    nothing. Each line is independently valid JSON (``json.dumps`` with no
    embedded newlines), so a reader can process the file streaming without
    ever loading the whole thing, and a crash between two appends leaves
    every previously-written line intact.
    """
    path = data_root(root).joinpath(*HISTORY_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str))
        f.write("\n")
    return path
