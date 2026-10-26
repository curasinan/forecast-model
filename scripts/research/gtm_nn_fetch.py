"""Fetch ERA5 6-hourly 2 m temperature by year and reduce it to the daily global-mean parquet."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.nn.daily import DAILY_PARQUET, daily_task, reduce_era5_daily_file  # noqa: E402
from climate.paths import root_path  # noqa: E402
from scripts.fetch_climate_forecasts import (  # noqa: E402
    parse_years,
    report_failures,
    run_tasks,
    setup_logging,
)
from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402

DEFAULT_YEARS = "1993-2025"
DEFAULT_JOBS = 2
DEFAULT_SOFT_TIMEOUT = 7200.0


def _years(text: str) -> list[int]:
    try:
        years = sorted(set(parse_years(text)))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"years like 1993-2025 or 1995,1997, got {text!r}"
        ) from None
    if not years:
        raise argparse.ArgumentTypeError(f"no year in {text!r}")
    return years


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch ERA5 6-hourly 2 m temperature from the CDS, one request per year, "
        "and reduce every file to the daily global-mean parquet under the climate data root."
    )
    parser.add_argument(
        "--root", default=None, help="climate data root (default: CLIMATE_DATA_ROOT)"
    )
    parser.add_argument(
        "--years",
        type=_years,
        default=DEFAULT_YEARS,
        help="1993-2025 or 1995,1997; complete years only, as the raw file of a year is "
        "cached by name (default: %(default)s)",
    )
    parser.add_argument(
        "--jobs", type=int, default=DEFAULT_JOBS, help="concurrent requests (default: %(default)s)"
    )
    parser.add_argument(
        "--soft-timeout",
        type=float,
        default=DEFAULT_SOFT_TIMEOUT,
        help="seconds to wait for one request, which stays queued at the CDS; a rerun "
        "resumes from the cache (default: %(default)s)",
    )
    return parser


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if not args.soft_timeout > 0:
        parser.error("--soft-timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = _args(argv)
    except SystemExit as exc:  # argparse exits with 2 on a usage error and 0 after --help
        return int(exc.code or 0)
    setup_logging()
    root = root_path(args.root)
    try:
        refuse_if_mirror(root, "download daily ERA5")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2

    tasks = [daily_task(root, year) for year in args.years]
    year_of = {task[2]: year for task, year in zip(tasks, args.years, strict=True)}
    cached = {path for _, _, path in tasks if path.exists() and path.stat().st_size > 0}
    print(f"climate data root: {root}")
    print(
        f"daily ERA5: {len(tasks)} years {args.years[0]}..{args.years[-1]}, "
        f"{len(cached)} raw files already present"
    )
    completed, failures = run_tasks(tasks, args.soft_timeout, args.jobs)

    reduced, total, unreadable = 0, None, 0
    for path in sorted(completed, key=year_of.__getitem__):
        year = year_of[path]
        try:
            added, total = reduce_era5_daily_file(path, root)
        except Exception as exc:  # one bad file must not hide the other years
            print(f"{year}: FAILED to reduce {path.name}: {exc!r}; delete it to fetch it again")
            unreadable += 1
            continue
        reduced += 1
        how = "cached" if path in cached else "downloaded"
        size = path.stat().st_size
        print(f"{year}: {path.name} {how} ({size} bytes), +{added} rows, {total} total")
    report_failures(failures)
    rows = "" if total is None else f", {total} daily rows"
    print(f"done: {reduced}/{len(tasks)} years reduced into {root.joinpath(*DAILY_PARQUET)}{rows}")
    return 1 if failures or unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main())
