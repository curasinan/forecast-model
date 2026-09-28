# tech-event-predictor

Quantitative prediction system. The live venue is **IBKR ForecastEx**
(climate lane): NOAA-label ingestion, SEAS5/ERA5-driven EMOS post-processing,
a strike ladder, and paper-trading execution against ForecastEx climate
contracts (`docs/ibkr_runbook.md` for operations, `docs/venue_semantics.md`
for verified venue facts). Paper-trading since 2026-08-25; live trading
stays a manual act by the account holder, never enabled by a commit here.

The original **Polymarket** track (evidence collection, probability
estimation, calibration, edge detection, position sizing) is research-only
now: its runtime entrypoints are gated off (`POLYMARKET_RUNTIME=1` required
to run them at all) and its live order/wallet code is retired to
`legacy/polymarket_onchain/`, but the research code, data, and findings are
preserved on purpose -- see `docs/MIGRATION_CHECKPOINT.md` for why the venue
changed and what was kept.

Research software. Nothing here is financial advice, and the trading layer
defaults to dry-run.

## Layout

| Path | What it is |
|---|---|
| `src/` | Core pipeline: predictors, features, math, evaluation, storage, trading, CLI dashboard |
| `collectors/` | Market, news, and resolution collectors (`collectors/weather/` for the weather track) |
| `weather/` | Weather probability layer — EMOS post-processing, buckets, calibration, serve features |
| `features/`, `model/`, `decision/`, `analysis/` | Feature engineering, model training, decision logic, offline studies |
| `monitoring/` | Runtime health checks and heartbeat state |
| `mcp_server/` | Stdio MCP server exposing market/weather tools to an agent |
| `scripts/` | CLI entry points (collection cycles, paper loop, fitting, dashboards) |
| `tests/` | Test suite — synthetic only, no live API calls |
| `config/` | Station registry, risk limits, base rates |
| `docs/` | Task inventories, API findings, scoping documents |
| `learning/` | Standalone scikit-learn study scripts (not part of the pipeline) |
| `bist_brief/` | Independent tool: BIST 100 daily bulletin generator |

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env   # then fill in keys
```

Optional extras: `.[trading]` for Polymarket execution, `.[mcp]` for the MCP
server, `.[web]` for the web UI.

## Tests

```bash
python -m pytest -q
```

## Weather track

The weather subsystem has its own guide covering data-root setup, Open-Meteo
licensing, the historical EMOS training pipeline, and lead-day semantics:
[`weather/README.md`](weather/README.md).

Two properties are load-bearing there and easy to break:

- **No look-ahead.** Training joins require a forecast's effective init plus the
  per-model availability lag to precede the decision time, and labels to be
  finalized and at least 72 hours old.
- **Train/serve feature parity.** Fitted parameters are stamped with a
  `feature_set`, and the serve path refuses any parameters that do not match.

## Configuration

Settings load from `.env` via pydantic-settings (`src/config.py`). Secrets never
belong in the repo — `.env` is gitignored; `.env.example` documents the keys.

Weather data (SQLite + Parquet) is deliberately stored outside the repo and off
OneDrive, defaulting to `~/quant_data/polymarket_weather` (override with
`WEATHER_DATA_ROOT`). Never `%LOCALAPPDATA%`: that was the pre-2026-08-25
default, moved off it after a documented packaged-app incident where the
same path silently resolved to two different stores depending on whether
the writer ran inside the app or from Task Scheduler -- see the historical
account in `docs/MIGRATION_CHECKPOINT.md` section 3.
