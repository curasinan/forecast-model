# Migration Checkpoint -- Polymarket -> IBKR ForecastEx

**Checkpoint date: 2026-08-25.** Purpose: let any future session (new model,
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
account is DONE, including the hourly gap backfill (2026-08-25). The paper
account EXISTS (**DU0000000**, $1,000,000 simulated) and the Gateway was
logged into it on 2026-08-23; the paper-parity compare was started but the
Gateway went offline before it finished, so Stage 0's last box is still open.
**Everything remaining needs a logged-in Gateway**: paper parity, the paper
dress rehearsal, and the first paper orders. As of 2026-08-25 12:54 the
Gateway is DOWN (796 consecutive refused connects since ~2026-08-24) and the
recorder is correctly sitting in its retry loop; sweeps exist only for
08-16, 08-17 and 08-23.

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
- [~] **Paper-account parity** -- account provisioned and verified reachable
      2026-08-23: **DU0000000** (DU prefix = paper), AvailableFunds
      $1,000,000.00 simulated, AccruedCash $1,291.20 (simulated interest on
      the simulated cash -- NOT a deposit and NOT the ForecastEx coupon).
      Probe D was resolving chains normally (ACD 72 contracts vs 70 in the
      week-old live snapshot = ordinary listing drift) when the Gateway went
      offline mid-run, so the universe diff was never produced. Re-run with
      the Gateway logged into PAPER:
      `python -u scripts/verify_ibkr_entitlements.py --port 4002 --compare data/ibkr_universe_live.json --report docs/stage0_report_2026-08-25_paper.md`
      (~20 min; exit 0 = parity, 4 = paper missing contracts).
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
- [x] Historical-bars/depth probe RUN 2026-08-23 (`docs/stage5_probe_findings.md`,
      venue_semantics Q4/Q7): depth = ONE level per side (top-of-book only);
      daily bars unavailable but HOURLY BID_ASK/MIDPOINT history exists back
      to 2026-01-17 -> recorder gaps are backfillable via HistoricalPacer.
- [x] **Gap backfill BUILT 2026-08-25**: `src/venues/ibkr/backfill.py` +
      `scripts/backfill_ibkr_hourly.py`. One paced reqHistoricalData call per
      (contract, tick type) spanning the whole gap; bars land in a SEPARATE
      tree `%LOCALAPPDATA%\ibkr_ticks\quotes_hourly\date=*\` because hourly
      OHLC aggregates are not point-in-time snapshots, and every row carries
      `what_to_show` + `source='reqHistoricalData'`. For BID_ASK bars IB packs
      open=avg bid, high=max ask, low=min bid, close=avg ask (see
      `bid_ask_columns()`). Contracts dropped by `--max-requests` are reported,
      never silently trimmed; rows flush every 25 answered requests so an
      interrupted multi-hour run keeps what it fetched.
- [x] **Backfill RUN 2026-08-25** (run_id 20260825T143919Z, ~75 min):
      360 requests, **0 failures**, **222,342 hourly BID_ASK bars** over
      2026-07-26..08-24 (30 date partitions, 420 files) for 314 of 360
      climate-lane contracts -- GT 84,836 / GTM 55,858 / ACD 50,120 /
      GTTA 18,640 / GTTM 12,888. The other 46 contracts have no history at
      all (same set that never quotes; consistent with the Stage-5 probe).
      Prices span 0.01..0.99 with no nulls. Deeper history (back to
      2026-01-17) needs ~8 more 30-day chunks at ~75 min each.
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
- [x] Paper account provisioned and connected 2026-08-23 (DU0000000; log the
      Gateway in with the LIVE username and the Paper toggle -- the separate
      paper username is not needed). Market-data sharing is ON, so paper sees
      the same quotes; `scripts/scheduling/run_ibkr_recorder.ps1` now defaults
      to `IBKR_TRADING_MODE=paper` to match.
- [x] Paper entrypoint `scripts/run_ibkr_paper.py` (GTM signals -> executor
      on IBKRAdapter; refuses unless IBKR_TRADING_MODE=paper AND the account
      is DU/DF; adapter transmits only on a verified paper account;
      --simulate for local fills). Still UNRUN against the real paper
      session: the dress rehearsal (`--simulate`) and the first
      `--mode approve` orders both need the Gateway logged into paper.

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
      trust as edge; see gtm_model_report.md section 5. `TradingExecutor` /
      `OrderManager` are now on `VenueAdapter` (ef4e31f) and
      `scripts/run_ibkr_paper.py` turns those signals into paper orders; what
      is left is running it against a live paper session.

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
- **`%LOCALAPPDATA%` is a trap under a packaged (MSIX) app** (found
  2026-08-25). Claude Code runs inside the packaged Claude app, which gets a
  per-app virtualised view of AppData: the SAME path resolved to two
  different stores depending on whether the writer ran inside the container
  or outside it (Task Scheduler). Symptom: the recorder heartbeat looked
  11,162 minutes stale to a probe run in-session while the hourly health task
  read a fresh one and exited 0. The whole climate ledger (78 files, 7.6 GB)
  sat in the container-private copy, invisible to any scheduled job, and the
  quote store was split (24 files one side, 21 the other). Fix: both data
  roots now default to `~/quant_data/...` (`settings.py`, `climate/paths.py`),
  data copied across, recorder restarted onto the new root. Never point a
  data root at AppData again, and read "two processes disagree about a file"
  as this until proven otherwise.
- Stopping the recorder task can ORPHAN its python child (the cmd.exe wrapper
  dies, python keeps running and keeps clientId=2, so the next instance would
  fail with error 326). After Stop-ScheduledTask, check for a surviving
  `record_ibkr_ticks.py` process before starting the task again.

---

## 4. How to run things

```
# Stage 0 gate (Gateway must be running and logged in)
python scripts/verify_ibkr_entitlements.py --port 4002 [--snapshot f.json] [--compare f.json]
# Recorder (normally Task Scheduler does this)
$env:IBKR_PORT='4002'; $env:IBKR_TRADING_MODE='paper'; python scripts/record_ibkr_ticks.py --interval-minutes 20
# Recorder health
python scripts/check_ibkr_recorder.py
# Backfill hourly bars for days the recorder missed (plan first, then run)
python scripts/backfill_ibkr_hourly.py --dry-run
python scripts/backfill_ibkr_hourly.py --port 4002 --mode paper
# Paper trading: dress rehearsal, then real paper orders one by one
python scripts/run_ibkr_paper.py --simulate
python scripts/run_ibkr_paper.py --mode approve --top 3
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

**Roots moved out of `%LOCALAPPDATA%` on 2026-08-25** -- see section 3 for
why. Everything now lives under `~/quant_data` (`C:\Users\<you>\quant_data`).

- `~/quant_data/ibkr_ticks/quotes/date=*/` -- IBKR quote sweeps (parquet)
- `~/quant_data/ibkr_ticks/quotes_hourly/date=*/` -- backfilled hourly bars
  (different semantics from sweeps; see Stage 5)
- `~/quant_data/ibkr_ticks/state/recorder_state.json` -- recorder heartbeat
- `~/quant_data/ibkr_ticks/contracts_cache.db` -- resolver cache
- `~/quant_data/climate_data/labels/labels_ledger.parquet` -- NOAA labels
- `~/quant_data/climate_data/raw|forecasts|reference/` -- CDS downloads + reductions
- `~/quant_data/climate_data/models/gtm_emos.json` -- fitted GTM EMOS
- `~/quant_data/climate_data/reports/gtm_emos_diagnostics.md` -- Step 4 report
- `~/quant_data/climate_data/paper/gtm_signals.json` -- latest paper ladder
- `logs\ibkr_recorder.log`, `logs\ibkr_recorder_task.log`, `logs\ibkr_events.jsonl`

## 6. Git state at checkpoint

Committed on `main` through `ad58918` (2026-08-23): `ad58918` Stage 5/8
finish (subscription manager, probe, migration report), `b548283`+`ef4e31f` executor
rewiring + paper order path, `d3dac8d` checkpoint, `1ff5f52` GTM model +
report, `d976323` Polymarket runtime gate, preceded by `597d96f` (backfills),
`7e8242f`, `e8ec379`, `99ace7a`, `c7d0e19`, `7f6d532`, `5cfddd7`, `aaa732a`.
Pushed through `def51b5` (Stage-5 probe results). 2026-08-25 adds the
recorder's paper default and the hourly-gap backfill.
Untracked and intentionally NOT part of the migration: `.agents/`,
`bist_brief/*`, `archive (1).zip`, `logs/`.

## 7. Open user actions (priority order)

1. **Clear "Read-Only API"**: Gateway -> Configure -> Settings -> API ->
   Settings, untick, OK, restart the Gateway, log back into PAPER. Error 321
   on 2026-08-25 proves it is currently ON, and whatIf answers happily in
   that mode -- so nothing so far has actually tested order transmission.
   This is the ONLY thing between here and
   `scripts/run_ibkr_paper.py --mode approve --top 3`, which starts the
   365-day live-paper clock. (Parity, backfill and the `--simulate` dress
   rehearsal are all done as of 2026-08-25.)
2. Add `collect_climate_labels.py` as a daily scheduled task (elevated PS:
   `scripts/scheduling/register_climate_tasks.ps1`) -- still NOT registered.
3. Unregister leftover `\Polymarket\` Task Scheduler jobs if they still exist
   (`scripts/scheduling/unregister_polymarket_tasks.ps1`, elevated).
4. Read the legal entity name off Client Portal for venue_semantics.md, and
   the ForecastEx live-feed subscription PRICE (read only, do not subscribe)
   for operating_costs.md.
5. Optional: configure Telegram ALERT_METHOD so health alerts leave log-only
   mode -- with the Gateway down for a day nothing alerted anyone.
6. Remaining engineering (not a user action): coupon-rate verification once a
   paper position exists (Stage 4/venue_semantics Q5); streaming/depth
   recorder on SubscriptionManager (Stage 5, optional).

Done and off this list: Stage 0 live gate, CDS licences + backfills, paper
account provisioning, executor rewiring, Polymarket runtime gate, migration
report, hourly-gap backfill build.
