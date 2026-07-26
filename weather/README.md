# Weather probability layer

Station-level temperature PDFs for Polymarket weather markets.

## Phase 0 gates

### Open-Meteo licensing

The public hosts are **non-commercial**:

- Forecast: `https://api.open-meteo.com`
- Ensemble: `https://ensemble-api.open-meteo.com` (used by `collect_forecasts`)

For this trading bot, set one of:

1. `OPEN_METEO_API_KEY` — customer ensemble host `customer-ensemble-api.open-meteo.com`
2. `OPEN_METEO_BASE_URL` — point at a [self-hosted Open-Meteo](https://github.com/open-meteo/open-meteo)
3. `OPEN_METEO_ALLOW_NONCOMMERCIAL=true` — research-only escape hatch (collectors refuse trading mode otherwise)

### Data directory (avoid OneDrive)

SQLite + OneDrive sync corrupts databases. Weather data defaults to:

- Windows: `%LOCALAPPDATA%\polymarket_weather`
- Override: `WEATHER_DATA_ROOT=C:\data\polymarket_weather`

### Snapshot liquidity

`get_snapshot_candidates` applies a lower liquidity/volume floor for temperature questions (`WEATHER_MIN_LIQUIDITY`, `WEATHER_MIN_VOLUME`).

## Work split

| Track | Owner | Scope |
|---|---|---|
| Historical EMOS backfill (Previous/Single Runs + IEM history + fit) | Claude Code | training data / fitted params |
| Live markets + CLOB quotes + serve-path parity | Cursor | discover, `xmodel_prevruns_v1` features, paper wire-up |

## Commands

```bash
# Refresh active station-temp markets into data/polymarket.db (Gamma)
python -m collectors.weather.refresh_markets

# Live discovery (local polymarket.db; optional Gamma) → weather_markets
python -m collectors.weather.discover_live_markets --skip-gamma

# Full collect cycle (refresh + discover + forecasts + US IEM obs)
python scripts/run_weather_collect_cycle.py --allow-noncommercial

# Forecasts only (requires commercial key or --allow-noncommercial for research)
python -m collectors.weather.collect_forecasts --station KLGA --allow-noncommercial

# Observations
python -m collectors.weather.collect_obs_iem --station KLGA

# Labels
python -m collectors.weather.collect_labels_wu --station KLGA --date 2026-07-20
python -m collectors.weather.collect_labels_cli --station KNYC --date 2026-07-20

# Paper loop — loads EMOS with feature_set=xmodel_prevruns_v1 only;
# builds the same Previous-Runs cross-model features at serve time;
# uses snapshot mids, with optional live CLOB fallback
python scripts/run_weather_paper.py --allow-noncommercial
python scripts/run_weather_paper.py --require-live-quote --allow-noncommercial --paper-days 90

# Snapshot CLOB books for rows already in weather_markets (thin books OK)
python -m collectors.weather.collect_weather_snapshots --limit 100
```

### Live market ops (Cursor track)

1. `refresh_markets` needs Gamma reachability. If `ConnectTimeout`, use VPN/network
   fix — local `polymarket.db` may be stale (no new daily city markets).
2. `discover_live_markets --skip-gamma` parses local DB without descriptions by
   city hint + `end_date` year (titles like `on July 22?`).
3. Quotes: `resolve_quote` prefers `snapshots`, else live CLOB via `clob_token_ids`.
4. Seed fixtures (`wx-*`) are purged on discover and skipped by paper by default.


### Serve-path feature parity

Fitted params under `models/emos/*.json` are stamped
`feature_set: xmodel_prevruns_v1`. The paper loop:

1. Loads params via `load_emos_for_feature_set` (mismatched feature_sets dropped).
2. Builds serve features with `build_xmodel_features` from
   `forecast_history_daily`, fetching Previous Runs live into the DB when a
   market date is missing (`weather/serve_features.py`).
3. Applies `predict_emos(ens_mean, ens_var)` — never the live ensemble-member
   mean/std path.


## Historical EMOS training

Backfill Open-Meteo **Previous Runs** forecasts + IEM ASOS observations, then
fit and persist EMOS params per station × metric × lead_day:

```bash
# 1) Forecast history (Previous Runs API; archived since ~Jan 2024)
python -m collectors.weather.collect_history_forecasts --station KLGA --station KORD --station KLAX --allow-noncommercial

# 2) Settlement-quality labels — NWS CLI archive, one request per station-year
python -m collectors.weather.collect_labels_cli --history --station KLGA --station KORD --station KLAX

# 3) Observation history + local-calendar daily max/min (obs_daily; ASOS fallback labels)
python -m collectors.weather.collect_obs_iem --station KLGA --station KORD --station KLAX --start 2024-01-01 --end 2026-07-25 --aggregate-daily

# 4) Fit + persist params to <data_root>/models/emos/<STATION>_<metric>_lead<k>.json
python scripts/fit_emos_history.py --station KLGA --station KORD --station KLAX

# Lead 6 needs the two models that publish previous_day7 (ICON stops at day6)
python scripts/fit_emos_history.py --station KLGA --lead-days 6 --models ecmwf_ifs025 --models gfs_seamless
```

### Label sources (settlement quality first)

`build_training_frame` resolves one label per day by priority
(`TrainingFrameSpec.label_sources`, default
`iem_cli > nws_cli > wunderground_api > iem_asos`). The first three come from
`resolution_labels` (settlement truth, whole °F); `iem_asos` is the
`obs_daily` fallback derived from METAR samples. Each fitted model records
the realised mix in `metadata.label_source` / `label_source_counts`, so
in-sample CRPS is always interpretable against the label it was trained on.

Settlement labels are Fahrenheit whole degrees, so they are **skipped for
`unit_default: C` stations** (a 77 °F CLI value must never be joined as
77 °C); those stations train on `obs_daily` until a metric-aware conversion
exists.

### Lead-day semantics

The Previous Runs API serves **hourly** `temperature_2m_previous_dayN`
(N=1..7): the value forecast for each hour by runs ~24·N hours older. Hourly
values over local day D embed runs initialized up to
`utc(start of local day D+1) − 24N h`; we store that **conservative effective
init** and map `previous_dayN → lead_day N−1` (consistent with the repo's
init→local-noon lead convention). `previous_day0` is the stitched latest-run
series — pure look-ahead — and is never ingested. Training joins apply
`effective_init + model availability lag ≤ decision time` (local
`--decision-hour`, default 12, on day D−lead).

### Caveats

- **Licensing**: the backfill hits the free previous-runs host — research
  only (`--allow-noncommercial` / `OPEN_METEO_ALLOW_NONCOMMERCIAL=true`), or
  set `OPEN_METEO_API_KEY` for the customer host.
- **Label bias (addressed)**: ASOS METAR extremes understate true daily
  extremes by ~0.5–1°F versus the NWS CLI values US markets resolve on.
  `collect_labels_cli --history` backfills CLI truth from IEM's archive and
  training prefers it; only days with no CLI report fall back to
  `iem_asos`. Check `metadata.label_source_counts` on a fitted model before
  reading its CRPS as market-calibrated.
- **Archive gaps**: Open-Meteo Previous Runs does not reach 2024-01-01 for
  every model — `gfs_seamless`/`icon_seamless` start ~2024-01-19 and
  `ecmwf_ifs025` ~2024-02-03, so full-ensemble training effectively starts
  in February 2024. `icon_seamless` publishes no `temperature_2m_previous_day7`,
  so **lead 6 is empty under the default 3-model set** — fit it with
  `--models ecmwf_ifs025 --models gfs_seamless` (recorded in
  `metadata.models`, a different ensemble composition than leads 0–5).
- **Multi-source labels**: a station-date-metric may carry several oracle
  rows. Consumers of `resolution_labels` must pick one — see
  `weather.training.label_priority_rank_sql`, used by the paper calibration
  join — or each prediction is counted once per source.
- **Feature parity (resolved for paper)**: params fitted on
  `xmodel_prevruns_v1` are consumed only with matching serve features.
  `run_weather_paper.py` refuses other feature_sets.

## Important

- Do **not** call `decision/edge_calculator.py` for weather — use `weather.edge.weather_edge`.
- Wunderground is a sparse resolution oracle (Global); NWS CLI is the US oracle.
- Labels need `finalized=1` and ≥72h age before `usable_for_training=1`.

## MCP (Cursor agents)

Live/recent fetch & analysis tools for agents: see [`mcp_server/README.md`](../mcp_server/README.md).

```bash
pip install -e ".[mcp,trading]"
python -m mcp_server   # stdio; Cursor loads .cursor/mcp.json
```

Typical agent flow: `status` → `wx_discover_markets` → `wx_get_quote` / `wx_snapshot_books` → `wx_paper_signals`.
