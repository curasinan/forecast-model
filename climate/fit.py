"""Fit GTM EMOS, score it out of sample, and write a diagnostics report.

Operational entry: ``scripts/fit_gtm_emos.py``. This module is the library
the script and the paper loop share: it never talks to IBKR or Polymarket.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from climate.bridge import Bridge
from climate.dataset import FRAME_COLUMNS, build_training_frame, frame_bridge
from climate.emos import (
    EmosModel,
    expanding_window_predictions,
    expanding_window_scores,
    interval_coverage,
    pit,
    pit_histogram,
    summarise_scores,
    target_year,
)
from climate.paths import data_root

MODEL_REL = ("models", "gtm_emos.json")
REPORT_MD_REL = ("reports", "gtm_emos_diagnostics.md")
REPORT_JSON_REL = ("reports", "gtm_emos_diagnostics.json")


def model_fingerprint(model: EmosModel) -> str:
    """Short, stable identity of a fitted model (audit finding M2a).

    A paper-trading measurement is only interpretable if every order
    record can be traced to the exact fitted model that produced it, so
    this is threaded through ``GtmFitResult.to_dict`` and the model
    artifact here, then ``climate.paper.run_paper``'s signals payload,
    then the run-ledger records ``scripts/run_ibkr_paper.py`` appends.

    Computed over ``model.to_json(indent=0)`` -- the model's own
    canonical serialisation, which never includes a ``fingerprint`` key
    -- so the field is always excluded from the hashed payload, and this
    is stable across repeated to_json()/from_json() round-trips of the
    same fit and changes whenever any ``LeadParams`` coefficient does.
    """
    return hashlib.sha256(model.to_json(indent=0).encode()).hexdigest()[:16]


@dataclass
class GtmFitResult:
    """In-memory product of one GTM fit + expanding-window evaluation."""

    frame: pd.DataFrame
    bridge: Bridge
    model: EmosModel
    scores: pd.DataFrame
    summary: pd.DataFrame
    oos_rows: pd.DataFrame
    pit_hist: pd.DataFrame
    coverage: dict[float, float]
    first_test_year: int
    last_test_year: int
    n_rows: int
    n_labelled: int
    trend: bool
    fitted_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fitted_at_utc": self.fitted_at_utc,
            "fingerprint": model_fingerprint(self.model),
            "trend": self.trend,
            "n_rows": self.n_rows,
            "n_labelled": self.n_labelled,
            "first_test_year": self.first_test_year,
            "last_test_year": self.last_test_year,
            "bridge": json.loads(self.bridge.to_json()),
            "emos": self.model.to_dict(),
            "summary": self.summary.to_dict(orient="records"),
            "coverage_oos": {str(k): v for k, v in self.coverage.items()},
            "pit_histogram": self.pit_hist.to_dict(orient="records"),
        }


def labelled_years(frame: pd.DataFrame) -> np.ndarray:
    y = np.asarray(frame["y_C"], dtype=float)
    years = target_year(frame)
    return np.unique(years[np.isfinite(y)])


def default_test_window(
    frame: pd.DataFrame, *, min_train_years: int = 8
) -> tuple[int, int]:
    """Expanding-window test years: leave the first ``min_train_years`` labelled years for train."""
    years = labelled_years(frame)
    if len(years) < min_train_years + 1:
        raise ValueError(
            f"need at least {min_train_years + 1} labelled years for an expanding window, "
            f"got {list(map(int, years))}"
        )
    return int(years[min_train_years]), int(years[-1])


def fit_gtm(
    frame: pd.DataFrame | None = None,
    *,
    root: str | Path | None = None,
    trend: bool = False,
    leads: range | list[int] = range(1, 7),
    first_test_year: int | None = None,
    last_test_year: int | None = None,
    min_train_targets: int = 24,
) -> GtmFitResult:
    """Build (or accept) the training frame, fit per-lead EMOS, score out of sample."""
    if frame is None:
        frame = build_training_frame(root)
    missing = [c for c in FRAME_COLUMNS if c not in frame.columns]
    if missing:
        raise KeyError(f"training frame missing columns: {missing}")
    bridge = frame_bridge(frame)
    leads_list = [int(x) for x in leads]
    labelled = frame[np.isfinite(frame["y_C"].to_numpy(dtype=float))]
    if labelled.empty:
        raise ValueError("training frame has no labelled rows (y_C all NaN)")

    model = EmosModel(trend=trend).fit(labelled, leads=leads_list, trend=trend)
    if first_test_year is None or last_test_year is None:
        lo, hi = default_test_window(frame)
        first_test_year = first_test_year if first_test_year is not None else lo
        last_test_year = last_test_year if last_test_year is not None else hi

    oos = expanding_window_predictions(
        frame,
        first_test_year=first_test_year,
        last_test_year=last_test_year,
        trend=trend,
        leads=leads_list,
        min_train_targets=min_train_targets,
    )
    scores = expanding_window_scores(
        frame,
        first_test_year=first_test_year,
        last_test_year=last_test_year,
        trend=trend,
        leads=leads_list,
        min_train_targets=min_train_targets,
    )
    summary = summarise_scores(scores)
    finite = oos[np.isfinite(oos["y_C"]) & np.isfinite(oos["mu_emos"])] if not oos.empty else oos
    if finite.empty:
        cov = {0.5: float("nan"), 0.9: float("nan")}
        hist = pit_histogram([])
    else:
        cov = interval_coverage(finite["y_C"], finite["mu_emos"], finite["sigma_emos"])
        hist = pit_histogram(pit(finite["y_C"], finite["mu_emos"], finite["sigma_emos"]))
    return GtmFitResult(
        frame=frame,
        bridge=bridge,
        model=model,
        scores=scores,
        summary=summary,
        oos_rows=oos,
        pit_hist=hist,
        coverage=cov,
        first_test_year=int(first_test_year),
        last_test_year=int(last_test_year),
        n_rows=int(len(frame)),
        n_labelled=int(len(labelled)),
        trend=bool(trend),
    )


def _fmt(v: float, digits: int = 3) -> str:
    if v is None or not np.isfinite(v):
        return "nan"
    return f"{v:.{digits}f}"


def render_markdown(result: GtmFitResult) -> str:
    """Human-readable diagnostics; the JSON sidecar is the machine contract."""
    b = result.bridge
    lines = [
        "# GTM EMOS diagnostics",
        "",
        f"Fitted (UTC): {result.fitted_at_utc}",
        f"Trend term: {result.trend} (origin 2010.0)",
        f"Rows / labelled: {result.n_rows} / {result.n_labelled}",
        f"Expanding-window test years: {result.first_test_year}–{result.last_test_year}",
        "",
        "## ERA5 → NOAA bridge",
        "",
        f"- model: `{b.params['model']}`",
        f"- beta (degC per K): {_fmt(b.beta, 4)}",
        f"- R²: {_fmt(b.r2, 4)}  residual sd: {_fmt(b.resid_sd, 4)}  n={b.n_obs}",
        f"- fit window: {b.fit_start} .. {b.fit_end}",
        "",
        "## Per-lead EMOS (in-sample CRPS of the final fit)",
        "",
        "| lead | a | b | c | d | c_t | n | train CRPS | converged |",
        "| ---: | --: | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for lead, p in sorted(result.model.params.items()):
        lines.append(
            f"| {lead} | {_fmt(p.a, 4)} | {_fmt(p.b, 4)} | {_fmt(p.c, 4)} | {_fmt(p.d, 4)} | "
            f"{_fmt(p.c_t, 4)} | {p.n_train} | {_fmt(p.train_crps, 4)} | {p.converged} |"
        )
    if result.model.skipped:
        lines.append("")
        lines.append("Skipped leads: " + ", ".join(f"{k} ({v})" for k, v in result.model.skipped.items()))
    lines += [
        "",
        "## Out-of-sample CRPS (expanding window, target year held out)",
        "",
        "| lead | n | years | CRPS EMOS | CRPS clim | CRPS trend | skill vs clim | skill vs trend | cover50 | cover90 |",
        "| ---: | --: | --: | --: | --: | --: | --: | --: | --: | --: |",
    ]
    for _, row in result.summary.iterrows():
        lines.append(
            f"| {int(row['lead_month'])} | {int(row['n'])} | {int(row['years'])} | "
            f"{_fmt(row['crps_emos'], 4)} | {_fmt(row['crps_clim'], 4)} | {_fmt(row['crps_trend'], 4)} | "
            f"{_fmt(row['skill_vs_clim'], 3)} | {_fmt(row['skill_vs_trend'], 3)} | "
            f"{_fmt(row['cover50_emos'], 3)} | {_fmt(row['cover90_emos'], 3)} |"
        )
    lines += [
        "",
        f"Pooled OOS coverage: 50%={_fmt(result.coverage.get(0.5), 3)}  "
        f"90%={_fmt(result.coverage.get(0.9), 3)}",
        "",
        "## PIT histogram (OOS, 10 bins)",
        "",
        "| lo | hi | count | frac | expected |",
        "| --: | --: | --: | --: | --: |",
    ]
    for _, row in result.pit_hist.iterrows():
        lines.append(
            f"| {_fmt(row['lo'], 1)} | {_fmt(row['hi'], 1)} | {int(row['count'])} | "
            f"{_fmt(row['frac'], 3)} | {_fmt(row['expected_frac'], 2)} |"
        )
    lines += [
        "",
        "## Notes",
        "",
        "- Lead 1 is the initialisation month (empirical, see `climate/dataset.py`).",
        "- Strictly-greater strike semantics live in `climate/ladder.py`, not here.",
        "- NOAA month M publishes in the first half of M+1; the expanding window",
        "  slightly overstates the label set by that lag (recorded in `climate/emos.py`).",
        "",
    ]
    return "\n".join(lines)


def write_artifacts(
    result: GtmFitResult,
    *,
    root: str | Path | None = None,
    model_path: str | Path | None = None,
    report_md: str | Path | None = None,
    report_json: str | Path | None = None,
) -> dict[str, Path]:
    """Persist EMOS JSON + markdown/JSON diagnostics under the climate data root."""
    base = data_root(root)
    paths = {
        "model": Path(model_path) if model_path else base.joinpath(*MODEL_REL),
        "report_md": Path(report_md) if report_md else base.joinpath(*REPORT_MD_REL),
        "report_json": Path(report_json) if report_json else base.joinpath(*REPORT_JSON_REL),
    }
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
    # Injected after the fact, not via model.to_json(path) directly:
    # EmosModel's own serialisation never carries a fingerprint (that
    # field is this module's concern, and must stay out of the hashed
    # payload -- see model_fingerprint's docstring), so the model JSON
    # written to disk here has the field merged in alongside it.
    model_payload = result.model.to_dict()
    model_payload["fingerprint"] = model_fingerprint(result.model)
    paths["model"].write_text(
        json.dumps(model_payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    paths["report_md"].write_text(render_markdown(result), encoding="utf-8")
    payload = result.to_dict()
    payload["artifact_paths"] = {k: str(v) for k, v in paths.items()}
    paths["report_json"].write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return paths
