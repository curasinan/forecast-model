"""FastMCP app: live/recent Polymarket + weather tools and resources."""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server.serializers import json_safe
from mcp_server.tools import markets, quotes, signals, status, weather_data

# Stdio MCP: never log to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)

mcp = FastMCP("polymarket-weather")


# ----- tools ---------------------------------------------------------------


@mcp.tool(name="status")
def status_tool(ping_network: bool = False) -> dict[str, Any]:
    """DB paths and table counts. Network Gamma/CLOB ping is off by default (retired)."""
    return status.status(ping_network=ping_network)


@mcp.tool()
def wx_list_markets(
    station_icao: str | None = None,
    min_valid_date: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List station temperature markets from weather_markets (recent/active)."""
    return markets.wx_list_markets(
        station_icao=station_icao,
        min_valid_date=min_valid_date,
        limit=limit,
    )


@mcp.tool()
def wx_discover_markets(
    skip_gamma: bool = True,
    min_end_date: str | None = None,
    local_limit: int = 2000,
) -> dict[str, Any]:
    """Refresh weather_markets from local polymarket.db (default skip_gamma=True)."""
    return markets.wx_discover_markets(
        skip_gamma=skip_gamma,
        min_end_date=min_end_date,
        local_limit=local_limit,
    )


@mcp.tool()
def wx_refresh_gamma(timeout: float = 12.0) -> dict[str, Any]:
    """Retired: live Gamma pull into polymarket.db. Use IBKR climate scripts instead."""
    return markets.wx_refresh_gamma(timeout=timeout)


@mcp.tool()
def wx_get_quote(
    market_id: str,
    allow_live: bool = False,
    clob_token_ids: str | None = None,
) -> dict[str, Any]:
    """Resolve mid/spread from local snapshots (live CLOB collection retired)."""
    return quotes.wx_get_quote(
        market_id,
        allow_live=allow_live,
        clob_token_ids=clob_token_ids,
    )


@mcp.tool()
def wx_snapshot_books(limit: int = 100, delay_ms: int = 100) -> dict[str, Any]:
    """Retired: live CLOB snapshots. Quotes come from the IBKR recorder now."""
    return quotes.wx_snapshot_books(limit=limit, delay_ms=delay_ms)


@mcp.tool()
def wx_collect_forecasts(
    station_icao: str,
    allow_noncommercial: bool = False,
    include_ai: bool = False,
) -> dict[str, Any]:
    """Collect Open-Meteo ensemble forecasts for a station (license gate applies)."""
    return weather_data.wx_collect_forecasts(
        station_icao,
        allow_noncommercial=allow_noncommercial,
        include_ai=include_ai,
    )


@mcp.tool()
def wx_collect_obs(station_icao: str, hours: int = 48) -> dict[str, Any]:
    """Collect recent IEM ASOS observations for a US K* station."""
    return weather_data.wx_collect_obs(station_icao, hours=hours)


@mcp.tool()
def wx_paper_signals(
    min_edge: float = 0.05,
    require_live_quote: bool = False,
    allow_noncommercial: bool = False,
    allow_live_clob: bool = True,
    horizon_past_days: int = 0,
    horizon_future_days: int = 7,
    fetch_features: bool = True,
) -> dict[str, Any]:
    """Retired: Polymarket weather paper loop. Use scripts/run_gtm_paper.py."""
    return signals.wx_paper_signals(
        min_edge=min_edge,
        require_live_quote=require_live_quote,
        allow_noncommercial=allow_noncommercial,
        allow_live_clob=allow_live_clob,
        horizon_past_days=horizon_past_days,
        horizon_future_days=horizon_future_days,
        fetch_features=fetch_features,
    )


@mcp.tool()
def pm_list_markets(
    query: str | None = None,
    snapshot_candidates_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """List recent active markets from polymarket.db."""
    return markets.pm_list_markets(
        query=query,
        snapshot_candidates_only=snapshot_candidates_only,
        limit=limit,
    )


@mcp.tool()
def pm_latest_snapshots(
    market_ids: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Latest snapshot mid/spread per market from local polymarket.db (archival)."""
    return quotes.pm_latest_snapshots(market_ids=market_ids, limit=limit)


@mcp.tool()
def pm_recent_edges(
    limit: int = 50,
    tradeable_only: bool = False,
    try_generate: bool = True,
) -> dict[str, Any]:
    """Recent politics/ML edge signals from DB or generate_signals."""
    return signals.pm_recent_edges(
        limit=limit,
        tradeable_only=tradeable_only,
        try_generate=try_generate,
    )


# ----- resources -----------------------------------------------------------


@mcp.resource("polymarket://status")
def resource_polymarket_status() -> str:
    """Polymarket DB path, market/snapshot counts, latest snapshot timestamp."""
    import json

    return json.dumps(json_safe(status.polymarket_status()), indent=2)


@mcp.resource("weather://status")
def resource_weather_status() -> str:
    """Weather DB path, table counts, EMOS model file count."""
    import json

    return json.dumps(json_safe(status.weather_status()), indent=2)


@mcp.resource("weather://markets")
def resource_weather_markets() -> str:
    """Compact list of weather_markets (upcoming / recent)."""
    import json
    from datetime import date, timedelta

    min_d = (date.today() - timedelta(days=3)).isoformat()
    payload = markets.wx_list_markets(min_valid_date=min_d, limit=100)
    return json.dumps(json_safe(payload), indent=2)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
