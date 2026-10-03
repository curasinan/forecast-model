# MCP Server (archival weather DB + climate handoff)

Stdio MCP server. **Polymarket collection is retired** (Gamma/CLOB pulls,
weather paper loop, live quote refresh). Local `polymarket.db` reads still
work for archival research. The trading venue is IBKR ForecastEx — use the
climate scripts, not these PM tools.

```bash
pip install -e ".[mcp,trading]"
```

Cursor loads [`.cursor/mcp.json`](../.cursor/mcp.json).

## What still runs

- `status` (DB counts; network ping **off** by default)
- `wx_list_markets`, `wx_discover_markets(skip_gamma=true)` — local DB
- `wx_get_quote(allow_live=false)` — local snapshots
- `pm_list_markets`, `pm_latest_snapshots` — local DB
- `wx_collect_forecasts` / `wx_collect_obs` — Open-Meteo / IEM (not Polymarket)

## What returns `polymarket_retired`

- `wx_refresh_gamma`, `wx_snapshot_books`, `wx_paper_signals`
- `wx_get_quote(allow_live=true)`, `wx_discover_markets(skip_gamma=false)`
- `pm_recent_edges(try_generate=true)`

Climate / IBKR instead:

```bash
python scripts/collect_climate_labels.py
python scripts/fit_gtm_emos.py --trend --live
python scripts/run_gtm_paper.py
```

## Safety

No order placement. Retired PM tools do not call Gamma or CLOB.
