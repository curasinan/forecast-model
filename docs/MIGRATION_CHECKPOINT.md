# Migration Checkpoint -- Polymarket -> IBKR ForecastEx

**Checkpoint date: 2026-08-23.** Purpose: let any future session (new model,
fresh context, resumed after a break) pick up exactly here. Read this first,
then `docs/venue_semantics.md` (verified venue facts) and
`docs/feature_migration_audit.md` (per-feature verdicts). The original spec
is the 8-stage migration specification supplied by the account holder; the
execution order below deviates from the spec's numbering where evidence
justified it.

Legend: `[x]` done and verified, `[~]` partially done, `[ ]` not started,
`[U]` blocked on a user action.

---

## 0. One-paragraph state

Stage 0 gate PASSED on the live account (2,964 ForecastEx contracts,
2,900 tradable). Venue abstraction, IBKR adapter package, pacing limiter,
fee/carry-aware sizing math, import ban, legacy retirement, gateway
supervisor, health monitoring and a Task-Scheduler-managed quote recorder
are built, tested and running. Stage 6 audit done; user chose the
**climate lane first**. Climate steps 1–4 (oracles, NOAA ledger, CDS
hindcast, GTM EMOS + strike ladder + diagnostics) are in tree. Polymarket
**runtime** (collectors, scheduled jobs, MCP Gamma/CLOB tools, scan/trade
CLIs) is gated off. Executor is rewired onto VenueAdapter with a paper-only IBKR order path;
subscription manager, Gateway probe, and the final migration report are in
tree. Every engineering item that does not need a live Gateway or the paper
account is DONE. Next: live paper book on the GTM ladder (blocked on
paper-account provisioning) and running the Stage-5 probe (blocked on a
Gateway re-login -- recorder has been dark since 2026-08-17).

---

## 1. Spec stages -- status

### Stage 0 -- Entitlement gate  `[x]` live / `[U]` paper parity
- [x] `scripts/verify_ibkr_entitlements.py` written and run 2026-08-16
      (reports `docs/stage0_report_2026-08-16_live.md` + `_followup.md`;
      universe snapshot `data/ibkr_universe_live.json`, committed).
- [x] Permissions present (Client Portal tile "Prediction Markets" enabled).
- [x] Universe: 2,964 contracts / 2,900 tradable / 64 underlyings.
- [x] Order types via whatIf: LMT DAY, LMT GTC (resting OK), MKT, conditional.
- [x] Commission: flat $0.01/contract billed separately (NOT an embedded
      $1.01 price sum; mid YES+NO ~ 1.00).
- [U] **Paper-account parity** -- paper account not yet provisioned in
      Client Portal. When it exists: log Gateway in as paper, then run
      `python scripts/verify_ibkr_entitlements.py --port 4002 --compare data/ibkr_universe_live.json`.
- [U] Legal entity name -- read off Client Portal, record in venue_semantics.md.
- [ ] Coupon rate -- verify from statements / a real position (paper phase).

### Stage 1 -- Venue abstraction  `[x]`
- [x] `src/domain/` (MarketId, FeeModel/FlatCommissionFeeModel,
      CarryModel/ForecastExCouponCarry with staleness guards, Market incl.
      scheduled_resolution_time, Quote, OrderBook, OrderRequest ...).
- [x] `src/venues/base.py` VenueAdapter (read-only default, single-writer).
- [x] `src/venues/polymarket.py` read-only research adapter + the sanctioned
      `readonly_clob_client()`; the three collectors repointed through it.
- [x] Import ban: ruff TID251 + `tests/test_architecture/test_import_bans.py`.
- [x] **TradingExecutor / OrderManager rewired onto VenueAdapter** (commit
      ef4e31f, 2026-08-23): TradeIntent input, fee/carry/holding-period
      re-pricing, NO leg at its own quote or FeeModel complement, whole-
      contract rounding, simulated vs venue paths, sync() via get_order.

### Stage 2 -- Connection layer  `[x]`
- [x] ib_async 2.1.0 (works on Python 3.14.2).
- [x] `src/venues/ibkr/clients.py` client-id map 1=live_trading /
      2=data_collector / 3=research; error 326 fails loudly, never auto-increments.
