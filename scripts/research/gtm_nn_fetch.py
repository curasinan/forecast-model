"""Fetch ERA5 6-hourly 2 m temperature by year or month into the daily global-mean parquet."""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.cds import CdsSoftTimeout  # noqa: E402
from climate.nn.daily import (  # noqa: E402
    DAILY_PARQUET,
    daily_month_task,
    daily_task,
    has_era5_day,
    parse_months,
    reduce_era5_daily_file,
)
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
DAY_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
REFRESH_HINT = "rerun with --refresh"
ZIP_MAGIC = b"PK\x03\x04"


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


def _months(text: str) -> list[str]:
    try:
        return parse_months(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"months like 2026-01,2026-03 or 2026-01..2026-10, got {text!r}"
        ) from None


def _day(text: str) -> date:
    if DAY_TEXT.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"a date like 2026-10-27, got {text!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch ERA5 6-hourly 2 m temperature from the CDS, one request per year or "
        "per month, and reduce every file to the daily global-mean parquet under the climate "
        "data root."
    )
    parser.add_argument(
        "--root", default=None, help="climate data root (default: CLIMATE_DATA_ROOT)"
    )
    parser.add_argument(
        "--years",
        type=_years,
        default=None,
        help="1993-2025 or 1995,1997; complete years only, as the raw file of a year is "
        f"cached by name (default: {DEFAULT_YEARS}, or no year when --months is given)",
    )
    parser.add_argument(
        "--months",
        type=_months,
        default=None,
        help="2026-01,2026-03 or 2026-01..2026-10: one request per month for the days ERA5 has "
        "published by --today, fetched after the years; a month with none yet is skipped and "
        "said so",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="delete the raw files of the --months, fetch them again and replace their stored "
        "days; the years stay as they are. Rerun a failed month with --refresh too: a plain "
        "rerun keeps the older stored days",
    )
    parser.add_argument(
        "--today",
        type=_day,
        default=None,
        help="YYYY-MM-DD that the available days of a month are counted back from "
        "(default: today's UTC date)",
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
    args.months = args.months or []
    if args.refresh and not args.months:
        parser.error("--refresh needs --months")
    if args.years is None:
        args.years = [] if args.months else _years(DEFAULT_YEARS)
    return args


def _bad_delivery(path: Path) -> str | None:
    """What is wrong with a delivery of a kind that is known to go wrong, or None."""
    if path.stat().st_size == 0:
        return "empty download"
    with path.open("rb") as handle:
        if handle.read(len(ZIP_MAGIC)) == ZIP_MAGIC:
            return "the CDS returned a zip, not a netCDF"
    return None


def _remedy(path: Path, month: str | None) -> str:
    """What fetches a failed item again: a raw file stays cached, so a plain rerun would not."""
    return f"{REFRESH_HINT} --months {month}" if month else f"delete {path} and rerun"


def _report_failures(
    failures: list[tuple[Path, BaseException]], refreshing: dict[Path, str]
) -> None:
    """report_failures, each failure of a refreshed month ending in its one remedy instead."""
    for failure in failures:
        path, exc = failure
        if path not in refreshing:
            report_failures([failure])
            continue
        remedy = _remedy(path, refreshing[path])
        if isinstance(exc, CdsSoftTimeout):
            print(f"submitted/queued, not complete: {path.name} -- {remedy}")
            continue
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            report_failures([failure])
        *first, last = shown.getvalue().splitlines()
        print(*first, f"{last} -- {remedy}", sep="\n")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _args(argv)
    except SystemExit as exc:  # argparse exits with 2 on a usage error and 0 after --help
        return int(exc.code or 0)
    setup_logging()
    today = args.today if args.today is not None else datetime.now(timezone.utc).date()
    root = root_path(args.root)
    try:
        refuse_if_mirror(root, "download daily ERA5")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        available = [month for month in args.months if has_era5_day(month, today=today)]
    except OverflowError:
        print(
            f"--today {today} is too early to count the ERA5 lag back from: give a later date",
            file=sys.stderr,
        )
        return 2
    skipped = [month for month in args.months if month not in available]
    month_tasks = [daily_month_task(root, month, today=today) for month in available]
    tasks = [daily_task(root, year) for year in args.years] + month_tasks
    labels = [str(year) for year in args.years] + available
    position = {path: index for index, (_, _, path) in enumerate(tasks)}
    months = {path: month for (_, _, path), month in zip(month_tasks, available, strict=True)}
    requested = {path: len(request["day"]) for _, request, path in month_tasks}
    refreshed = set()
    if args.refresh:
        for _, _, path in month_tasks:
            if path.exists():
                path.unlink()
                refreshed.add(path)
    cached = {path for _, _, path in tasks if path.exists() and path.stat().st_size > 0}

    scope = []
    if args.years:
        scope.append(f"{len(args.years)} years {args.years[0]}..{args.years[-1]}")
    if args.months:
        scope.append(f"{len(args.months)} months {args.months[0]}..{args.months[-1]}")
    deleted = f", {len(refreshed)} deleted for refresh" if args.refresh else ""
    print(f"climate data root: {root}")
    print(f"daily ERA5: {', '.join(scope)}, {len(cached)} raw files already present{deleted}")
    for month in skipped:
        print(f"{month}: no ERA5 day available by {today}, skipped")
    completed, failures = run_tasks(tasks, args.soft_timeout, args.jobs)

    replace = {path for _, _, path in month_tasks} if args.refresh else set()
    reduced, total, unreadable = 0, None, 0
    for path in sorted(completed, key=position.__getitem__):
        label = labels[position[path]]
        remedy = _remedy(path, months.get(path))
        bad = _bad_delivery(path)
        if bad is not None:
            print(f"{label}: FAILED to reduce {path.name}: {bad}; {remedy}")
            unreadable += 1
            continue
        try:
            added, total = reduce_era5_daily_file(path, root, replace=path in replace)
        except (ValueError, KeyError, OSError) as exc:  # one bad file must not hide the others
            print(f"{label}: FAILED to reduce {path.name}: {exc!r}; {remedy}")
            unreadable += 1
            continue
        reduced += 1
        how = "refreshed" if path in refreshed else "cached" if path in cached else "downloaded"
        size = path.stat().st_size
        line = f"{label}: {path.name} {how} ({size} bytes), +{added} rows, {total} total"
        if path in replace and added < requested[path]:
            line += f", {added} of {requested[path]} requested days delivered"
        print(line)
    _report_failures(failures, {path: months[path] for path in replace})
    unit = (
        "years and months" if args.years and args.months else "months" if args.months else "years"
    )
    rows = "" if total is None else f", {total} daily rows"
    note = f", {len(skipped)} skipped" if skipped else ""
    parquet = root.joinpath(*DAILY_PARQUET)
    print(f"done: {reduced}/{len(tasks)} {unit} reduced into {parquet}{rows}{note}")
    return 1 if failures or unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main())
