# Migration Checkpoint -- Polymarket -> IBKR ForecastEx

**Checkpoint date: 2026-08-25 (migration COMPLETE).** Purpose: record the state of the
migration so that work can be picked up exactly here. Read this first,
then `docs/venue_semantics.md` (verified venue facts) and
`docs/feature_migration_audit.md` (per-feature verdicts). The original spec
is the 8-stage migration specification supplied by the account holder; the
execution order below deviates from the spec's numbering where evidence
justified it.

Legend: `[x]` done and verified, `[~]` partially done, `[ ]` not started,
`[U]` blocked on a user action.

---

## 0. One-paragraph state

**THE MIGRATION IS DONE. All 8 stages are closed and the system is trading
on paper.** As of 2026-08-25 19:58 local, three BUY LMT GTC orders are
resting on ForecastEx under paper account **DU0000000** (`GTM_1226_1.2_YES`
29 @ 0.34, `_1.3_YES` 41 @ 0.24, `_1.4_YES` 71 @ 0.14; $29.64 of a $100
bankroll). **The 365-day live-paper clock starts 2026-08-25.**

What is running unattended: the quote recorder sweeps ~2,800 ForecastEx
contracts every 20 minutes into `~/quant_data/ibkr_ticks` (Task Scheduler,
paper session, clientId 2); a health probe checks it hourly; the NOAA label
collector runs daily at 16:30. Stage 0 passed on both the live account
(2,964 contracts) and paper (3,097, parity confirmed 2026-08-25). Venue
abstraction, adapter package, pacing limiter, fee/carry-aware sizing,
import ban, gateway supervisor, subscription manager, hourly-gap backfill
(222,342 bars recovered) and the climate lane (oracles -> NOAA first-print
ledger -> SEAS5/ERA5 -> trend-EMOS GTM model -> strike ladder -> paper
orders) are all built, tested and exercised against the venue. The
Polymarket **runtime** is gated off; its research code and data are
preserved on purpose.

From here the work is accumulation, not construction: let the paper book
fill, verify the coupon rate off a filled position (the last open venue
fact), and re-evaluate at 12 resolved months + 365 days
(`climate/paper_criteria.py`). Live trading stays a manual act by the
account holder; no commit in this repo enables it.

---

## 1. Spec stages -- status

### Stage 0 -- Entitlement gate  `[x]` live + `[x]` paper parity
- [x] `scripts/verify_ibkr_entitlements.py` written and run 2026-08-16
      (reports `docs/stage0_report_2026-08-16_live.md` + `_followup.md`;
      universe snapshot `data/ibkr_universe_live.json`, committed).
- [x] Permissions present (Client Portal tile "Prediction Markets" enabled).
- [x] Universe: 2,964 contracts / 2,900 tradable / 64 underlyings.
- [x] Order types via whatIf: LMT DAY, LMT GTC (resting OK), MKT, conditional.
- [x] Commission: flat $0.01/contract billed separately (NOT an embedded
      $1.01 price sum; mid YES+NO ~ 1.00).
- [x] **Paper-account parity VERIFIED 2026-08-25 (exit 0)** --
      **DU0000000** (DU prefix = paper), AvailableFunds $1,000,000.00
      simulated, AccruedCash simulated interest on that cash (NOT a deposit,
      NOT the ForecastEx coupon). Paper sees **3,097 contracts / 3,032
      tradable** vs 2,964 in the 9-day-old live snapshot; of the 140
      "missing", **112 simply expired** between 08-16 and 08-25. Yes/No
      structure, $0.01 commission and all four order types reproduce, and
      quote coverage is identical (bid>0 on 1,872 of 2,788 in both
      sessions). Report: `docs/stage0_report_2026-08-25_paper.md`. Re-run
      after any entitlement change:
      `python -u scripts/verify_ibkr_entitlements.py --port 4002 --compare data/ibkr_universe_live.json`
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
      `\IBKR\IBKR Recorder Health` (hourly; alerts on state changes, not on
      every unhealthy run). Registered by the USER from an
      ELEVATED PowerShell via `scripts/scheduling/register_ibkr_tasks.ps1`.
      (S4U logon type was tried first and produced phantom runs -- do not
      go back to it.)
