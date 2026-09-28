"""GTM paper loop: latest recorder sweep vs fitted EMOS. No orders.

Usage:
    python scripts/run_gtm_paper.py
    python scripts/run_gtm_paper.py --model %LOCALAPPDATA%\\climate_data\\models\\gtm_emos.json
"""

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
from climate.paths import data_root, warn_if_onedrive  # noqa: E402

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
        # print (not just log.error) to match the other CLI refusals in this
        # file below: logging.basicConfig only configures the root logger
        # once per process, so a stream captured by an EARLIER test's
        # capsys can go stale across repeated main() calls in the same
        # pytest run -- stderr always gets it.
        reason = (
            "refusing: --paper-days/--resolved-months must be computed from the "
            "run/label ledgers (audit finding M2b); pass --allow-manual-gate-inputs "
            "to override with an explicit operator-typed value"
        )
        print(reason, file=sys.stderr)
        log.error(reason)
        return 1

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

    # Audit finding M2b: paper_days/resolved_months are DERIVED from the
    # run/label ledgers, not trusted from the CLI, so nobody can
    # (accidentally or otherwise) inflate paper-trading progress. A manual
    # value is only used when the operator explicitly opted in above via
    # --allow-manual-gate-inputs; per-field, so e.g. a manual --paper-days
    # can be combined with a computed --resolved-months.
    paper_days = args.paper_days
    resolved_months = args.resolved_months
    # Final-review bundle B-1: provenance. A manual value is legitimate
    # (the operator opted in above) but must never be indistinguishable,
    # after the fact, from a ledger-derived one -- so it is WARNED here and
    # recorded as go_live.gate_inputs_source in the payload (and therefore
    # in the gtm_signals_history.jsonl line) below.
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
    # The hindcast go-live gate scores the fit's expanding-window summary,
    # which fit_gtm_emos.py writes next to the model (reports/...json).
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

    # Staleness gate (H2): refuse to write a signals file priced off a
    # stale sweep or a stale SEAS5 init. This must run BEFORE
    # write_paper_payload -- a stale sweep must not even produce a
    # gtm_signals.json for run_ibkr_paper.py to (mis)trust later. No
    # signal-file-age check here: this process is the one writing the
    # file, so it is fresh by construction; run_ibkr_paper.py checks that
    # age against its OWN clock when it later reads the file.
    sweep_ts = parse_ts_utc(payload["sweep_ts_utc"]) if payload.get("sweep_ts_utc") else None
    reasons: list[str] = []
    if sweep_ts is None:
        # This entrypoint ALWAYS loads quotes from disk (run_paper is
        # never called with an injected quotes_df here), so a null
        # sweep_ts_utc cannot mean "no sweep file to be stale" -- it means
        # a real sweep file was found but its name did not match the
        # recorder's own quotes-HHMMSSZ.parquet convention. That must
        # refuse just as loudly as an actually-old sweep; check_staleness's
        # "None skips this check" path is reserved for data that is absent
        # BY DESIGN (e.g. an injected quotes_df in tests), not this case.
        try:
            bad_path = latest_sweep_path(ticks_root)
        except FileNotFoundError:
            bad_path = f"{ticks_root}/quotes/date=*/*.parquet"
        reasons.append(
            f"could not parse a sweep timestamp from {bad_path} "
            "(unrecognised filename; expected quotes-HHMMSSZ.parquet)"
        )
    # payload["inits"] covers the PRICED LADDER's target months only
    # (climate.paper.inits_for_priced_targets), and each must carry
    # min(expected latest init, target) -- final-review finding C-1; the
    # same shared check runs in scripts/run_ibkr_paper.py.
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
    # L8 (Task 3.8): AFTER the latest-file write, so a run that refused
    # above (staleness gate) never appends a history line for a signals
    # file it never produced -- write_paper_payload overwrites gtm_signals
    # .json every run, this is the run-by-run series alongside it.
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
