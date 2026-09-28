"""Bridge robustness study (audit H4): hindcast skill with the bridge fit inside each window.

The production hindcast (``climate.fit.fit_gtm`` via ``build_training_frame``)
fits the ERA5 -> NOAA :class:`climate.bridge.Bridge` ONCE on the full
ERA5/NOAA overlap, then runs the expanding-window EMOS evaluation on the
resulting ``fc_mean_C`` / ``fc_sd_C``. The EMOS and both baselines are
refit per test year on strictly earlier targets, but the bridge feeding
them has already seen every test year's (and later years') NOAA labels.
That is a leak of future information into past test years ("leaky").

This study measures how much that leak flatters the reported skill. For
each test year Y it

  1. fits a Bridge on ERA5/NOAA months strictly before Y
     (``Bridge.fit(start=None, end=f"{Y-1}-12")``),
  2. rebuilds the whole training frame with that window bridge
     (``build_frame_from_inputs(..., bridge=window_bridge)``) so both the
     train rows (target year < Y) and the test rows (target year == Y) of
     that window carry window-bridge ``fc_mean_C`` / ``fc_sd_C``,
  3. scores that single year with the unchanged expanding-window machinery
     (``expanding_window_scores(frame, Y, Y, ...)``),

then aggregates the per-year rows with ``summarise_scores`` ("honest") and
runs ``evaluate_hindcast_go_live`` on the honest summary with the default,
unchanged criteria. The leaky side is the production computation verbatim:
``expanding_window_scores`` over the same years on the full-bridge frame.

Only ``fc_mean_C`` / ``fc_sd_C`` differ between the two sides: labels,
rows, test years and both climatology baselines are identical, so the
per-lead comparison is paired and the delta is attributable to the bridge.

Additive only: nothing in ``climate/`` changes. Read-only on the data root
(the root is resolved WITHOUT ``climate.paths.data_root``'s mkdir side
effect); the only writes are the two ``--out`` files.

Usage::

    python scripts/study_bridge_window.py --out docs/gtm_bridge_robustness.md \\
        --json-out docs/gtm_bridge_robustness.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.bridge import Bridge, to_year_month_index  # noqa: E402
from climate.dataset import (  # noqa: E402
    build_frame_from_inputs,
    load_era5,
    load_noaa_gtm,
    load_seas5,
)
from climate.emos import expanding_window_scores, summarise_scores, target_year  # noqa: E402
from climate.fit import MODEL_REL, default_test_window  # noqa: E402
from climate.paper_criteria import GoLiveVerdict, evaluate_hindcast_go_live  # noqa: E402
from climate.paths import ENV_VAR  # noqa: E402
from climate.reduce import ERA5_PARQUET, SEAS5_PARQUET  # noqa: E402

log = logging.getLogger("study_bridge_window")

#: Variant name -> EMOS trend flag.
VARIANTS = {"notrend": False, "trend": True}
#: docs/gtm_model_report.md: "Chosen: the trend variant ... It is the operational gtm_emos.json".
DOCUMENTED_OPERATIONAL_TREND = True

COMPARISON_COLUMNS = [
    "lead_month",
    "years",
    "n_leaky",
    "n_honest",
    "crps_emos_leaky",
    "crps_emos_honest",
    "crps_trend",
    "crps_clim",
    "skill_vs_trend_leaky",
    "skill_vs_trend_honest",
    "skill_delta",
    "skill_vs_clim_leaky",
    "skill_vs_clim_honest",
    "cover50_leaky",
    "cover50_honest",
    "cover90_leaky",
    "cover90_honest",
]


# --------------------------------------------------------------------------- #
# the study
# --------------------------------------------------------------------------- #


def window_bridge(era5_K: pd.Series, noaa_C: pd.Series, test_year: int) -> Bridge:
    """Bridge fit on ERA5/NOAA months strictly before ``test_year`` (inclusive end Y-1-12)."""
    return Bridge().fit(era5_K, noaa_C, start=None, end=f"{int(test_year) - 1}-12")


@dataclass
class BridgeWindowStudy:
    """Leaky (full-history bridge) vs honest (in-window bridge) hindcast for one EMOS variant."""

    trend: bool
    leads: list[int]
    first_test_year: int
    last_test_year: int
    min_train_targets: int
    full_bridge: Bridge
    window_bridges: dict[int, Bridge]
    leaky_scores: pd.DataFrame
    honest_scores: pd.DataFrame
    leaky_summary: pd.DataFrame
    honest_summary: pd.DataFrame
    comparison: pd.DataFrame
    leaky_verdict: GoLiveVerdict
    honest_verdict: GoLiveVerdict
    #: test_year -> n_test_rows / mean and mean-abs (window - full) fc_mean_C on scored rows
    window_shifts: dict[int, dict[str, float]] = field(default_factory=dict)
    runtime_s: float = float("nan")

    @property
    def variant(self) -> str:
        return "trend" if self.trend else "notrend"

    def bridge_table(self) -> pd.DataFrame:
        """One row per window bridge, with its drift vs the full-history bridge.

        The leak's size is reported as the shift of ``fc_mean_C`` (window
        minus full bridge, degC) on the rows actually scored for that test
        year -- NOT as an alpha difference: alpha and beta trade off
        against the ~287 K ERA5 level, so alpha deltas alone look huge
        while the prediction barely moves.
        """
        full = self.full_bridge
        rows = []
        for year in sorted(self.window_bridges):
            b = self.window_bridges[year]
            shift = self.window_shifts.get(year, {})
            rows.append(
                {
                    "test_year": int(year),
                    "fit_start": b.fit_start,
                    "fit_end": b.fit_end,
                    "n_obs": int(b.n_obs),
                    "beta": float(b.beta),
                    "r2": float(b.r2),
                    "resid_sd": float(b.resid_sd),
                    "beta_minus_full": float(b.beta - full.beta),
                    "n_test_rows": int(shift.get("n_test_rows", 0)),
                    "test_fc_mean_shift_C": float(shift.get("test_fc_mean_shift_C", np.nan)),
                    "test_fc_mean_abs_shift_C": float(
                        shift.get("test_fc_mean_abs_shift_C", np.nan)
                    ),
                    "alpha": {str(m): float(b.alpha[m]) for m in range(1, 13)},
                }
            )
        return pd.DataFrame(rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "trend": self.trend,
            "leads": list(self.leads),
            "first_test_year": self.first_test_year,
            "last_test_year": self.last_test_year,
            "min_train_targets": self.min_train_targets,
            "runtime_s": self.runtime_s,
            "full_bridge": self.full_bridge.params,
            "window_bridges": self.bridge_table().to_dict(orient="records"),
            "comparison": self.comparison.to_dict(orient="records"),
            "leaky_summary": self.leaky_summary.to_dict(orient="records"),
            "honest_summary": self.honest_summary.to_dict(orient="records"),
            "leaky_scores": self.leaky_scores.to_dict(orient="records"),
            "honest_scores": self.honest_scores.to_dict(orient="records"),
            "leaky_verdict": self.leaky_verdict.to_dict(),
            "honest_verdict": self.honest_verdict.to_dict(),
        }


def compare_summaries(leaky: pd.DataFrame, honest: pd.DataFrame) -> pd.DataFrame:
    """Per-lead paired table: leaky vs honest skill and the delta (honest - leaky)."""
    if leaky.empty or honest.empty:
        return pd.DataFrame(columns=COMPARISON_COLUMNS)
    merged = leaky.merge(honest, on="lead_month", how="outer", suffixes=("_leaky", "_honest"))
    merged = merged.sort_values("lead_month").reset_index(drop=True)
    out = pd.DataFrame(
        {
            "lead_month": merged["lead_month"].astype(int),
            "years": merged["years_honest"],
            "n_leaky": merged["n_leaky"],
            "n_honest": merged["n_honest"],
            "crps_emos_leaky": merged["crps_emos_leaky"],
            "crps_emos_honest": merged["crps_emos_honest"],
            # bridge-free baselines: identical on both sides (paired rows)
            "crps_trend": merged["crps_trend_honest"],
            "crps_clim": merged["crps_clim_honest"],
            "skill_vs_trend_leaky": merged["skill_vs_trend_leaky"],
            "skill_vs_trend_honest": merged["skill_vs_trend_honest"],
            "skill_delta": merged["skill_vs_trend_honest"] - merged["skill_vs_trend_leaky"],
            "skill_vs_clim_leaky": merged["skill_vs_clim_leaky"],
            "skill_vs_clim_honest": merged["skill_vs_clim_honest"],
            "cover50_leaky": merged["cover50_emos_leaky"],
            "cover50_honest": merged["cover50_emos_honest"],
            "cover90_leaky": merged["cover90_emos_leaky"],
            "cover90_honest": merged["cover90_emos_honest"],
        }
    )
    return out[COMPARISON_COLUMNS]


def run_study(
    seas5: pd.DataFrame,
    era5_K: pd.Series,
    noaa_C: pd.Series,
    *,
    first_test_year: int | None = None,
    last_test_year: int | None = None,
    trend: bool = False,
    leads: Iterable[int] = range(1, 7),
    min_train_targets: int = 24,
) -> BridgeWindowStudy:
    """Score one EMOS variant twice: full-history bridge (leaky) vs in-window bridge (honest)."""
    t0 = time.perf_counter()
    leads_list = [int(x) for x in leads]

    # Leaky side == production: bridge=None fits on the full ERA5/NOAA overlap.
    leaky_frame = build_frame_from_inputs(seas5, era5_K, noaa_C)
    full_bridge: Bridge = leaky_frame.attrs["bridge"]
    if first_test_year is None or last_test_year is None:
        lo, hi = default_test_window(leaky_frame)
        first_test_year = lo if first_test_year is None else first_test_year
        last_test_year = hi if last_test_year is None else last_test_year
    first_test_year, last_test_year = int(first_test_year), int(last_test_year)
    if last_test_year < first_test_year:
        raise ValueError(f"empty test window {first_test_year}..{last_test_year}")

    leaky_scores = expanding_window_scores(
        leaky_frame,
        first_test_year,
        last_test_year,
        trend=trend,
        leads=leads_list,
        min_train_targets=min_train_targets,
    )

    # Honest side: a fresh bridge per test year, fit strictly before it.
    keys = ["init_date", "lead_month"]
    row_year = target_year(leaky_frame)
    scorable = leaky_frame["lead_month"].isin(leads_list).to_numpy() & np.isfinite(
        leaky_frame["y_C"].to_numpy(dtype=float)
    )
    window_bridges: dict[int, Bridge] = {}
    window_shifts: dict[int, dict[str, float]] = {}
    parts: list[pd.DataFrame] = []
    for year in range(first_test_year, last_test_year + 1):
        try:
            wb = window_bridge(era5_K, noaa_C, year)
        except ValueError as exc:
            raise ValueError(
                f"test year {year}: cannot fit the in-window bridge on months before "
                f"{year} ({exc}); start the study later with --first-test-year"
            ) from exc
        window_bridges[year] = wb
        frame = build_frame_from_inputs(seas5, era5_K, noaa_C, bridge=wb)
        if not frame[keys].equals(leaky_frame[keys]):  # same seas5 => same row order
            raise RuntimeError("window frame rows do not align with the full-bridge frame")
        sel = scorable & (row_year == year)
        diff = frame["fc_mean_C"].to_numpy()[sel] - leaky_frame["fc_mean_C"].to_numpy()[sel]
        window_shifts[year] = {
            "n_test_rows": int(sel.sum()),
            "test_fc_mean_shift_C": float(diff.mean()) if diff.size else float("nan"),
            "test_fc_mean_abs_shift_C": float(np.abs(diff).mean()) if diff.size else float("nan"),
        }
        parts.append(
            expanding_window_scores(
                frame,
                year,
                year,
                trend=trend,
                leads=leads_list,
                min_train_targets=min_train_targets,
            )
        )
        log.info(
            "window %d (%s): bridge %s..%s beta=%.4f",
            year,
            "trend" if trend else "notrend",
            wb.fit_start,
            wb.fit_end,
            wb.beta,
        )
    nonempty = [p for p in parts if not p.empty]
    honest_scores = (
        pd.concat(nonempty, ignore_index=True) if nonempty else parts[0].iloc[0:0].copy()
    )

    leaky_summary = summarise_scores(leaky_scores)
    honest_summary = summarise_scores(honest_scores)
    return BridgeWindowStudy(
        trend=bool(trend),
        leads=leads_list,
        first_test_year=first_test_year,
        last_test_year=last_test_year,
        min_train_targets=int(min_train_targets),
        full_bridge=full_bridge,
        window_bridges=window_bridges,
        leaky_scores=leaky_scores,
        honest_scores=honest_scores,
        leaky_summary=leaky_summary,
        honest_summary=honest_summary,
        comparison=compare_summaries(leaky_summary, honest_summary),
        leaky_verdict=evaluate_hindcast_go_live(leaky_summary, require_live_paper=False),
        honest_verdict=evaluate_hindcast_go_live(honest_summary, require_live_paper=False),
        window_shifts=window_shifts,
        runtime_s=time.perf_counter() - t0,
    )


# --------------------------------------------------------------------------- #
# inputs (read-only)
# --------------------------------------------------------------------------- #


def resolve_root(override: str | Path | None = None) -> Path:
    """Same priority as ``climate.paths.data_root`` (arg, $CLIMATE_DATA_ROOT, default),
    but never creates anything: a missing root is an error, not a new directory."""
    if override is not None and str(override).strip():
        root = Path(override).expanduser().resolve()
    elif os.environ.get(ENV_VAR, "").strip():
        root = Path(os.environ[ENV_VAR]).expanduser().resolve()
    else:
        root = Path.home() / "quant_data" / "climate_data"
    if not root.is_dir():
        raise FileNotFoundError(f"climate data root {root} does not exist")
    return root


def load_inputs(root: Path) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """SEAS5 members, ERA5 K, NOAA gtm first prints via the repo's own loaders."""
    required = [
        root / SEAS5_PARQUET[0] / SEAS5_PARQUET[1],
        root / ERA5_PARQUET[0] / ERA5_PARQUET[1],
        root / "labels" / "labels_ledger.parquet",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing climate inputs: {missing}")
    return load_seas5(root), load_era5(root), load_noaa_gtm(root)


def operational_trend(root: Path) -> tuple[bool, str]:
    """Trend flag of the operational model artifact (read-only), else the documented choice."""
    path = root.joinpath(*MODEL_REL)
    rel = "/".join(MODEL_REL)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload.get("trend"), bool):
                return payload["trend"], f"{rel} (trend={payload['trend']})"
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", path, exc)
    return (
        DOCUMENTED_OPERATIONAL_TREND,
        f"assumed trend={DOCUMENTED_OPERATIONAL_TREND} (docs/gtm_model_report.md; "
        f"{rel} absent or unreadable)",
    )


