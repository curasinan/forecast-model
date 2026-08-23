"""Fetch Copernicus CDS climate forecast data (SEAS5) + ERA5 reference.

Modes:
  --pilot           Small end-to-end validation: ERA5 monthly means for the
                    last 2 complete years + SEAS5 (per-member monthly means)
                    for the 2 most recent available init months, 1.0 deg
                    grid, leads 1-6. Downloads run concurrently.
  --hindcast YEARS  Bulk backfill: SEAS5 for the given years ('1993-2016'
                    or '1995,1997'), chunked one-year-per-request (all 12
                    init months, leads 1-6). Resumable: completed files are
                    skipped by the idempotent cache in climate.cds.fetch.

Raw netCDF lands under <root>/raw/; reduced global-mean parquet under
<root>/forecasts/ and <root>/reference/. Root defaults to
%LOCALAPPDATA%/climate_data (override: CLIMATE_DATA_ROOT env or --root).
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.cds import (  # noqa: E402
    ERA5_MONTHLY_DATASET,
    SEAS5_DATASET,
    CdsSoftTimeout,
    build_era5_monthly_request,
    build_seas5_request,
    climate_data_root,
    fetch,
)
from climate.reduce import reduce_era5_file, reduce_seas5_file  # noqa: E402

log = logging.getLogger("fetch_climate_forecasts")

LEADS = list(range(1, 7))


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def parse_years(value: str) -> list[int]:
    """Accept '1993-2016' ranges or '1995,1997' lists."""
    value = value.strip()
    if "-" in value and "," not in value:
        lo, hi = value.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(x) for x in value.split(",") if x.strip()]


def recent_init_months(today: date, count: int = 2) -> list[tuple[int, int]]:
    """The ``count`` most recent SEAS5 init months, newest first.

    C3S publishes each month's seasonal forecast around the 13th; before
    the 15th we conservatively assume the current month is not yet up.
    """
    year, month = today.year, today.month
    if today.day < 15:
        month -= 1
    out = []
    for _ in range(count):
        if month < 1:
            month += 12
            year -= 1
        out.append((year, month))
        month -= 1
    return out


def _seas5_task(root: Path, year: int, months: list[int], tag: str) -> tuple[str, dict, Path]:
    request = build_seas5_request(year, months, LEADS)
    out = root / "raw" / f"seas5_{tag}_t2m_monthly_mean.nc"
    return SEAS5_DATASET, request, out


def _era5_task(root: Path, year: int) -> tuple[str, dict, Path]:
    request = build_era5_monthly_request([year])
    out = root / "raw" / f"era5_{year}_t2m_monthly.nc"
    return ERA5_MONTHLY_DATASET, request, out


def run_tasks(
    tasks: list[tuple[str, dict, Path]],
    soft_timeout: float | None,
    jobs: int,
) -> tuple[list[Path], list[tuple[Path, BaseException]]]:
    """Run fetches concurrently. Returns (completed_paths, failures)."""
    completed: list[Path] = []
    failures: list[tuple[Path, BaseException]] = []

    def _one(task: tuple[str, dict, Path]) -> None:
        dataset, request, out_path = task
        try:
            completed.append(fetch(dataset, request, out_path, soft_timeout=soft_timeout))
        except CdsSoftTimeout as exc:
            log.warning("soft timeout: %s", exc)
            failures.append((out_path, exc))
        except BaseException as exc:  # noqa: BLE001 -- reported at end, exit code 1
            log.error("fetch failed for %s: %r", out_path.name, exc)
            failures.append((out_path, exc))

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(_one, tasks))
    return completed, failures


def reduce_completed(completed: list[Path], root: Path) -> None:
    for path in completed:
        name = path.name
        if name.startswith("seas5_"):
            added, total = reduce_seas5_file(path, root)
            target = root / "forecasts" / "seas5_global_mean.parquet"
        elif name.startswith("era5_"):
            added, total = reduce_era5_file(path, root)
            target = root / "reference" / "era5_global_mean.parquet"
        else:
            log.warning("unrecognised raw file, skipping reduce: %s", path)
            continue
        print(f"reduced {name} ({path.stat().st_size} bytes) -> {target} (+{added} rows, {total} total)")


def report_failures(failures: list[tuple[Path, BaseException]]) -> None:
    for out_path, exc in failures:
        msg = str(exc)
        if "licence" in msg.lower() or "license" in msg.lower() or "terms" in msg.lower():
            print(f"USER ACTION required for {out_path.name}: accept the dataset licence(s) ")
            print("on the CDS website (https://cds.climate.copernicus.eu, dataset download ")
            print(f"page, 'Terms of use'). CDS error verbatim: {exc}")
        elif isinstance(exc, CdsSoftTimeout):
            print(f"submitted/queued, not complete: {out_path.name} -- {exc}")
        else:
            print(f"FAILED: {out_path.name} -- {exc!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pilot", action="store_true", help="small end-to-end validation run")
    mode.add_argument("--hindcast", metavar="YEARS", help="bulk SEAS5 backfill, e.g. 1993-2016")
    mode.add_argument("--era5", metavar="YEARS",
                      help="ERA5 monthly reference backfill for the same years as the hindcast")
    parser.add_argument("--root", default=None, help="data root override (else CLIMATE_DATA_ROOT)")
    parser.add_argument(
        "--soft-timeout",
        type=float,
        default=None,
        help="per-request soft timeout in seconds (default: wait forever)",
    )
    parser.add_argument("--jobs", type=int, default=4, help="concurrent CDS requests (default 4)")
    args = parser.parse_args(argv)

    setup_logging()
    root = climate_data_root(args.root)
    print(f"climate data root: {root}")

    tasks: list[tuple[str, dict, Path]] = []
    if args.pilot:
        today = date.today()
        era_years = [today.year - 2, today.year - 1]
        inits = recent_init_months(today)
        print(f"pilot: ERA5 years {era_years}, SEAS5 init months "
              f"{[f'{y}-{m:02d}' for y, m in inits]}, leads {LEADS}")
        tasks += [_era5_task(root, y) for y in era_years]
        tasks += [_seas5_task(root, y, [m], f"{y}{m:02d}") for y, m in inits]
    elif args.era5:
        years = parse_years(args.era5)
        print(f"era5: monthly reference years {years[0]}..{years[-1]} ({len(years)} requests)")
        tasks += [_era5_task(root, y) for y in years]
    else:
        years = parse_years(args.hindcast)
        print(f"hindcast: SEAS5 years {years[0]}..{years[-1]} ({len(years)} one-year requests)")
        tasks += [_seas5_task(root, y, list(range(1, 13)), f"hindcast_{y}") for y in years]

    completed, failures = run_tasks(tasks, args.soft_timeout, args.jobs)
    reduce_completed(completed, root)
    report_failures(failures)
    print(f"done: {len(completed)}/{len(tasks)} files ready under {root / 'raw'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
