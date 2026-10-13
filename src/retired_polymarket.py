"""Polymarket collectors and trade CLIs are retired; IBKR ForecastEx is the venue."""

from __future__ import annotations

import os
import sys

ENV_OVERRIDE = "POLYMARKET_RUNTIME"
RETIRED_EXIT = 2
RETIRED_CODE = "polymarket_retired"

RETIRED_MESSAGE = (
    "Polymarket runtime is retired. This repo's trading venue is IBKR "
    "ForecastEx (climate lane). Do not run Polymarket collectors, scanners, "
    "or the legacy trade CLI. Use scripts/record_ibkr_ticks.py, "
    "scripts/collect_climate_labels.py, scripts/fit_gtm_emos.py, "
    "scripts/run_gtm_paper.py. Override only for archival research: "
    "set POLYMARKET_RUNTIME=1."
)


def polymarket_runtime_allowed() -> bool:
    return os.environ.get(ENV_OVERRIDE, "").strip().lower() in {"1", "true", "yes"}


def refuse_polymarket_runtime(entrypoint: str) -> None:
    """Exit 2 unless POLYMARKET_RUNTIME=1. Call at the top of a CLI main()."""
    if polymarket_runtime_allowed():
        print(
            f"{entrypoint}: POLYMARKET_RUNTIME=1 set; proceeding with retired Polymarket path",
            file=sys.stderr,
        )
        return
    print(f"{entrypoint}: {RETIRED_MESSAGE}", file=sys.stderr)
    raise SystemExit(RETIRED_EXIT)


def retired_mcp_error(tool: str) -> dict:
    """JSON payload for an MCP tool that would have collected/traded on Polymarket."""
    from mcp_server.serializers import err

    return err(
        RETIRED_CODE,
        RETIRED_MESSAGE,
        tool=tool,
        hint="IBKR climate lane: scripts/fit_gtm_emos.py and scripts/run_gtm_paper.py",
    )