- [x] `src/venues/ibkr/gateway.py` GatewaySession: active reqCurrentTime
      heartbeat, jittered backoff 1s->60s, resubscription hooks, structured
      ConnectionEvent (kind/ts_utc/expected/downtime_seconds) appended to
      `logs/ibkr_events.jsonl`, expected nightly-restart window.
- [x] Task Scheduler: `\IBKR\IBKR Recorder` (Interactive logon, AtLogOn +
      5-min keep-alive, IgnoreNew, 3650-day limit) and
      `\IBKR\IBKR Recorder Health` (hourly). Registered by the USER from an
      ELEVATED PowerShell via `scripts/scheduling/register_ibkr_tasks.ps1`.
      (S4U logon type was tried first and produced phantom runs -- do not
      go back to it.)
- [x] `scripts/check_ibkr_recorder.py` health probe -> `monitoring/heartbeat.py`
      (Telegram if ALERT_METHOD configured, else log-only).
- [ ] Headless Gateway auto-login (IBC) deliberately NOT configured -- it
      stores credentials on disk; user decision. Gateway is set to Auto
      restart at **23:45 local** and needs a manual re-login weekly.

### Stage 3 -- Contract identity  `[x]`
- [x] MarketId; `src/venues/ibkr/contracts.py` resolver with SQLite cache
      (24h TTL, flush on reconnect, seeded from the Stage 0 snapshot).
- [x] Yes/No are SEPARATE conIds: secType OPT on FORECASTX, right C=YES /
      P=NO, localSymbol `SYM_MMYY_strike_YES|NO`, exactly 2 per
      (symbol, expiry, strike). Underlyings are non-tradable IND rows.
- [x] Complement-pricing audit documented; math reparameterised via FeeModel.

### Stage 4 -- Fees, carry, settlement  `[~]` (coupon verification open)
- [x] EV and Kelly extended with fee_model / carry_model / holding_days
      (bit-identical defaults; Kelly with carry f = p*c/(c-r) - q*c/(1+r-c)).
- [x] Live Polymarket on-chain path retired to `legacy/polymarket_onchain/`.
- [x] Fee/carry/holding-days wired into the executor's re-pricing (net EV +
      fee/carry-aware half-Kelly feed the existing risk checks) -- ef4e31f.
- [ ] Coupon-rate verification (CarryModel refuses stale/unverified rates).

### Stage 5 -- Market data & pacing  `[~]`
- [x] `src/venues/ibkr/pacing.py` HistoricalPacer (15s identical / 6 per 2s
      per contract / 60 per 10 min with BID_ASK x2; blocking, logged).
- [x] `src/venues/ibkr/recorder.py`: snapshot sweeps (~13 min per ~2,800
      contracts, delayed data type 4, batches of 50 reqTickers) every 20 min
      -> `%LOCALAPPDATA%\ibkr_ticks\quotes\date=YYYY-MM-DD\*.parquet`
      (date partition + con_id column; per-conid partitioning rejected as
      thousands of tiny files). Heartbeat at `...\state\recorder_state.json`.
      RUNNING under Task Scheduler since 2026-08-17 23:55.
- [x] `src/venues/ibkr/subscriptions.py` SubscriptionManager: line-budget
      accounting, lowest-priority/LRU eviction (raises LineBudgetExceeded,
      never silently drops), subscribe-rate token bucket. Wiring a streaming/
      depth recorder onto it is the only remaining Stage-5 build.
- [~] Historical-bars/depth probe WRITTEN (`scripts/probe_ibkr_history.py`);
      must be RUN against a live Gateway to fill `docs/stage5_probe_findings.md`.
- [x] `docs/operating_costs.md` filled (delayed free, $0.01 commission, ~100
      line budget); only the live-feed subscription PRICE stays UNKNOWN.

### Stage 6 -- Modelling consequences  `[x]` audit; decision made
- [x] `docs/feature_migration_audit.md` (30+ verdicts, 13 flagged decisions).
- [x] scheduled_resolution_time populated from the contract settlement date.
- [x] Validation reconciliation: purged k-fold / triple-barrier / deflated
      Sharpe DO NOT EXIST in this repo; temporal split + causal features +
      pre-registered evaluation do, and must not be weakened.