def describe_inputs(seas5: pd.DataFrame, era5_K: pd.Series, noaa_C: pd.Series) -> dict[str, Any]:
    inits = pd.to_datetime(seas5["init_date"])
    era5_idx = to_year_month_index(era5_K.index)
    noaa_idx = to_year_month_index(noaa_C.index)
    overlap = sorted(set(era5_idx) & set(noaa_idx))
    return {
        "seas5_rows": int(len(seas5)),
        "seas5_inits": f"{inits.min():%Y-%m} .. {inits.max():%Y-%m}",
        "seas5_leads": sorted(int(x) for x in pd.unique(seas5["lead_month"])),
        "era5_months": f"{min(era5_idx)} .. {max(era5_idx)} ({len(era5_idx)})",
        "noaa_gtm_months": f"{min(noaa_idx)} .. {max(noaa_idx)} ({len(noaa_idx)})",
        "era5_noaa_overlap": f"{overlap[0]} .. {overlap[-1]} ({len(overlap)})" if overlap else "",
    }


def display_path(path: Path) -> str:
    """Home-relative spelling for the committed report (no user-specific absolute path)."""
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _fmt(v: Any, digits: int = 3, signed: bool = False) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "nan"
    if not math.isfinite(f):
        return "nan"
    return f"{f:+.{digits}f}" if signed else f"{f:.{digits}f}"


