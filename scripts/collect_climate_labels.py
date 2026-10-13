"""Collect NOAA climate oracle labels into the first-print ledger."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate import noaa  # noqa: E402
from climate.noaa import LabelObservation  # noqa: E402
from climate.paths import data_root, root_path, warn_if_onedrive  # noqa: E402
from climate.storage import append_observations, ledger_path  # noqa: E402
from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402

log = logging.getLogger("collect_climate_labels")

# Monkeypatch point for tests; order is the fetch/report order.
FETCHERS: dict[str, Callable[[], list[LabelObservation]]] = {
    "gtm": noaa.fetch_gtm,
    "gt": noaa.fetch_gt,
    "acd_annual": noaa.fetch_acd_annual,
    "acd_monthly": noaa.fetch_acd_monthly,
}


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Collect NOAA climate labels (first-print ledger)")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Record fetched periods as backfill_current_vintage (first run)",
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="Override climate data root (else CLIMATE_DATA_ROOT / %%LOCALAPPDATA%%/climate_data)",
    )
    args = parser.parse_args(argv)

    try:
        refuse_if_mirror(root_path(args.data_root), "append to the label ledger")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2

    root = data_root(args.data_root)
    warning = warn_if_onedrive(root)
    if warning:
        log.warning(warning)

    observations: list[LabelObservation] = []
    failed: list[str] = []
    for series, fetcher in FETCHERS.items():
        try:
            rows = fetcher()
        except Exception as exc:  # noqa: BLE001 - any fetch/parse failure is fatal
            log.error("FETCH FAILED for series %s: %r", series, exc)
            failed.append(series)
            continue
        log.info("Fetched %d rows for %s (%s .. %s)", len(rows), series, rows[0].period,
                 rows[-1].period)
        observations.extend(rows)

    summary = append_observations(observations, backfill=args.backfill, root=root)

    total_new = 0
    for series in FETCHERS:
        counts = summary.get(series, {"new": 0, "revisions": 0, "skipped": 0})
        total_new += counts["new"] + counts["revisions"]
        log.info(
            "%-12s new=%d revisions=%d skipped=%d",
            series, counts["new"], counts["revisions"], counts["skipped"],
        )
    mode = "backfill" if args.backfill else "incremental"
    log.info("Mode=%s ledger=%s total appended rows=%d", mode, ledger_path(root), total_new)

    if failed:
        log.error("Fetch failed for: %s -- exiting 1", ", ".join(failed))
        return 1
    if total_new == 0:
        log.info("Nothing new (all periods already recorded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
