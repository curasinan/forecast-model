# Cursor tasks (weather + Polymarket live track)

Inventory of work done by Cursor in this repo for the weather prediction-market stack and MCP.  
Companion: [OPUS_TASKS.md](OPUS_TASKS.md) (Claude Code / Opus historical EMOS track).

---

## 1. Phase 0 gates

- Open-Meteo commercial / non-commercial license gate (`OPEN_METEO_API_KEY`, self-host, or `OPEN_METEO_ALLOW_NONCOMMERCIAL`)
- Weather data root off OneDrive (`%LOCALAPPDATA%\polymarket_weather` / `WEATHER_DATA_ROOT`)
- Lower snapshot liquidity/volume floors for temperature markets in `get_snapshot_candidates`
- Station registry: `config/weather_stations.json` + `weather/stations.py`

**Key paths:** `weather/open_meteo.py`, `weather/paths.py`, `src/config.py`, `collectors/collect_markets.py`

---

## 2. Weather collectors MVP

- Market parser (rules → ICAO / buckets / platform): `collectors/weather/parse_markets.py`
- Open-Meteo ensemble forecasts: `collectors/weather/collect_forecasts.py`
- IEM ASOS obs: `collectors/weather/collect_obs_iem.py`
- WU / CLI label collectors: `collect_labels_wu.py`, `collect_labels_cli.py`
- Storage schema: `weather/storage.py` (`weather_markets`, `forecast_daily`, labels, etc.)

---

## 3. EMOS, buckets, edge, paper criteria

- Truncation-aware buckets: `weather/buckets.py`
- EMOS fit/predict (CRPS): `weather/emos.py`
- Calibration helpers: `weather/calibrate.py`
- Weather edge + half-Kelly (not politics `edge_calculator`): `weather/edge.py`
- Go-live paper criteria: `weather/paper_criteria.py`
- Optional ML quantile scaffolding: `weather/ml_quantile.py`
- Seed + paper scripts: `scripts/seed_weather_markets.py`, `scripts/run_weather_paper.py`
- Collect cycle orchestration: `scripts/run_weather_collect_cycle.py`

---

## 4. WU label parsing fix

- Bug: HTML regex matched font names → bogus temps (e.g. `2.0` °F)
- Fix: weather.com historical API for daily max/min (verified e.g. KLGA)

**Key path:** `collectors/weather/collect_labels_wu.py`

---

## 5. Serve-path feature parity (`xmodel_prevruns_v1`)

- Claude-fitted EMOS uses cross-model Previous Runs mean/var
- Added `weather/serve_features.py` + load filter so paper never uses ensemble-member stats for those params
- Paper hang fix: skip dates outside today..+7d; cache features per (station, date, metric)

**Key paths:** `weather/serve_features.py`, `scripts/run_weather_paper.py`

---

## 6. Live market discovery (offline-capable)

- `collectors/weather/discover_live_markets.py` — Gamma + local `polymarket.db`
- Date parse without year (`on July 22?`) using `end_date`
- City-hint ICAO when description missing; reject global climate series
- Purge `wx-*` fixtures on discover; paper skips fixtures by default
- `collectors/weather/refresh_markets.py` — Gamma → `polymarket.db` (fail-fast on timeout/SSL)

---

## 7. Quotes + weather snapshots

- `weather/market_quotes.py` — `resolve_quote` (snapshot mid, then live CLOB)
- `collectors/weather/collect_weather_snapshots.py` — thin weather books without politics liquidity filter
- Paper flags: `--require-live-quote`, `--horizon-past-days`, `--no-live-clob`, `--skip-gamma`

---

## 8. Project MCP server (live + recent data)

- Package: `mcp_server/` (FastMCP stdio)
- Cursor config: `.cursor/mcp.json` → server name `polymarket-weather`
- Tools: `status`, `wx_*` discover/list/quotes/forecasts/obs/paper, `pm_*` list/snapshots/edges
- Resources: `polymarket://status`, `weather://status`, `weather://markets`
- Tests: `tests/test_mcp_server/` (10 passed at ship)
- Docs: `mcp_server/README.md`, pointer in `weather/README.md`
- Deps: `pyproject.toml` optional extra `mcp`

**Agent flow:** `status` → `wx_discover_markets` → `wx_get_quote` / `wx_snapshot_books` → `wx_paper_signals`

---

## 9. Tests & docs (Cursor-owned)

- Weather test suite under `tests/test_weather/` (parse, quotes, discover, snapshots, serve features, etc.)
- Scheduling notes updated for weather collect cycle
- `.env.example` Open-Meteo / weather vars

---

## Known blockers left for ops (not unfinished Cursor code)

- Gamma/CLOB often unreachable here (SSL intercept / ConnectTimeout)
- `polymarket.db` snapshots last seen ~2026-07-19 until refresh runs on a working network
- Live city markets after mid-July need `wx_refresh_gamma` when Gamma works
