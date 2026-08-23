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
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.dataset import build_training_frame  # noqa: E402
from climate.emos import EmosModel  # noqa: E402
from climate.fit import MODEL_REL  # noqa: E402
from climate.paper import run_paper, write_paper_payload  # noqa: E402
from climate.paths import data_root, warn_if_onedrive  # noqa: E402

log = logging.getLogger("run_gtm_paper")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _default_ticks_root() -> Path:
    local_app = os.environ.get("LOCALAPPDATA")
    if os.environ.get("IBKR_DATA_ROOT", "").strip():
        return Path(os.environ["IBKR_DATA_ROOT"]).expanduser()
    if local_app:
        return Path(local_app) / "ibkr_ticks"
    return Path.home() / ".local" / "share" / "ibkr_ticks"


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="GTM paper signals (research only)")
    parser.add_argument("--root", default=None, help="climate data root")
    parser.add_argument("--model", default=None, help="path to gtm_emos.json")
    parser.add_argument("--ticks-root", default=None, help="IBKR recorder data root")
    parser.add_argument("--universe-json", default=None, help="Stage-0 universe snapshot")
    parser.add_argument("--paper-days", type=int, default=0)
    parser.add_argument("--resolved-months", type=int, default=0)
    parser.add_argument(
        "--hindcast-gate-only",
        action="store_true",
        help="report the hindcast-skill gate alone (default: the live paper gate of "
             "365 days + 12 resolved months is REQUIRED, so go_live stays False until "
             "a real paper record exists)",
    )
    args = parser.parse_args(argv)

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
        paper_days=args.paper_days,
        resolved_months=args.resolved_months,
        require_live_paper=not args.hindcast_gate_only,
    )
    out = write_paper_payload(payload, root=root)
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