def _json_clean(obj: Any) -> Any:
    """Strict-JSON payload: non-finite floats -> null, numpy scalars -> Python."""
    if isinstance(obj, dict):
        return {str(k): _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_clean(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    return obj


def _verdict_word(v: GoLiveVerdict) -> str:
    return "PASS" if v.passed else "FAIL"


def _variant_section(study: BridgeWindowStudy, *, operational: bool) -> list[str]:
    tag = " (operational)" if operational else ""
    lines = [
        f"## Variant `{study.variant}`{tag}",
        "",
        f"- Test years {study.first_test_year}–{study.last_test_year}, leads "
        f"{study.leads}, min_train_targets {study.min_train_targets}, "
        f"runtime {study.runtime_s:.1f}s.",
        f"- Honest-bridge gate: **{_verdict_word(study.honest_verdict)}**; "
        f"leaky (production) gate: **{_verdict_word(study.leaky_verdict)}**.",
    ]
    if study.honest_verdict.reasons:
        lines.append("- Honest-gate refusal reasons:")
        lines += [f"  - {r}" for r in study.honest_verdict.reasons]
    if study.leaky_verdict.reasons:
        lines.append("- Leaky-gate refusal reasons:")
        lines += [f"  - {r}" for r in study.leaky_verdict.reasons]
    lines += [
        "",
        "| lead | n leaky | n honest | CRPS EMOS leaky | CRPS EMOS honest | CRPS trend-clim | "
        "skill vs trend (leaky) | skill vs trend (honest) | delta (honest − leaky) | "
        "cover50 honest | cover90 honest |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, r in study.comparison.iterrows():
        lines.append(
            f"| {int(r['lead_month'])} | {_fmt(r['n_leaky'], 0)} | {_fmt(r['n_honest'], 0)} | "
            f"{_fmt(r['crps_emos_leaky'], 4)} | {_fmt(r['crps_emos_honest'], 4)} | "
            f"{_fmt(r['crps_trend'], 4)} | {_fmt(r['skill_vs_trend_leaky'])} | "
            f"{_fmt(r['skill_vs_trend_honest'])} | {_fmt(r['skill_delta'], signed=True)} | "
            f"{_fmt(r['cover50_honest'])} | {_fmt(r['cover90_honest'])} |"
        )
    lines.append("")
    return lines


def render_markdown(meta: dict[str, Any], studies: dict[str, BridgeWindowStudy]) -> str:
    head = studies[meta["headline_variant"]]
    full = head.full_bridge
    lines = [
        "# GTM hindcast skill with the ERA5→NOAA bridge fit inside each window (audit H4)",
        "",
        f"Generated (UTC): {meta['generated_at_utc']}  ",
        f"Command: `{meta['command']}`  ",
        f"Data root (read-only): `{meta['data_root']}`",
        "",
        "## Headline",
        "",
        f"Honest-bridge hindcast gate ({head.variant} variant): "
        f"**{_verdict_word(head.honest_verdict)}** — `evaluate_hindcast_go_live` with the "
        "default, unchanged criteria on the in-window-bridge summary.",
        "",
        f"- Operational variant: `{head.variant}` — {meta['operational_trend_source']}.",
        f"- Same variant with the production full-history bridge (leaky): "
        f"**{_verdict_word(head.leaky_verdict)}**.",
        "- Per-lead skill delta vs trend climatology (honest − leaky), "
        f"`{head.variant}`: "
        + ", ".join(
            f"L{int(r['lead_month'])} {_fmt(r['skill_delta'], signed=True)}"
            for _, r in head.comparison.iterrows()
        )
        + ".",
    ]
    for name, st in studies.items():
        if name != head.variant:
            lines.append(
                f"- Other variant `{name}`: honest gate **{_verdict_word(st.honest_verdict)}**, "
                f"leaky gate **{_verdict_word(st.leaky_verdict)}**."
            )
    lines += [
        "",
        "## Inputs",
        "",
    ]
    lines += [f"- {k}: {v}" for k, v in meta["inputs"].items()]
    lines.append("")
    for name, st in studies.items():
        lines += _variant_section(st, operational=(name == head.variant))
    lines += [
        "## Bridge drift across windows",
        "",
        f"Production full-history bridge: beta {_fmt(full.beta, 4)}, R² {_fmt(full.r2, 4)}, "
        f"resid sd {_fmt(full.resid_sd, 4)}, n {full.n_obs}, {full.fit_start}..{full.fit_end}.",
        "",
        "The leak's size is the shift of `fc_mean_C` (window bridge − full bridge, degC) on "
        "the rows scored for that test year (alpha and beta trade off against the ~287 K "
        "ERA5 level, so raw alpha differences are not informative). `fc_sd_C` scales by "
        "beta_window / beta_full.",
        "",
        "| test year | fit window | n months | beta | beta − full | R² | resid sd | "
        "scored rows | mean Δ fc_mean_C | mean abs Δ fc_mean_C |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, r in head.bridge_table().iterrows():
        lines.append(
            f"| {int(r['test_year'])} | {r['fit_start']}..{r['fit_end']} | {int(r['n_obs'])} | "
            f"{_fmt(r['beta'], 4)} | {_fmt(r['beta_minus_full'], 4, signed=True)} | "
            f"{_fmt(r['r2'], 4)} | {_fmt(r['resid_sd'], 4)} | {int(r['n_test_rows'])} | "
            f"{_fmt(r['test_fc_mean_shift_C'], 4, signed=True)} | "
            f"{_fmt(r['test_fc_mean_abs_shift_C'], 4)} |"
        )
    lines += [
        "",
        "## Method",
        "",
        "- **Leaky** (production): one bridge fit on the full ERA5/NOAA overlap, then "
        "`expanding_window_scores` over the test years — exactly what `climate.fit.fit_gtm` "
        "computes.",
        "- **Honest**: for each test year Y, `Bridge.fit(start=None, end=f\"{Y-1}-12\")`, the "
        "frame rebuilt with `build_frame_from_inputs(..., bridge=window_bridge)` (train and "
        "test rows of that window), and `expanding_window_scores(frame, Y, Y, ...)` for that "
        "single year; the per-year rows are aggregated with `summarise_scores`.",
        "- Labels, rows, test years and both climatology baselines are identical on the two "
        "sides (the baselines never see the bridge), so each delta is paired and is due to "
        "the bridge alone. `skill = 1 − CRPS_EMOS / CRPS_trend-clim`.",
        "- Timing caveat (residual lookahead, not measured here): the target-year (< Y) "
        "window convention is shared by the EMOS fit, both baselines and both sides of this "
        "comparison, so the honest − leaky DELTA isolates the bridge leak correctly. It does, "
        "however, leave residual optimism in the ABSOLUTE skill numbers on both sides: a test "
        "row at lead L whose target is Jan..(L−1) of year Y was initialised in year Y−1, so "
        "its window bridge (like its EMOS and baseline windows) uses ERA5/NOAA labels for up "
        "to L−1 months from the init month onward (up to 5 at lead 6), none of which exist "
        "at the init date; on top of that the windows ignore ERA5/NOAA publication lag. The "
        "window bridge is also held fixed for the whole test year rather than refit as "
        "months arrive. The size of these effects is not measured in this report; an "
        "init-date-bounded sensitivity study is a candidate follow-up (out of scope here — "
        "controller Ruling R15).",
        "- The gate (`climate/paper_criteria.py`) and `climate/fit.py` are untouched; this "
        "study is additive. JSON sidecar carries the per-year scores and every window's "
        "bridge parameters.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_leads(text: str) -> list[int]:
    leads = [int(x) for x in text.split(",") if x.strip()]
    if not leads:
        raise argparse.ArgumentTypeError("--leads needs at least one lead")
    return leads


def _parse_variants(text: str) -> list[str]:
    names = [x.strip() for x in text.split(",") if x.strip()]
    bad = [n for n in names if n not in VARIANTS]
    if bad or not names:
        raise argparse.ArgumentTypeError(f"--variants must be from {sorted(VARIANTS)}, got {text!r}")
    return names


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Hindcast skill with the ERA5->NOAA bridge refit inside each window (H4)"
    )
    parser.add_argument("--root", default=None, help="climate data root (read-only)")
    parser.add_argument("--out", required=True, help="markdown report path")
    parser.add_argument("--json-out", default=None, help="JSON sidecar (default: --out as .json)")
    parser.add_argument("--first-test-year", type=int, default=None)
    parser.add_argument("--last-test-year", type=int, default=None)
    parser.add_argument("--leads", type=_parse_leads, default=[1, 2, 3, 4, 5, 6])
    parser.add_argument("--min-train-targets", type=int, default=24)
    parser.add_argument(
        "--variants",
        type=_parse_variants,
        default=list(VARIANTS),
        help="comma-separated EMOS variants to score (notrend,trend)",
    )
    args = parser.parse_args(argv)
    out_md = Path(args.out)
    out_json = Path(args.json_out) if args.json_out else out_md.with_suffix(".json")

    root = resolve_root(args.root)
    op_trend, op_source = operational_trend(root)
    headline = "trend" if op_trend else "notrend"
    variants = list(args.variants)
    if headline not in variants:
        variants.append(headline)
    log.info("reading %s (read-only); operational variant %s", root, headline)
    seas5, era5_K, noaa_C = load_inputs(root)

    studies: dict[str, BridgeWindowStudy] = {}
    for name in variants:
        studies[name] = run_study(
            seas5,
            era5_K,
            noaa_C,
            first_test_year=args.first_test_year,
            last_test_year=args.last_test_year,
            trend=VARIANTS[name],
            leads=args.leads,
            min_train_targets=args.min_train_targets,
        )
        st = studies[name]
        log.info(
            "%s: honest gate %s, leaky gate %s, deltas %s",
            name,
            _verdict_word(st.honest_verdict),
            _verdict_word(st.leaky_verdict),
            [round(float(x), 4) for x in st.comparison["skill_delta"]],
        )

    head = studies[headline]
    shown_argv = argv if argv is not None else sys.argv[1:]
    meta = {
        "study": "gtm_bridge_window (audit H4)",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": "python scripts/study_bridge_window.py " + " ".join(shown_argv),
        "data_root": display_path(root),
        "inputs": describe_inputs(seas5, era5_K, noaa_C),
        "operational_trend": op_trend,
        "operational_trend_source": op_source,
        "headline_variant": headline,
        "honest_gate_passed": bool(head.honest_verdict.passed),
        "leaky_gate_passed": bool(head.leaky_verdict.passed),
    }
    payload = dict(meta)
    payload["variants"] = {name: st.to_dict() for name, st in studies.items()}

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(meta, studies), encoding="utf-8")
    out_json.write_text(
        json.dumps(_json_clean(payload), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    log.info(
        "honest-bridge gate (%s): %s -> %s, %s",
        headline,
        _verdict_word(head.honest_verdict),
        out_md,
        out_json,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
