# forecast-model

Probabilistic forecast models and the pipeline around them: data collection,
post-processing, calibration, evaluation, and a paper-trading harness that
turns a forecast distribution into orders on a prediction market.

The active model is the **GTM climate forecast**, a forecast of NOAA's
monthly global temperature anomaly. It takes ECMWF SEAS5 seasonal forecasts,
bridges them from ERA5 to the NOAA series, and post-processes each lead with
a Gaussian EMOS. It is verified by expanding-window CRPS skill against trend
climatology ([`docs/gtm_model_report.md`](docs/gtm_model_report.md)), and its
forecasts are paper-traded as a strike ladder on **IBKR ForecastEx** climate
contracts. Paper trading started on 2026-08-25.

This is research software. Nothing here is financial advice. The trading
layer defaults to dry-run, the venue adapter refuses to transmit on a live
account, and switching to live trading is not something a commit in this
repository does.

## Contents

- [What is in here](#what-is-in-here)
- [Layout](#layout)
- [Setup](#setup)
- [Configuration](#configuration)
- [Climate pipeline](#climate-pipeline)
- [IBKR ForecastEx: recorder and paper trading](#ibkr-forecastex-recorder-and-paper-trading)
- [Scheduling and deployment](#scheduling-and-deployment)
- [Weather track](#weather-track)
- [Polymarket track](#polymarket-track)
- [LLM components](#llm-components)
- [Tests and lint](#tests-and-lint)
- [Conventions](#conventions)
- [Documents](#documents)
- [License](#license)

## What is in here

| Track | State | Where |
|---|---|---|
| Climate lane (GTM) | Active, paper-traded | `climate/`, `scripts/*climate*`, `scripts/fit_gtm_emos.py`, `scripts/run_gtm_paper.py` |
| IBKR ForecastEx venue | Active (paper) | `src/venues/ibkr/`, `src/trading/`, `scripts/*ibkr*` |
| Weather EMOS | Research | `weather/`, `collectors/weather/` |
| Polymarket | Research only, runtime retired | `collectors/`, `features/`, `model/`, `decision/`, `analysis/`, `legacy/` |
| Local LLM client | Data processing only, never pricing | `src/llm/` |

The Polymarket track came first. Two of its offline studies are worth
knowing: the fill simulation found that resting maker orders on political
markets were mostly filled when the price was about to move against them,
and the Monte Carlo study found that under the measured frictions a
quarter-Kelly strategy bleeds slowly rather than blows up
(`analysis/results/`). The research code and result tables are kept on
purpose; [`docs/migration_report.md`](docs/migration_report.md) records what
the move to ForecastEx invalidated.

## Layout

| Path | What it is |
|---|---|
| `climate/` | GTM lane: NOAA labels, Copernicus retrievals, ERA5 to NOAA bridge, EMOS fit, strike ladder, paper loop, go-live gates |
| `src/venues/` | Venue abstraction. `src/venues/ibkr/` is the IB Gateway adapter: session supervisor, pacing limiter, quote recorder, hourly backfill |
| `src/trading/` | Executor, risk manager, position tracker, paper-book reconciliation, lock file |
| `src/llm/` | Client for a local OpenAI-compatible model server |
| `src/` (rest) | Earlier pipeline: predictors, features, math, evaluation, storage, CLI dashboard |
| `weather/` | Station temperature layer: EMOS, bucket probabilities, calibration, serve features |
| `collectors/` | Market, news and resolution collectors; `collectors/weather/` for the weather track |
| `features/`, `model/`, `decision/`, `analysis/` | Feature engineering, model training, decision logic, offline studies |
| `monitoring/` | Alert dispatcher and daily backups |
| `mcp_server/` | Stdio MCP server exposing archival market and weather tools |
| `scripts/` | Command-line entry points |
| `scripts/scheduling/` | Windows Task Scheduler registration and launchers |
| `scripts/spark/`, `deploy/spark/` | Tooling and systemd units for running on a Linux box |
| `tests/` | Test suite: synthetic only, no live API calls |
| `config/` | Station registry, risk limits, base rates |
| `data/`, `model/results/`, `analysis/results/` | Small result tables and contract listings kept for the record |
| `docs/` | Reports, runbooks and verified venue facts |
| `legacy/` | Retired Polymarket on-chain order path, kept for reference |
| `learning/` | Standalone scikit-learn study scripts, not part of the pipeline |
| `bist_brief/` | Independent tool: BIST 100 daily bulletin generator |

## Setup

Python 3.12 or newer.

```bash
pip install -e ".[dev]"
cp .env.example .env
```

Optional extras:

| Extra | Adds |
|---|---|
| `ibkr` | `ib_async`, for anything that talks to IB Gateway |
| `climate` | `cdsapi`, `xarray`, `netcdf4`, for Copernicus retrievals |
| `trading` | `py-clob-client`, for the retired Polymarket path |
| `mcp` | the MCP server |
| `web` | the web UI |

The whole set: `pip install -e ".[dev,ibkr,climate,mcp,web,trading]"`.

## Configuration

Settings load from `.env` (`src/config.py`, `src/venues/ibkr/settings.py`,
`src/llm/settings.py`). Secrets never belong in the repository: `.env` is
gitignored and `.env.example` lists the keys.

### Data roots

Data is stored outside the repository on purpose. Keep these directories off
cloud-synced folders such as OneDrive, because sync during a write corrupts
SQLite and Parquet files, and off `%LOCALAPPDATA%`, because a packaged
Windows app sees a private copy of that folder, so the same path can resolve
to two different stores.

| Key | Default | Holds |
|---|---|---|
| `CLIMATE_DATA_ROOT` | `~/quant_data/climate_data` | Label ledger, forecasts, fitted model, paper signals |
| `IBKR_DATA_ROOT` | `~/quant_data/ibkr_ticks` | Recorder sweeps, hourly bars, heartbeat, paper book |
| `WEATHER_DATA_ROOT` | `~/quant_data/polymarket_weather` | Weather SQLite and Parquet |
| `DATABASE_PATH` | `data/predictions.db` | Predictions database of the earlier pipeline |

`CLIMATE_DATA_ROOT` is read straight from the process environment by
`climate/paths.py`, not through `src/config.py`.

### IBKR ForecastEx

| Key | Meaning |
|---|---|
| `IBKR_PORT` | **Required, no default.** Gateway: 4001 live, 4002 paper. TWS: 7496, 7497 |
| `IBKR_TRADING_MODE` | **Required, no default.** `paper` or `live`; checked against the account prefix on connect |
| `IBKR_HOST` | Default `127.0.0.1` |
| `IBKR_COMMISSION_PER_CONTRACT`, `IBKR_FEES_VERIFIED_ON` | Fee per contract and the date it was verified by a what-if probe. The fee model refuses to price without both |
| `IBKR_COUPON_APY`, `IBKR_COUPON_VERIFIED_ON` | Coupon rate and the date it was verified from statements. Carry is null without both |
| `IBKR_MAX_DATA_LINES` | Cap on concurrent market-data lines, default 100 |
| `IBKR_RECORD_INTERVAL_MINUTES` | Recorder sweep cadence, default 10 |
| `IBKR_MARKET_DATA_TYPE` | Default 4, delayed-frozen, which needs no paid subscription |
| `IBKR_RESTART_WINDOW_LOCAL` | `HH:MM-HH:MM` on the machine's local clock, may cross midnight; default `06:45-08:15`. Disconnects inside it are expected. Set it to your Gateway's actual restart time |

The two required keys have no defaults by design. A process that does not
say which Gateway and which mode it expects fails at start-up instead of
attaching to whatever session is listening.

### Weather

| Key | Meaning |
|---|---|
| `OPEN_METEO_API_KEY` | Customer key, selects the customer host |
| `OPEN_METEO_BASE_URL` | A self-hosted Open-Meteo instead |
| `OPEN_METEO_ALLOW_NONCOMMERCIAL` | Research-only switch that allows the free host. The free host is non-commercial, and the collectors refuse it in trading mode |
| `WEATHER_MIN_LIQUIDITY`, `WEATHER_MIN_VOLUME` | Floors for snapshot candidates |

### LLM

| Key | Meaning |
|---|---|
| `LLM_MODEL` | **Required where the local client is used.** The id the server lists under `/v1/models` |
| `LLM_BASE_URL` | Default `http://127.0.0.1:8000/v1` |
| `LLM_API_KEY` | Default `EMPTY`; vLLM ignores it |
| `LLM_MAX_TOKENS`, `LLM_REASONING_EFFORT`, `LLM_TEMPERATURE` | Generation settings. Keep max tokens at 8192 or more for a reasoning model |
| `LLM_CONNECT_TIMEOUT_S`, `LLM_READ_TIMEOUT_S` | Defaults 10 and 900 |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | Only for the Polymarket-era predictor and scorer |
| `LLM_PROMPTS_DIR` | Where prompt texts are read from, default `prompts/` under the repo root |

Other keys: `NEWSAPI_KEY` and `GITHUB_TOKEN` for optional evidence
collectors, `POLYMARKET_API_BASE`, and `POLYMARKET_RUNTIME=1` to run a
retired Polymarket entry point once.

Copernicus credentials go in `~/.cdsapirc`, never in the repository.

## Climate pipeline

Each step writes under `CLIMATE_DATA_ROOT`.

```bash
# 1. NOAA labels. First run backfills, later runs append first prints.
python scripts/collect_climate_labels.py --backfill
python scripts/collect_climate_labels.py

# 2. SEAS5 forecasts and the ERA5 reference. Needs a CDS account with the
#    dataset licences accepted.
python scripts/fetch_climate_forecasts.py --pilot
python scripts/fetch_climate_forecasts.py --hindcast 1993-2016

# 3. Fit the EMOS and write diagnostics.
python scripts/fit_gtm_emos.py --trend --live
python scripts/fit_gtm_emos.py --trend --root ~/quant_data_snapshot/climate_data

# 4. Compare the forecast ladder with the latest recorded quotes. No orders.
python scripts/run_gtm_paper.py
```

Things worth knowing:

- **Labels are a first-print ledger.** ForecastEx settles on the first
  published value, so the collector appends new periods and records later
  revisions as separate rows. It never overwrites. Run it daily.
- **A fit names its root.** `--live` refits the model the paper runtime
  reads; `--root` fits a copy. With neither, nothing is fitted and the script
  lists the files it would have overwritten. Keep `--trend` on a live refit:
  the operational model is the trend variant.
- **Staleness gates.** The paper scripts refuse to price off an old quote
  sweep, an old signal file, or a SEAS5 initialisation older than the one
  that should exist by now (`climate/paper_criteria.py`).
- **Go-live gates** are pre-registered: hindcast skill now, then 365 paper
  days and 12 resolved target months.

## IBKR ForecastEx: recorder and paper trading

Everything here needs a running, logged-in IB Gateway or TWS, the `ibkr`
extra, and `IBKR_PORT` and `IBKR_TRADING_MODE` set.

```bash
# What the account can see and trade. Read-only.
python scripts/verify_ibkr_entitlements.py --port 4002

# Record delayed quotes: one sweep, or a loop.
python scripts/record_ibkr_ticks.py --once
python scripts/record_ibkr_ticks.py

# Is the recorder healthy? Never touches the Gateway.
python scripts/check_ibkr_recorder.py
python scripts/check_ibkr_recorder.py --test-alert

# Hourly bars for days the recorder missed. Plan first.
python scripts/backfill_ibkr_hourly.py --dry-run
python scripts/backfill_ibkr_hourly.py

# Paper orders from the latest signals.
python scripts/run_ibkr_paper.py --simulate
python scripts/run_ibkr_paper.py
python scripts/run_ibkr_paper.py --mode auto --top 5

# Record what happened to resting orders.
python scripts/sync_ibkr_paper.py
```

The health probe exits 0 for `OK` and 1 for `STALE`, `CRITICAL`, `DOWN` or
`DEGRADED`, and alerts only when the state changes. `ALERT_METHOD` selects
the channel; the default only logs.

Safety properties, enforced in code:

- The paper entry points refuse to start unless `IBKR_TRADING_MODE=paper`
  and the connected account has a paper prefix (`DU` or `DF`).
- The adapter refuses to transmit an order on a live account.
- `--simulate` runs the same pipeline with local fills and a read-only
  adapter. The default mode asks for confirmation of each order.
- The reconciliation script uses a read-only adapter and cannot place or
  cancel an order.
- A lock file keeps two writers off the same paper book.
- Venue SDKs may be imported only under `src/venues/`, and the LLM client is
  fenced out of the pricing and ordering path. Tests in
  `tests/test_architecture/` enforce both.

Operations are described in [`docs/ibkr_runbook.md`](docs/ibkr_runbook.md),
verified venue facts in [`docs/venue_semantics.md`](docs/venue_semantics.md).

## Scheduling and deployment

**Windows.** The scripts in `scripts/scheduling/` register Task Scheduler
jobs and never start anything themselves. Run them from an elevated
PowerShell with the repository root as the current directory.

| Script | Does |
|---|---|
| `register_ibkr_tasks.ps1` | Registers the recorder, its health probe and the paper sync |
| `register_climate_tasks.ps1` | Registers the daily label collector |
| `register_spark_pull_task.ps1` | Registers the periodic pull of the Linux box's data root |
| `unregister_*.ps1` | Remove the matching tasks |
| `run_*.ps1` | The launchers the tasks call |
| `swap_live_root_for_mirror.ps1` | Swaps the live data root for a marked mirror at a cutover; `-Undo` reverses it |

**Linux.** `deploy/spark/` holds systemd units, timers and a sudoers drop-in
for running the recorder, health probe, paper sync, label collector and
backup under a dedicated `trading` user. `scripts/spark/` holds the tooling:

| Script | Does |
|---|---|
| `deploy.sh` | Pushes `main` to the runtime clone and restarts the recorder between two sweeps |
| `push_snapshot.sh`, `pull_snapshot.sh` | Copy a frozen data root to the box, or its live root back. Run inside WSL |
| `mirror_live.sh` | Refreshes the research copy from the live root, on the box |
| `research_run.sh` | Runs a research job under a host-memory ceiling; `--gpu-budget` sets its GPU budget |
| `rollback.sh` | Moves the runtime back to the Windows machine |
| `session_check.py`, `cutover_check.py`, `restore_check.py`, `verify_mirror.py` | Checks around a cutover |
| `cgroup_cuda_check.py` | Checks whether the memory ceiling covers GPU memory |

A data root that carries the marker file `.mirror-of-spark` is a copy, and
the code refuses to write live state into it. The full procedure is in
[`docs/spark_runbook.md`](docs/spark_runbook.md).

**GPU memory.** The ceiling that `research_run.sh` sets bounds host memory
only: on the box it was measured on, CUDA allocations are not charged to the
job's cgroup. The GPU side is bounded inside the job instead.
`--gpu-budget 20G` hands the job `RESEARCH_GPU_BUDGET_GIB=20`, and
`apply_gpu_budget()` in `analysis/gpu_budget.py` caps PyTorch's allocator at
that figure. A job may ask for less, never for more, and without the flag
the variable is removed from the job's environment. Every file in the
repository that imports torch has to call `apply_gpu_budget()` before its
first CUDA allocation; `tests/test_architecture/test_gpu_budget_guard.py`
fails for one that does not.

## Weather track

Station-level temperature distributions: Open-Meteo ensemble and
previous-run forecasts, station observations, EMOS fitted per station, metric
and lead day, and truncation-aware bucket probabilities. Guide:
[`weather/README.md`](weather/README.md).

Two properties are load-bearing there and easy to break:

- **No look-ahead.** A training join requires the forecast's effective
  initialisation plus the model's availability lag to precede the decision
  time, and labels to be final and at least 72 hours old.
- **Train and serve feature parity.** Fitted parameters carry a
  `feature_set` stamp, and the serve path refuses parameters that do not
  match.

## Polymarket track

Research only. The collectors, the trade CLIs and the MCP tools that fetch
from Polymarket exit unless `POLYMARKET_RUNTIME=1` is set for a one-off
archival run. The live order and wallet code is retired to
`legacy/polymarket_onchain/`. Offline studies still run against a local
database:

| Module | Question it answers |
|---|---|
| `analysis/calibration_study.py` | Are resolved markets calibrated, and is there a favourite-longshot bias? |
| `analysis/fill_simulator.py` | Did resting limit orders fill, and at what cost? |
| `analysis/monte_carlo.py` | Variance and ruin for a quarter-Kelly strategy |
| `analysis/prospective_evaluation.py` | Pre-registered evaluation of price-blinded LLM scores |
| `model/train.py` | Price-change regression with overfitting checks |

## LLM components

- `src/llm/` talks to a local OpenAI-compatible server. Its role is fixed:
  it turns text into structured data and is never the predictor.
  `python scripts/llm_smoke.py` checks that the server is up and serves the
  configured model.
- The Polymarket-era predictor (`src/predictors/llm_predictor.py`) and scorer
  (`collectors/collect_llm_scores.py`) call the Anthropic API.

**Prompt texts are not in the repository.** Those two modules read their
system prompts from `prompts/llm_predictor_system.txt` and
`prompts/llm_scores_system.txt`. The directory is gitignored; write your own
files there, or point `LLM_PROMPTS_DIR` at another directory. A missing file
raises an error that names the path.

### Comparing local models

`scripts/llm_eval.py` compares models served by vLLM on one set of
gold-labelled extraction cases (`data/llm_eval_cases.json`), all under the
same serving protocol. For each candidate it reports the share of replies
that are valid JSON for their schema, field accuracy against the gold values,
tokens per second, and the memory the model takes. It runs on the Linux box
that serves the models.

```bash
# Free memory with no model loaded.
python scripts/llm_eval.py baseline --out baseline.json

# One candidate: every case, three times by default.
python scripts/llm_eval.py run --label NAME --model MODEL --container CONTAINER \
    --baseline baseline.json --out NAME.json

# The comparison.
python scripts/llm_eval.py report A.json B.json --out report.txt
```

`baseline` refuses while a model server still answers. `run` refuses a
container that is not running and healthy, that is served outside the
protocol, or that does not serve the named model. `report` refuses results
that ran different cases, repeats, protocol or request settings.

## Tests and lint

```bash
python -m pytest -q
ruff check .
```

The suite is synthetic: no network, no Gateway, and it redirects the home
directory so nothing is written under `~/quant_data`.

## Conventions

- Explanations live in this README and in `docs/`. Code comments are kept to
  short single lines.
- Data, logs, credentials, prompt texts and working notes stay out of
  version control (`.gitignore`).
- Account identifiers in the documents are placeholders: `U0000000` for a
  live account and `DU0000000` for a paper account.

## Documents

| Document | Content |
|---|---|
| [`docs/MIGRATION_CHECKPOINT.md`](docs/MIGRATION_CHECKPOINT.md) | Project state, stage by stage, and the pitfalls found on the way |
| [`docs/venue_semantics.md`](docs/venue_semantics.md) | Verified ForecastEx facts: settlement, fees, order types, oracles |
| [`docs/gtm_model_report.md`](docs/gtm_model_report.md) | The GTM model: data, bridge, variants, skill, gates |
| [`docs/gtm_bridge_robustness.md`](docs/gtm_bridge_robustness.md) | Hindcast skill with the bridge fitted inside each window |
| [`docs/ibkr_runbook.md`](docs/ibkr_runbook.md) | Running the recorder: durability, re-login, alerts, triage |
| [`docs/spark_runbook.md`](docs/spark_runbook.md) | Running research and the runtime on a Linux box |
| [`docs/spark_parity_2026-10-03.md`](docs/spark_parity_2026-10-03.md) | Evidence that two machines produce the same numbers |
| [`docs/migration_report.md`](docs/migration_report.md) | Why and how the venue changed |
| [`docs/feature_migration_audit.md`](docs/feature_migration_audit.md) | Per-feature verdicts for the venue change |
| [`docs/operating_costs.md`](docs/operating_costs.md) | What the venue costs to run |
| [`docs/climate_oracle_research.md`](docs/climate_oracle_research.md) | Evidence trail for the contract resolution oracles |
| `docs/stage0_report_*.md`, [`docs/stage5_probe_findings.md`](docs/stage5_probe_findings.md) | Entitlement and data-availability probes |
| [`docs/api_findings.md`](docs/api_findings.md), [`docs/sports-migration-scoping.md`](docs/sports-migration-scoping.md) | Earlier API probes and a scoping study |

## License

MIT, see [`LICENSE`](LICENSE). Dependencies and data sources keep their own
licences and terms: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
