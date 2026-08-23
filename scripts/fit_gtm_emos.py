"""Fit CLI: GTM EMOS + expanding-window diagnostics.

Usage:
    python scripts/fit_gtm_emos.py
    python scripts/fit_gtm_emos.py --trend
    python scripts/fit_gtm_emos.py --root %LOCALAPPDATA%\\climate_data
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.fit import fit_gtm, write_artifacts  # noqa: E402
from climate.paper_criteria import evaluate_hindcast_go_live  # noqa: E402
from climate.paths import data_root, warn_if_onedrive  # noqa: E402

log = logging.getLogger("fit_gtm_emos")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Fit GTM EMOS and write diagnostics")
    parser.add_argument("--root", default=None, help="climate data root (default: climate.paths.data_root)")
    parser.add_argument("--trend", action="store_true", help="include a linear trend in the EMOS mean")
    parser.add_argument("--first-test-year", type=int, default=None)
    parser.add_argument("--last-test-year", type=int, default=None)
    args = parser.parse_args(argv)

    root = data_root(args.root)
    warn = warn_if_onedrive(root)
    if warn:
        log.warning(warn)
    log.info("Fitting GTM EMOS from %s (trend=%s)", root, args.trend)
    result = fit_gtm(
        root=root,
        trend=args.trend,
        first_test_year=args.first_test_year,
        last_test_year=args.last_test_year,
    )
    paths = write_artifacts(result, root=root)
    verdict = evaluate_hindcast_go_live(result.summary, require_live_paper=False)
    log.info("bridge %s", result.bridge)
    log.info("leads fitted: %s", sorted(result.model.params))
    log.info("hindcast go-live: passed=%s reasons=%s", verdict.passed, verdict.reasons)
    for key, path in paths.items():
        log.info("wrote %s -> %s", key, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
