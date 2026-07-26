# Polymarket + Weather MCP Server

Stdio MCP server for **live and recent** market/weather fetch & analysis. Wraps existing collectors — does not reimplement EMOS or trading execution.

## Setup

```bash
pip install -e ".[mcp,trading]"
```

Cursor loads [`.cursor/mcp.json`](../.cursor/mcp.json). Enable **polymarket-weather** under Settings → MCP, then restart the agent chat if needed.

Env (from repo `.env` / shell):

- `OPEN_METEO_API_KEY` or `OPEN_METEO_ALLOW_NONCOMMERCIAL=true` for forecasts/paper feature fetch
- `WEATHER_DATA_ROOT` (default `%LOCALAPPDATA%\polymarket_weather`)

Logs go to **stderr** only (stdout is JSON-RPC).

## Agent workflow

1. `status` — DB counts + Gamma/CLOB ping  
2. `wx_discover_markets` (`skip_gamma=true` if Gamma blocked)  
3. `wx_snapshot_books` / `wx_get_quote` — live/recent mids  
4. `wx_collect_forecasts` / `wx_collect_obs` — recent weather inputs  
5. `wx_paper_signals` — model vs market edges  

When Gamma fails (timeout/SSL): stay on local `polymarket.db` via discover; fix network/VPN for refresh.

## Tools (v1)

| Tool | Purpose |
|---|---|
| `status` | Health + connectivity |
| `wx_list_markets` | List `weather_markets` |
| `wx_discover_markets` | Parse local DB → weather_markets |
| `wx_refresh_gamma` | Live Gamma pull → polymarket.db |
| `wx_get_quote` | Snapshot / live CLOB mid |
| `wx_snapshot_books` | CLOB snapshots for weather markets |
| `wx_collect_forecasts` | Open-Meteo ensemble collect |
| `wx_collect_obs` | IEM ASOS obs (K* stations) |
| `wx_paper_signals` | Weather paper edge loop |
| `pm_list_markets` | Active polymarket.db markets |
| `pm_latest_snapshots` | Latest mids from snapshots |
| `pm_recent_edges` | Politics/ML edges (DB or generate) |

## Resources

- `polymarket://status`
- `weather://status`
- `weather://markets`

## Run manually

```bash
python -m mcp_server
```

## Safety

Read + collect + analyse only. No order placement / wallet tools.