- [x] **USER DECISION 2026-08-18: CLIMATE LANE FIRST** (GTM monthly global
      temperature, ACD CO2, hurricane contracts), reusing the weather
      track's EMOS / bucket-calibration / go-live discipline. The 18 tech
      questions map to nothing tradable; Polymarket research stays read-only.

### Stage 7 -- Paper trading  `[~]`
- [x] `IBKRSettings`: IBKR_PORT and IBKR_TRADING_MODE REQUIRED, no default.
- [x] Mode-mismatch guard (DU/DF prefix = paper) + WARNING log at connect.
- [U] Paper account provisioning (Client Portal -> Paper Trading Account).
- [x] Paper entrypoint `scripts/run_ibkr_paper.py` (GTM signals -> executor
      on IBKRAdapter; refuses unless IBKR_TRADING_MODE=paper AND the account
      is DU/DF; adapter transmits only on a verified paper account;
      --simulate for local fills). Untested against a real paper session
      until the account exists.

### Stage 8 -- Deliverables
- [x] Stage 0 script + reports; venue_semantics.md; feature_migration_audit.md;
      operating_costs.md (skeleton); src/venues/*; recorder running;
      ibkr_runbook.md; climate_oracle_research.md.
- [x] Final migration report `docs/migration_report.md` (11 invalidated
      assumptions with re-verified file:line, stage table, honest gaps).
- (historical) seed list: dead live-order path executor.py:236, FILLED never
      set, complement-pricing inventory, CLI mode default in trade.py, VPIN
      inert, liquidity check no-op, three id schemes, no gas code existed,
      the spec's embedded-$1.01 fee claim contradicted by whatIf).

---

## 2. Climate lane -- status

- [x] **Step 1 -- resolution oracles** (commit 99ace7a), from the venue's
      own T&C PDFs (`docs/climate_oracle_research.md` = evidence trail).
      GT/GTM = NOAA NCEI global land+ocean anomaly, baseline 1901-2000,
      strictly >; GTTA/GTTM = pre-industrial via a +0.16 C venue offset,
      early resolution possible; ACD = NOAA GML Mauna Loa annual mean
      (Jan 5, 8:00 CT); hurricanes = NHC operational advisories (NOT
      HURDAT2), calendar year, counts are inclusive "at least";
      **HLF strikes are REGION CODES, not numbers**; Rule 603(e) settlement
      irrevocable = first-print vintage governs.
- [x] **Step 2a -- NOAA label ledger LIVE** (commit 7f6d532):
      `climate/noaa.py`, `climate/storage.py`, `scripts/collect_climate_labels.py`.
      3,183 rows at `%LOCALAPPDATA%\climate_data\labels\labels_ledger.parquet`
      (gtm monthly 1850-01..2026-07 latest +1.18 C; gt annual; acd_annual
      latest 2025 = 427.35 ppm; acd_monthly). First-print / revision
      discipline + oracle-drift detection (fails if base != 1901-2000).
      [ ] schedule `collect_climate_labels.py` daily (Task Scheduler, elevated).
- [x] **Step 2b -- CDS forecast pipeline** (commits 7f6d532, 7e8242f):
      `climate/cds.py`, `climate/reduce.py`, `scripts/fetch_climate_forecasts.py`
      (--pilot | --hindcast YEARS | --era5 YEARS); SEAS5 = system 51, per-member
      monthly means; ERA5 monthly reference; idempotent cache; cos-lat global
      mean. Licences accepted by user 2026-08-23; pilot 4/4 OK.
- [x] **Step 3 -- backfills DONE 2026-08-23**: SEAS5 1993-2025 (33/33 one-year
      requests, 7.6 GB raw) -> `forecasts/seas5_global_mean.parquet` 76,860 rows,
      398 init months 1993-01..2026-08, leads 1-6; **25 members for 1993-2016
      hindcasts, 51 from 2017 (SEAS5 design, not a gap)**. ERA5 1993-2025 ->
      `reference/era5_global_mean.parquet` 396 monthly rows, gap-free.
- [x] **Step 4 -- GTM model** (2026-08-23): ERA5→NOAA monthly-intercept
      bridge (`climate/bridge.py`), training frame (`climate/dataset.py`,
      lead-1 = init month), per-lead Gaussian EMOS (`climate/emos.py`),
      strictly-greater strike ladder vs recorder quotes (`climate/ladder.py`),
      fit + expanding-window diagnostics (`climate/fit.py`,
      `scripts/fit_gtm_emos.py`). Tests under `tests/test_climate/`.
      **FIT RUN 2026-08-23** (commit 1ff5f52, `docs/gtm_model_report.md`):
      trend variant operational (`models/gtm_emos.json`; `_notrend` kept);
      bridge R2 0.983 / resid 0.036C; OOS expanding-window skill vs
      trend-climatology 0.49 (lead 1) .. 0.26 (lead 6), PIT near-flat,
      coverage 50%/90% = 0.535/0.882. Hindcast model gate PASSES.
- [x] **Step 5 -- go-live / edge gates re-scoped** (`climate/paper_criteria.py`):
      weather's 200 buckets / 90 days is unreachable at ~12 GTM resolutions/yr.
      Hindcast CRPS skill vs trend climatology is the model gate; live paper
      is 365 days + 12 resolved months (opt-in). min_edge floor = $0.01
      commission + 1 tick = 0.02; price band 0.05–0.95.
- [~] **Step 6 -- paper loop + executor**: research-only GTM paper loop
      (`climate/paper.py`, `scripts/run_gtm_paper.py`) scores the latest
      recorder sweep against the fitted EMOS and writes signals — **no
      orders**. First run 2026-08-23: 52 GTM contracts, 23 pass the
      research edge gate, go_live=False (live paper gate of 365 days +
      12 resolved months is now REQUIRED by default; `--hindcast-gate-only`
      shows the model gate). Dec-2026 ladder shows a ~60-point
      model-vs-market gap (model mu 1.44C, lead 5) -- paper-track, do not
      trust as edge; see gtm_model_report.md section 5. `TradingExecutor` / `OrderManager` remain on the retired
      Polymarket dry-run wallet path; rewiring onto `VenueAdapter` still
      waits on paper-account provisioning (Stage 7 [U]).

---

## 3. Things that bit us (do not rediscover)

- The IB Gateway port number does NOT imply paper/live; the login does.
  Account U0000000 is LIVE; a paper account starts with DU/DF.
- Always set `IB.RequestTimeout` (60): a request dropped during a
  connectivity blip otherwise blocks forever.
- Symbol-less wildcard reqContractDetails NEVER answers; the per-symbol
  OPT wildcard works; reqSecDefOptParams on the IND conId is the fallback;
  some symbols (FF Fed Funds, CPI headline) genuinely have no chains today.
- whatIf/orders need an explicit `tif`, or error 10349 aborts the round-trip.
- Task Scheduler S4U logon = phantom runs (instant 0x0, no side effects);
  use Interactive. A PT0S time limit may mean "stop immediately".
- The auto-mode classifier blocks registering/creating scheduled tasks and
  sometimes commits containing them -- the user runs those from an elevated
  PowerShell (PowerShell 5.1 needs `;`, not `&&`).
- Gateway nightly restart is 23:45 local ->
  `IBKR_RESTART_WINDOW_LOCAL=23:30-00:15` is set in `.env`.
- Credentials: the IBKR login is held by Gateway only; the CDS token lives
  at `~/.cdsapirc` (outside repo and OneDrive; `.cdsapirc` is gitignored).
  Never paste keys into chat.
- Python: the repo runs on 3.14; a performance test over the live DB was
  made hermetic (38617dc) because the pool grows with data.
- RC* symbols are Regional CPI, not rainfall; ForecastEx lists NO daily
  weather products -- the weather track's EMOS engine has no direct target.

---

## 4. How to run things

```
# Stage 0 gate (Gateway must be running and logged in)
python scripts/verify_ibkr_entitlements.py --port 4002 [--snapshot f.json] [--compare f.json]
# Recorder (normally Task Scheduler does this)
$env:IBKR_PORT='4002'; $env:IBKR_TRADING_MODE='live'; python scripts/record_ibkr_ticks.py --interval-minutes 20
# Recorder health
python scripts/check_ibkr_recorder.py
# Climate labels (daily) / first backfill
python scripts/collect_climate_labels.py [--backfill]
# Climate forecasts (needs CDS licences accepted)
python scripts/fetch_climate_forecasts.py --pilot | --hindcast 2000-2025
# GTM EMOS fit + diagnostics (writes under %LOCALAPPDATA%\climate_data)
python scripts/fit_gtm_emos.py [--trend]
# GTM paper signals vs latest IBKR recorder sweep (no orders)
python scripts/run_gtm_paper.py
# Tests / lint
python -m pytest -q ; python -m ruff check .

# Polymarket collectors/trade CLIs exit 2 unless POLYMARKET_RUNTIME=1
```

Task Scheduler: `Get-ScheduledTask -TaskPath '\IBKR\' | Get-ScheduledTaskInfo`;
remove with `scripts/scheduling/unregister_ibkr_tasks.ps1`. Leftover
`\Polymarket\` collector jobs: `scripts/scheduling/unregister_polymarket_tasks.ps1`.

## 5. Data locations (all outside the OneDrive repo on purpose)

- `%LOCALAPPDATA%\ibkr_ticks\quotes\date=*\` -- IBKR quote sweeps (parquet)
- `%LOCALAPPDATA%\ibkr_ticks\state\recorder_state.json` -- recorder heartbeat
- `%LOCALAPPDATA%\ibkr_ticks\contracts_cache.db` -- resolver cache
- `%LOCALAPPDATA%\climate_data\labels\labels_ledger.parquet` -- NOAA labels
- `%LOCALAPPDATA%\climate_data\raw|forecasts|reference\` -- CDS downloads + reductions
- `%LOCALAPPDATA%\climate_data\models\gtm_emos.json` -- fitted GTM EMOS
- `%LOCALAPPDATA%\climate_data\reports\gtm_emos_diagnostics.md` -- Step 4 report
- `%LOCALAPPDATA%\climate_data\paper\gtm_signals.json` -- latest paper ladder
- `logs\ibkr_recorder.log`, `logs\ibkr_recorder_task.log`, `logs\ibkr_events.jsonl`

## 6. Git state at checkpoint

Committed on `main` through `ad58918` (2026-08-23): `ad58918` Stage 5/8
finish (subscription manager, probe, migration report), `b548283`+`ef4e31f` executor
rewiring + paper order path, `d3dac8d` checkpoint, `1ff5f52` GTM model +
report, `d976323` Polymarket runtime gate, preceded by `597d96f` (backfills),
`7e8242f`, `e8ec379`, `99ace7a`, `c7d0e19`, `7f6d532`, `5cfddd7`, `aaa732a`.
Last push was `d3dac8d` -- run `git push origin main` (5 commits ahead).
Untracked and intentionally NOT part of the migration: `.agents/`,
`bist_brief/*`, `archive (1).zip`, `logs/`.

## 7. Open user actions (priority order)

0. **Recorder has been dark since 2026-08-17** -- log IB Gateway back in
   (weekly re-login; the supervisor resumes automatically). Every dark day
   is unrecoverable training data. Then run `scripts/probe_ibkr_history.py`.

1. (done 2026-08-23) CDS licences accepted; hindcast + ERA5 backfilled.
2. Provision the paper account; run the paper-parity compare.
3. Add `collect_climate_labels.py` as a daily scheduled task (elevated PS).
4. Unregister leftover `\Polymarket\` Task Scheduler jobs if they still exist
   (`scripts/scheduling/unregister_polymarket_tasks.ps1`, elevated).
5. Read the legal entity name off Client Portal for venue_semantics.md.
6. `git push origin main` (everything is committed through d976323).
7. Optional: configure Telegram ALERT_METHOD so health alerts leave log-only mode.
8. Remaining engineering (not a user action): coupon-rate verification;
   streaming/depth recorder + historical-bars probe (Stage 5); final
   migration report (Stage 8).
