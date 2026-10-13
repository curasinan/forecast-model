"""Thin, cache-aware wrappers over cdsapi for Copernicus CDS retrievals."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger("climate.cds")

SEAS5_DATASET = "seasonal-monthly-single-levels"
ERA5_MONTHLY_DATASET = "reanalysis-era5-single-levels-monthly-means"
SEAS5_SYSTEM = "51"
DEFAULT_GRID = "1.0/1.0"


class CdsSoftTimeout(TimeoutError):
    """Soft timeout: the CDS request is still queued/running server-side."""


def climate_data_root(override: str | Path | None = None) -> Path:
    """Return the local (non-OneDrive) climate data directory."""
    from climate.paths import data_root

    root = data_root(override)

    for sub in ("raw", "forecasts", "reference"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _two_digit(values: Iterable[int | str]) -> list[str]:
    return [f"{int(v):02d}" for v in values]


def build_seas5_request(
    year: int | str,
    months: Iterable[int | str],
    leadtime_months: Iterable[int | str],
    grid: str = DEFAULT_GRID,
) -> dict:
    """Request dict for SEAS5 per-member monthly-mean 2m temperature."""
    return {
        "originating_centre": "ecmwf",
        "system": SEAS5_SYSTEM,
        "variable": "2m_temperature",
        "product_type": "monthly_mean",
        "year": str(int(year)),
        "month": _two_digit(months),
        "leadtime_month": [str(int(m)) for m in leadtime_months],
        "data_format": "netcdf",
        "grid": grid,
    }


def build_era5_monthly_request(
    years: Iterable[int | str],
    grid: str = DEFAULT_GRID,
) -> dict:
    """Request dict for ERA5 monthly-averaged-reanalysis 2m temperature."""
    return {
        "product_type": "monthly_averaged_reanalysis",
        "variable": "2m_temperature",
        "year": [str(int(y)) for y in years],
        "month": _two_digit(range(1, 13)),
        "time": "00:00",
        "data_format": "netcdf",
        "grid": grid,
    }


def _make_client():
    """Lazy cdsapi client factory (kept separate so tests can monkeypatch)."""
    import cdsapi  # imported lazily: tests never need it

    return cdsapi.Client(quiet=False, progress=False)


def fetch(
    dataset: str,
    request: dict,
    out_path: str | Path,
    soft_timeout: float | None = None,
) -> Path:
    """Download ``dataset``/``request`` to ``out_path`` (idempotent)."""
    out_path = Path(out_path)
    if out_path.exists() and out_path.stat().st_size > 0:
        log.info("cache hit, skipping fetch: %s (%d bytes)", out_path, out_path.stat().st_size)
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = out_path.with_suffix(out_path.suffix + ".part")
    log.info("CDS request: dataset=%s request=%s -> %s", dataset, json.dumps(request), out_path)

    started = time.monotonic()
    outcome: dict = {}

    def _worker() -> None:
        try:
            client = _make_client()
            client.retrieve(dataset, request, str(part_path))
            outcome["ok"] = True
        except BaseException as exc:  # noqa: BLE001 -- surfaced to caller below
            outcome["exc"] = exc

    thread = threading.Thread(target=_worker, name=f"cds-fetch-{out_path.name}", daemon=True)
    thread.start()
    thread.join(soft_timeout)

    elapsed = time.monotonic() - started
    if thread.is_alive():
        raise CdsSoftTimeout(
            f"CDS request for {dataset} -> {out_path.name} still queued/running "
            f"after {elapsed:.0f}s (soft_timeout={soft_timeout}s). Not cancelled "
            "server-side; rerun later to resume via the idempotent cache."
        )
    if "exc" in outcome:
        exc = outcome["exc"]
        log.error(
            "CDS request FAILED after %.0fs: dataset=%s error=%r", elapsed, dataset, exc
        )
        raise exc

    os.replace(part_path, out_path)
    log.info(
        "CDS request complete in %.0fs: %s (%d bytes)",
        elapsed,
        out_path,
        out_path.stat().st_size,
    )
    return out_path