- [x] `scripts/check_ibkr_recorder.py` health probe -> `monitoring/heartbeat.py`
      (Telegram if ALERT_METHOD configured, else log-only). Alerts on health
      STATE CHANGES (the recorder/sync status pair changes: outage starts,
      recovers, changes kind), one reminder per 24 h of an unchanged outage,
      and a retry on every run after a failed send until one gets through (a
      lost recovery message included) -- not on every unhealthy run
      (2026-10-03; state in `logs/ibkr_health_alert_state.json`, every send
      attempt logged as ALERT_SENT / ALERT_FAILED in `logs/ibkr_health.log`
      together with its method, `telegram` or `log_only`). `--test-alert`
      sends a labelled test message without touching that state and says which
      method carried it (a `log_only` run says nothing went to Telegram).
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

### Stage 4 -- Fees, carry, settlement  `[x]` (coupon rate awaits a fill)
- [x] EV and Kelly extended with fee_model / carry_model / holding_days
      (bit-identical defaults; Kelly with carry f = p*c/(c-r) - q*c/(1+r-c)).
- [x] Live Polymarket on-chain path retired to `legacy/polymarket_onchain/`.
- [x] Fee/carry/holding-days wired into the executor's re-pricing (net EV +
      fee/carry-aware half-Kelly feed the existing risk checks) -- ef4e31f.
- [ ] Coupon-rate verification -- needs a FILLED paper position and its
      statement. CarryModel refuses unverified rates, so carry is null until
      then (conservative: no carry credit in EV/Kelly). Fee facts ARE
      verified and live in `.env` (IBKR_COMMISSION_PER_CONTRACT=0.01,
      IBKR_FEES_VERIFIED_ON=2026-08-25); the adapter refuses to price
      without them.

### Stage 5 -- Market data & pacing  `[x]`
- [x] `src/venues/ibkr/pacing.py` HistoricalPacer (15s identical / 6 per 2s
      per contract / 60 per 10 min with BID_ASK x2; blocking, logged).
- [x] `src/venues/ibkr/recorder.py`: snapshot sweeps (~13 min per ~2,800
      contracts, delayed data type 4, batches of 50 reqTickers) every 20 min
      -> `~/quant_data/ibkr_ticks/quotes/date=YYYY-MM-DD/*.parquet`
      (date partition + con_id column; per-conid partitioning rejected as
      thousands of tiny files). Heartbeat at `...\state\recorder_state.json`.
      RUNNING under Task Scheduler since 2026-08-17 23:55.
- [x] `src/venues/ibkr/subscriptions.py` SubscriptionManager: line-budget
      accounting, lowest-priority/LRU eviction (raises LineBudgetExceeded,
      never silently drops), subscribe-rate token bucket. Wiring a streaming/
      depth recorder onto it stays OPTIONAL: the Stage-5 probe found only one
      depth level per side, so there is no ladder to stream.
- [x] Historical-bars/depth probe RUN 2026-08-23 (`docs/stage5_probe_findings.md`,
      venue_semantics Q4/Q7): depth = ONE level per side (top-of-book only);
      daily bars unavailable but HOURLY BID_ASK/MIDPOINT history exists back
      to 2026-01-17 -> recorder gaps are backfillable via HistoricalPacer.
