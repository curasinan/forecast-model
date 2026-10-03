"""Fit CLI: GTM EMOS + expanding-window diagnostics.

A fit overwrites models/gtm_emos.json and reports/gtm_emos_diagnostics.{md,json} under the
root it runs on, so that root is always named on the command line:

    --root <path>   fit that root: a snapshot or a mirror, for a comparison refit
    --live          fit the default root of climate.paths.data_root (CLIMATE_DATA_ROOT, else
                    ~/quant_data/climate_data): the operational refit, which replaces the
                    model the paper runtime reads

With neither, or with an empty --root (which data_root would read as the default root),
nothing is fitted: the script prints the files it would have overwritten and exits 2.

Usage:
    python scripts/fit_gtm_emos.py --trend --live
    python scripts/fit_gtm_emos.py --trend --root ~/quant_data_snapshot/climate_data
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.fit import (  # noqa: E402
    MODEL_REL,
    REPORT_JSON_REL,
    REPORT_MD_REL,
    fit_gtm,
    write_artifacts,
)
from climate.paper_criteria import evaluate_hindcast_go_live  # noqa: E402
from climate.paths import ENV_VAR, data_root, warn_if_onedrive  # noqa: E402

log = logging.getLogger("fit_gtm_emos")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _default_root() -> Path:
    """The root ``data_root()`` falls back to with no override, without creating it.

    ``data_root`` makes the directories it returns and a refusal must leave nothing behind,
    so its two fallbacks are repeated here; tests/test_climate/test_fit_cli.py holds the
    two together.
    """
    if os.environ.get(ENV_VAR, "").strip():
        return Path(os.environ[ENV_VAR]).expanduser().resolve()
    return Path.home() / "quant_data" / "climate_data"


def _refusal(root: Path) -> str:
    """What a fit under the default ``root`` would have written, and how to name a root."""
    lines = [
        "refusing: no root was named (--root is missing or empty, and there is no --live).",
        "The fit would have run on the default climate data root, where the paper runtime",
        "reads its operational model:",
        f"  {root}",
        "and written these files, overwriting the ones that exist:",
    ]
    for rel in (MODEL_REL, REPORT_MD_REL, REPORT_JSON_REL):
        path = root.joinpath(*rel)
        lines.append(f"  {path} ({'exists' if path.exists() else 'not there yet'})")
    lines.append("Pass --root <path> to fit a snapshot or a mirror, or --live to refit this root.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Fit GTM EMOS and write the model and diagnostics under the root named by "
        "--root or --live; with neither, name the files at stake and exit 2"
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument(
        "--root",
        default=None,
        help="climate data root to fit and write under: a snapshot or a mirror",
    )
    where.add_argument(
        "--live",
        action="store_true",
        help="fit the default root (CLIMATE_DATA_ROOT, else ~/quant_data/climate_data), "
        "replacing the operational model the paper runtime reads",
    )
    parser.add_argument("--trend", action="store_true", help="include a linear trend in the EMOS mean")
    parser.add_argument("--first-test-year", type=int, default=None)
    parser.add_argument("--last-test-year", type=int, default=None)
    args = parser.parse_args(argv)

    # No root named, no fit. A blank --root (an unset shell variable) counts as unnamed:
    # data_root() reads it as no override and would resolve the default root.
    if not (args.root or "").strip() and not args.live:
        print(_refusal(_default_root()), file=sys.stderr)
        return 2

    root = data_root(args.root)  # --live leaves --root unset: the default root
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
