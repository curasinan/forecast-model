"""Thin, cache-aware wrappers over cdsapi for Copernicus CDS retrievals.

Datasets covered:
  * ``seasonal-monthly-single-levels`` -- SEAS5 seasonal forecast monthly
    statistics (per-ensemble-member monthly means: the EMOS input).
  * ``reanalysis-era5-single-levels-monthly-means`` -- ERA5 monthly-mean
    reference series.

SEAS5 ``system`` choice (determined EMPIRICALLY, not guessed)
-------------------------------------------------------------
Probed the public CDS catalogue constraints for
``seasonal-monthly-single-levels`` on 2026-08-18 (endpoint
``/api/catalogue/v1/collections/seasonal-monthly-single-levels`` ->
constraints JSON on the CDS object store). For ``originating_centre:
ecmwf`` the available systems and their most recent init months are:

  * system ``4``  -> ends 2017-10 (SEAS4, retired)
  * system ``5``  -> ends 2022-10 (original SEAS5 stream)
  * system ``51`` -> 1981-01 .. 2026-08 (current month), all 12 init
    months per year, leadtime_month 1-6, includes ``2m_temperature`` +
    ``monthly_mean``.

System ``51`` (SEAS5.1, operational since Nov 2022, with the full
re-published 1981+ hindcast/archive) is therefore the only system with
current init dates and it also covers the whole hindcast range, so both
live pilots and hindcast backfills use ``SEAS5_SYSTEM = "51"``.

Credentials: ``cdsapi.Client()`` reads ``~/.cdsapirc`` itself. This
module never reads, prints, or logs that file.

Data root: kept OUTSIDE the OneDrive-synced repo (cloud sync corrupts
actively-written SQLite/parquet files -- see ``weather/paths.py`` for the
original rationale). Default ``%LOCALAPPDATA%/climate_data``, override
via the ``CLIMATE_DATA_ROOT`` environment variable.
"""

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
SEAS5_SYSTEM = "51"  # see module docstring: empirical, from CDS catalogue constraints
DEFAULT_GRID = "1.0/1.0"


class CdsSoftTimeout(TimeoutError):
    """Soft timeout: the CDS request is still queued/running server-side.

    The request was NOT cancelled -- CDS keeps processing it, and because
    :func:`fetch` is idempotent a later rerun resumes cheaply (CDS caches
    completed results server-side for a while).
    """


def climate_data_root(override: str | Path | None = None) -> Path:
    """Return the local (non-OneDrive) climate data directory.

    Priority: explicit override > ``CLIMATE_DATA_ROOT`` env var >
    ``%LOCALAPPDATA%/climate_data`` > ``~/.local/share/climate_data``.
    """
    env = os.environ.get("CLIMATE_DATA_ROOT", "").strip()
    if override is not None and str(override).strip():
        root = Path(override).expanduser().resolve()
    elif env:
        root = Path(env).expanduser().resolve()
    else:
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            root = Path(local_app) / "climate_data"
        else:
            root = Path.home() / ".local" / "share" / "climate_data"

    root.mkdir(parents=True, exist_ok=True)
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
    """Request dict for SEAS5 per-member monthly-mean 2m temperature.

    ``product_type='monthly_mean'`` is the per-ensemble-member monthly
    mean (one field per member) -- the EMOS input. NOT ``ensemble_mean``.
    """
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
    """Request dict for ERA5 monthly-averaged-reanalysis 2m temperature.

    ``time='00:00'`` is required by the dataset form for
    ``monthly_averaged_reanalysis`` (the field is a monthly mean; the
    hour is a formality of the API).
    """
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

    # quiet=False so CDS queue status lines reach the log; cdsapi reads
    # ~/.cdsapirc itself -- never read or print that file here.
    return cdsapi.Client(quiet=False, progress=False)


def fetch(
    dataset: str,
    request: dict,
    out_path: str | Path,
    soft_timeout: float | None = None,
) -> Path:
    """Download ``dataset``/``request`` to ``out_path`` (idempotent).

    * Skips entirely (no cdsapi import, no network) if ``out_path``
      already exists and is non-empty.
    * Downloads to a ``.part`` sibling and renames on success, so a
      partial download never poisons the cache.
    * ``soft_timeout`` (seconds, default None = wait forever): CDS
      queueing means a request can take minutes-to-hours. On expiry a
      :class:`CdsSoftTimeout` is raised; the server-side request keeps
      running and a later rerun resumes.
    * cdsapi exceptions are logged loudly and re-raised unchanged.
    """
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