- [x] **Gap backfill BUILT 2026-08-25**: `src/venues/ibkr/backfill.py` +
      `scripts/backfill_ibkr_hourly.py`. One paced reqHistoricalData call per
      (contract, tick type) spanning the whole gap; bars land in a SEPARATE
      tree `quotes_hourly\date=*\` under the ticks root (now
      `~/quant_data/ibkr_ticks`, see section 5) because hourly
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
      2026-01-17) needs 7 more chunks of at most 29 days, about 2 h each.
      (The "~75 min" above was an estimate; the run ledger shows 114 min.)
- [x] **Backfill RUN 2026-10-04** (run_id 20261004T172227Z, 111 min): 338
      requests, 0 failures, 231,504 hourly BID_ASK bars over 2026-09-03..10-02
      for 318 of 338 climate-lane contracts (the other 20 returned no bars).
      Two things the August run had hidden: a run's length is set by pacing,
      about 20 s per BID_ASK request; and IB counts a duration in sessions
      that open at 21:16 UTC the evening before, so the first day of both
      runs (2026-07-26, 2026-09-03) holds only its last three bars.
- [x] **Backfill first-day fix 2026-10-04**: a request now reaches one session
      further back and only bars on or after the first gap day are written, so
      the longest gap one request fills is 29 days. `--refetch DATE...`
      fetches a day again although it holds files, and a bar -- identified by
      (contract, instant, tick type) -- is never written twice, which makes
      `--refetch` the way to finish a cut run: every contract is requested
      again over the window (about 2 h for the lane) and only the missing
      bars land. The plan reads the stored bars of the window before anything
      connects (an unreadable file stops it, exit 5), one run at a time per
      store (lock, exit 6), one connection attempt (no Gateway is exit 3, not
      an endless retry), an interrupted or stopped run still records itself
      (`interrupted: true` / `error`, exit 130 / 1), a run with failed
      requests exits 7, and an empty answer for a (contract, tick type) that
      already has bars in the window is recorded as a failure, because a
      timed-out request answers with no bars; the ledger lists the contracts
      without bars and the failed requests. Only unexpired contracts are
      requested, so refetching completes 2026-09-03 for all 318 contracts if
      done before 2026-10-10 (22 of them expire 2026-10-09) and 2026-07-26 for
      288 of its 310 (22 expired 2026-09-10). Neither has been refetched yet.
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

### Stage 7 -- Paper trading  `[x]` LIVE ON PAPER since 2026-08-25
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
      --simulate for local fills, its own DB so the measured book stays clean).
- [x] Dress rehearsal 2026-08-25: 3 intents -> 3 simulated fills, $29.32.
- [x] **FIRST REAL PAPER ORDERS 2026-08-25 19:58 local** (user cleared the
      Gateway's Read-Only API checkbox first). 3/3 accepted and RESTING GTC
      on ForecastEx: `GTM_1226_1.2_YES` 29 @ 0.34, `GTM_1226_1.3_YES`
      41 @ 0.24, `GTM_1226_1.4_YES` 71 @ 0.14 (orderIds 16/18/20, account
      DU0000000, $29.64 committed of a $100 bankroll, $10 position cap).
      **The 365-day live-paper clock starts here.** Nothing is filled yet --
      these are bids below the ask, which is the intended posture.
- [x] First attempt was cancelled on arrival with error 110 (off-tick price
      0.345); fixed by snapping limits DOWN to `Market.tick_size` (ea6bcfc).
- [ ] Re-run `run_ibkr_paper.py` on a cadence, and `--simulate`-free
      `executor.sync()` to catch fills; 12 resolved months + 365 days are
      the go-live gate (`climate/paper_criteria.py`).

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
- [x] **Step 6 -- paper loop + executor**: research-only GTM paper loop
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
  2026-08-25). A process started inside a packaged desktop app gets a
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
# Recorder health (alerts only when the state changes; docs/ibkr_runbook.md section 7)
python scripts/check_ibkr_recorder.py
# Alert-channel test: one labelled message, no health check, alert state untouched
python scripts/check_ibkr_recorder.py --test-alert
# Backfill hourly bars for days the recorder missed (plan first, then run).
# A day that holds files is skipped; --refetch DATE fetches it again without
# doubling stored bars (finish a cut run, complete a short first day).
python scripts/backfill_ibkr_hourly.py --dry-run
python scripts/backfill_ibkr_hourly.py --port 4002 --mode paper
# Paper trading: dress rehearsal, then real paper orders one by one
python scripts/run_ibkr_paper.py --simulate
python scripts/run_ibkr_paper.py --mode approve --top 3
# Climate labels (daily) / first backfill
python scripts/collect_climate_labels.py [--backfill]
# Climate forecasts (needs CDS licences accepted)
python scripts/fetch_climate_forecasts.py --pilot | --hindcast 2000-2025
# GTM EMOS fit + diagnostics. --live refits the operational model under
# ~/quant_data/climate_data (override with CLIMATE_DATA_ROOT -- never
# %LOCALAPPDATA%, see section 3); --root <path> fits another root; with
# neither the fit names the files it would have overwritten and exits 2.
# The operational model is the trend variant: keep --trend on a live refit
# (without it the no-trend fit would replace models/gtm_emos.json).
python scripts/fit_gtm_emos.py --trend --live
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

Pushed on `main` through `062998c` (2026-08-25). The 2026-08-25 run of work,
newest first: `062998c` Polymarket task-cleanup list, `efd31b9` first-orders
facts, `ea6bcfc` tick snapping, `7e73535`/`d07b7f6` backfill run + ledger,
`217f959` paper book DB, `11c8270` scheduler paths, `94e10d0` data roots off
%LOCALAPPDATA% + paper parity, `a87b88d` backfill build. Before that:
`def51b5` Stage-5 probe, `ad58918` Stage 5/8 finish, `ef4e31f` executor
rewiring, `1ff5f52` GTM model, `d976323` Polymarket runtime gate, `597d96f`
backfills, `7f6d532` climate package, `99ace7a` oracles.
Untracked and intentionally NOT part of the migration:
`bist_brief/*`, `archive (1).zip`, `logs/`.

## 7. Open user actions (priority order)

1. **Re-log into the Gateway weekly.** The session drops roughly weekly and
   the recorder then sits in its retry loop; that cost 24 hours of sweeps on
   2026-08-24. Nothing alerts you today (see 4).
2. Read off Client Portal, read-only: the **legal entity name** (for
   venue_semantics Q1) and the **ForecastEx live-feed subscription PRICE**
   (for operating_costs.md -- do NOT subscribe; delayed data is free and is
   what the recorder uses).
3. Once a paper order FILLS, pull the statement so the **coupon rate** can be
   verified (venue_semantics Q5, the last open venue fact). `CarryModel`
   refuses to price with an unverified rate, so until then carry is null.
4. Optional: configure Telegram `ALERT_METHOD` so health alerts leave
   log-only mode.

Done and off this list (2026-08-25): Read-Only API cleared, climate task
registered, retired Polymarket tasks unregistered (`backup_db` kept on
purpose), Stage 0 live + paper gates, CDS licences + backfills, executor
rewiring, hourly-gap backfill, first paper orders.

## 8. What to do next

Nothing is half-built. The useful moves, in order:

1. `python scripts/check_ibkr_recorder.py` -- is collection healthy? (A hand
   run is a real run of the alert state machine: it can send a message and
   updates the alert state. To explore options such as `--max-age-minutes`,
   use scratch `--alert-state-path` / `--health-log-path` files and
   `ALERT_METHOD=log_only`; see `docs/ibkr_runbook.md` section 6.)
2. `python scripts/run_gtm_paper.py` then
   `python scripts/run_ibkr_paper.py --mode approve` -- refresh signals and
   add to the paper book. Orders rest as GTC, so re-running does not cancel
   anything; check for duplicates against `get_open_orders()` first.
3. `python scripts/backfill_ibkr_hourly.py --dry-run` -- if the recorder was
   down, this shows the hole and the real run fills it.
4. Deeper quote history (back to 2026-01-17) is available in 7 more
   `--start/--end` chunks of at most 29 days, about 2 h each, if the model
   ever needs it. Only contracts that have not expired are requested.

## 9. History rewrite and rename (2026-10-02)

The repository was renamed `forecast-model` and its history rewritten once.
The two real IBKR
account identifiers were replaced everywhere by placeholders: `U0000000`
is the live account and `DU0000000` the paper account (the rule that a
DU/DF prefix means paper is unchanged). Every commit hash therefore
changed. Hashes cited in this file and in `docs/migration_report.md` are the
PRE-rewrite hashes;
`docs/history_rewrite_2026-10-02_sha_map.txt` maps each old hash to its new
one. The real account identifiers live only in the operator's local,
untracked `docs/local/` notes.

## 10. Spark research mirror (2026-10)

The research half of the repo also runs on the DGX Spark against a one-way
mirror of `~/quant_data`; the trading runtime, every client id and every
scheduled task stay on the Zenbook. Code reaches the Spark by
`git push spark` from the Zenbook (no GitHub credential is used on the
Spark). Parity is evidenced in the newest `docs/spark_parity_*.md`; the
operator's view is `docs/spark_runbook.md`. The local vLLM model is
reachable through `src/llm/` (config-driven model name) and is fenced out of
the pricing/ordering path by `tests/test_architecture/test_llm_boundary.py`.

## 11. Runtime move to the Spark (2026-10)

**Status: cut over on 2026-10-20; acceptance pending.** The Spark is the runtime
(cutover report `docs/spark_runtime_cutover_2026-10-20.md`); the Zenbook's four
scheduled tasks are disabled and its old live root is kept as
`~/quant_data_zenbook_final` for one rollback week. Wherever sections 0 to 8
say "Task Scheduler" or `~/quant_data` on the Zenbook, read "systemd on the
Spark" and `/srv/quant_data`; the Gateway's daily restart there is at 03:30
Europe/Istanbul, not 23:45, and trading's `.env` names that window
(`IBKR_RESTART_WINDOW_LOCAL=03:15-04:00`). What exists is the build in the
repository: systemd units under `deploy/spark/` (among them the owner rule
that admits only `trading` to the Gateway port), a marker file that tells a
mirror from the live root, a daily backup with a restore check, and the
deploy, pull, mirror and rollback scripts under `scripts/spark/`. What will
exist once the Spark is set up: the spike report
(`docs/spark_runtime_spike_<date>.md`), a Linux user `trading` on the Spark
with a runtime clone and the units installed, and the external heartbeat
checks. The operator's view is `docs/spark_runbook.md` section 7. This
section is rewritten at the cutover and again at acceptance.
