# Opus / Claude Code tasks (historical EMOS track)

Inventory of work done by Claude Code (Opus) for historical training data, labels, and fitted EMOS params.  
Companion: [CURSOR_TASKS.md](CURSOR_TASKS.md) (Cursor live markets / serve / MCP track).

---

## 1. Previous Runs forecast history backfill

- Collector: `collectors/weather/collect_history_forecasts.py`
- Table: `forecast_history_daily` in weather DB
- Look-ahead rules: never ingest `previous_day0`; `previous_dayN → lead_day N−1`; effective-init / decision-time joins
- Expanded from June smoke to multi-station / multi-year coverage (status later showed ~55k history rows)

**Key paths:** `weather/history.py`, `collectors/weather/collect_history_forecasts.py`

---

## 2. Observation history + `obs_daily`

- IEM ASOS history collection + local-calendar daily max/min aggregation
- Table: `obs_daily`
- Later scale: thousands of station-days (status showed ~2811 `obs_daily` rows)

**Key paths:** `weather/obs_daily.py`, `collectors/weather/collect_obs_iem.py` (`--aggregate-daily`)

---

## 3. EMOS fit pipeline + model store

- Fit script: `scripts/fit_emos_history.py`
- Persist params under `%LOCALAPPDATA%\polymarket_weather\models\emos\*.json`
- Stamp `feature_set: xmodel_prevruns_v1` (cross-model Previous Runs mean/var)
- Modules: `weather/training.py`, `weather/model_store.py`, `weather/finalization.py`
- Scale: smoke (~12 JSON) → fuller refit (status showed ~42 EMOS model files)
- Lead-6 often empty with default 3-model set (ICON lacks `previous_day7`); ECMWF/GFS-only path documented where needed

---

## 4. Settlement-quality labels + training join

- Expand `resolution_labels` (CLI / WU / ASOS sources)
- Label priority for training (`iem_cli` > `nws_cli` > `wunderground_api` > `iem_asos`) so ASOS does not clobber settlement truth
- Fahrenheit settlement labels skipped for `unit_default: C` stations
- Document ~0.5–1°F ASOS bias vs CLI/WU for US markets
- Later scale: thousands of label rows (status showed ~5615 `resolution_labels`)

**Key paths:** `weather/training.py`, label collectors, `weather/README.md` historical section

---

## 5. Adversarial / reliability fixes (training path)

- Prevent label clobbering (worse source overwriting better)
- Unit consistency guards
- Self-hosted / Open-Meteo routing corrections for history fetch
- Training join decision-time lag semantics preserved

---

## 6. Tests for history / fit / store

- `tests/test_weather/` coverage for history collect, training join, model store, fit script, label guards, previous-runs client, storage migration, finalization, obs_daily
- Weather suite kept green alongside Cursor live-path tests (~90+ weather tests in earlier full runs)

---

## 7. Commit / handoff

- Committed historical EMOS + label/backfill work so Cursor MCP/`status` sees filled weather DB
- Did **not** own live Gamma discovery, CLOB snapshot ops, or MCP server (Cursor track)

---

## Out of scope for this track (by design)

- `run_weather_paper.py` serve-path rewiring (Cursor already did `xmodel_prevruns_v1` parity)
- Live order execution
- Project MCP server (`mcp_server/`)
