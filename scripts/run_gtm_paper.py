"""GTM paper loop: latest recorder sweep vs fitted EMOS. No orders."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.dataset import build_training_frame  # noqa: E402
from climate.emos import EmosModel  # noqa: E402
from climate.fit import MODEL_REL  # noqa: E402
from climate.ladder import latest_sweep_path  # noqa: E402
from climate.paper import append_paper_history, parse_ts_utc, run_paper, write_paper_payload  # noqa: E402
from climate.paper_criteria import check_staleness  # noqa: E402
from climate.paper_progress import compute_paper_progress  # noqa: E402
from climate.paths import data_root, root_path, warn_if_onedrive  # noqa: E402
from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402

log = logging.getLogger("run_gtm_paper")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _default_ticks_root() -> Path:
    """IBKR_DATA_ROOT, else the IBKRSettings default (~/quant_data/ibkr_ticks)."""
    override = os.environ.get("IBKR_DATA_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "quant_data" / "ibkr_ticks"


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="GTM paper signals (research only)")
    parser.add_argument("--root", default=None, help="climate data root")
    parser.add_argument("--model", default=None, help="path to gtm_emos.json")
    parser.add_argument("--ticks-root", default=None, help="IBKR recorder data root")
    parser.add_argument("--universe-json", default=None, help="Stage-0 universe snapshot")
    parser.add_argument(
        "--paper-days", type=int, default=None,
        help="manual override; requires --allow-manual-gate-inputs (default: computed "
             "from ibkr_paper_runs.jsonl -- audit finding M2b)",
    )
    parser.add_argument(
        "--resolved-months", type=int, default=None,
        help="manual override; requires --allow-manual-gate-inputs (default: computed "
             "from the run/label ledgers -- audit finding M2b)",
    )
    parser.add_argument(
        "--allow-manual-gate-inputs",
        action="store_true",
        help="permit --paper-days/--resolved-months to override the computed values "
             "(audit finding M2b: without this flag, a manually-typed value is refused "
             "so nobody can accidentally inflate paper-trading progress)",
    )
    parser.add_argument(
        "--hindcast-gate-only",
        action="store_true",
        help="report the hindcast-skill gate alone (default: the live paper gate of "
             "365 days + 12 resolved months is REQUIRED, so go_live stays False until "
             "a real paper record exists)",
    )
    args = parser.parse_args(argv)

    manual_paper_days = args.paper_days is not None
    manual_resolved_months = args.resolved_months is not None
    if (manual_paper_days or manual_resolved_months) and not args.allow_manual_gate_inputs:
        reason = (
            "refusing: --paper-days/--resolved-months must be computed from the "
            "run/label ledgers (audit finding M2b); pass --allow-manual-gate-inputs "
            "to override with an explicit operator-typed value"
        )
        print(reason, file=sys.stderr)
        log.error(reason)
        return 1

    try:
        refuse_if_mirror(root_path(args.root), "write GTM paper signals")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2

    root = data_root(args.root)
    warn = warn_if_onedrive(root)
    if warn:
        log.warning(warn)
    model_path = Path(args.model) if args.model else root.joinpath(*MODEL_REL)
    if not model_path.exists():
        log.error("fitted model not found at %s — run scripts/fit_gtm_emos.py first", model_path)
        return 1
    ticks_root = Path(args.ticks_root) if args.ticks_root else _default_ticks_root()
    universe = args.universe_json or str(REPO_ROOT / "data" / "ibkr_universe_live.json")

    paper_days = args.paper_days
    resolved_months = args.resolved_months
    manual_fields = [
        f"{name}={value}"
        for name, value in (("paper_days", paper_days), ("resolved_months", resolved_months))
        if value is not None
    ]
    gate_inputs_source = (
        "manual:" + ",".join(field.split("=", 1)[0] for field in manual_fields)
        if manual_fields else "ledgers"
    )
    if manual_fields:
        log.warning(
            "go-live gate inputs set MANUALLY (--allow-manual-gate-inputs): %s -- NOT "
            "derived from the run/label ledgers; recorded as gate_inputs_source=%s",
            ", ".join(manual_fields), gate_inputs_source,
        )
    if paper_days is None or resolved_months is None:
        progress = compute_paper_progress(
            runs_jsonl=root / "paper" / "ibkr_paper_runs.jsonl",
            labels_root=root,
            now=datetime.now(timezone.utc),
        )
        if paper_days is None:
            paper_days = progress.paper_days
        if resolved_months is None:
            resolved_months = progress.resolved_months
        log.info(
            "gate inputs from ledgers: paper_days=%d resolved_months=%d first_order_ts=%s",
            paper_days, resolved_months, progress.first_order_ts,
        )

    log.info("loading frame from %s, model from %s", root, model_path)
    frame = build_training_frame(root)
    model = EmosModel.from_json(model_path)
    summary = None
    diag_path = root / "reports" / "gtm_emos_diagnostics.json"
    if diag_path.exists():
        diag = json.loads(diag_path.read_text(encoding="utf-8"))
        if diag.get("summary"):
            summary = pd.DataFrame(diag["summary"])
    else:
        log.warning("no diagnostics at %s; hindcast go-live gate cannot be scored", diag_path)
    payload = run_paper(
        frame,
        model,
        summary=summary,
        ticks_root=ticks_root,
        universe_json=universe if Path(universe).exists() else None,
        paper_days=paper_days,
        resolved_months=resolved_months,
        require_live_paper=not args.hindcast_gate_only,
    )
    payload["go_live"]["gate_inputs_source"] = gate_inputs_source  # B-1 provenance

    sweep_ts = parse_ts_utc(payload["sweep_ts_utc"]) if payload.get("sweep_ts_utc") else None
    reasons: list[str] = []
    if sweep_ts is None:
        try:
            bad_path = latest_sweep_path(ticks_root)
        except FileNotFoundError:
            bad_path = f"{ticks_root}/quotes/date=*/*.parquet"
        reasons.append(
            f"could not parse a sweep timestamp from {bad_path} "
            "(unrecognised filename; expected quotes-HHMMSSZ.parquet)"
        )
    reasons += check_staleness(
        sweep_ts=sweep_ts,
        signal_ts=None,
        inits_by_target=payload.get("inits"),
        now=datetime.now(timezone.utc),
    )
    if reasons:
        for reason in reasons:
            print(reason, file=sys.stderr)
        log.error("refusing to write gtm_signals.json: %d staleness reason(s)", len(reasons))
        return 1

    out = write_paper_payload(payload, root=root)
    append_paper_history(payload, root=root)
    log.info(
        "ladder=%s tradeable=%s go_live=%s -> %s",
        payload["n_ladder"],
        payload["n_tradeable"],
        payload["go_live"]["passed"],
        out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
