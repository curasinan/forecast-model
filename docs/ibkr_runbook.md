# IBKR recorder runbook

Operational procedures for the ForecastEx quote recorder. Factual entries
follow the claim/evidence convention of `docs/venue_semantics.md`: claim,
evidence, date verified, invalidation condition. Anything sourced from IBKR
documentation rather than our own logs/probes is labeled as such.

Quick facts:

| Item | Value |
| --- | --- |
| Account | `U0000000` (live, U-prefix), API read-only |
| Gateway | IB Gateway on `127.0.0.1:4002` (port does NOT imply mode) |
| Recorder client id | 2 (`data_collector` -- the only sanctioned tick writer) |
| Sweep cadence | every 20 min; one sweep takes ~13 min over ~2,900 contracts |
| Data root | `~/quant_data/ibkr_ticks` (outside OneDrive AND outside AppData -- see MIGRATION_CHECKPOINT section 3) |
| Heartbeat | `~/quant_data/ibkr_ticks/state/recorder_state.json` |
| Recorder log | `logs\ibkr_recorder.log` (structured), `logs\ibkr_recorder_task.log` (task launcher capture) |
| Scheduled tasks | `\IBKR\IBKR Recorder`, `\IBKR\IBKR Recorder Health` |

## 1. What runs where

- `scripts\scheduling\register_ibkr_tasks.ps1` registers two Task Scheduler
  jobs under `\IBKR\` (run it elevated; registration only, nothing starts
  immediately). `unregister_ibkr_tasks.ps1` removes them.
- `\IBKR\IBKR Recorder` runs `scripts\scheduling\run_ibkr_recorder.ps1`,
  which sets `IBKR_PORT=4002` / `IBKR_TRADING_MODE=live` (only if unset;
  scheduled tasks have no .env-loading shell) and launches
  `python -u scripts\record_ibkr_ticks.py --interval-minutes 20`.
- `\IBKR\IBKR Recorder Health` runs `scripts\check_ibkr_recorder.py` hourly
  (5-minute execution cap); it alerts via Telegram, stdout is discarded.
- Parquet layout: `quotes\date=YYYY-MM-DD\quotes-HHMMSSZ.parquet`, one file
  per sweep, UTC date partition, `con_id` as an ordinary column
  (`src/venues/ibkr/recorder.py` module docstring explains the deviation
  from per-contract partitioning).

## 2. Client-id map and the single-writer rule

- Claim: each process role owns exactly one Gateway client id, ids are never
  auto-incremented on collision, and a collision raises
  `ClientIdInUseError` instead of silently taking the next id.

  | Role | clientId | Notes |
  | --- | --- | --- |
  | `live_trading` | 1 | reserved; never connected by this codebase |
  | `data_collector` | 2 | the recorder; the ONLY sanctioned writer of ticks |
  | `research` | 3 | ad-hoc notebooks/probes; read-only |

- Evidence: `src/venues/ibkr/clients.py` (`CLIENT_IDS`, `connect_ib`).
- Date verified: 2026-08-16 (code review; collision path unit-tested with fakes).
- Invalidation: adding a new process role; changing the map.

Rules of thumb:

- Never start a second recorder by hand. If you must run a one-off sweep,
  stop the task first (`Stop-ScheduledTask -TaskPath '\IBKR\' -TaskName 'IBKR Recorder'`).
- Ad-hoc Gateway poking uses role `research` (clientId 3), never 2.
- The Gateway enforces id exclusivity, so even a Task Scheduler double-start
  cannot produce two writers: the second connect fails loudly.

## 3. Durability: how the recorder survives reboots and process death

- `AtStartup` trigger covers reboots; `StartWhenAvailable` covers missed
  triggers (machine asleep at boot-tick time).
- The every-5-minutes repeating trigger plus `MultipleInstances IgnoreNew`
  is the keep-alive: while the recorder runs, each tick is a no-op; when it
  dies, the next tick restarts it within 5 minutes.
- `ExecutionTimeLimit` is 0 (PT0S = unlimited) -- Task Scheduler never kills
  the loop. `RestartCount 3` / `RestartInterval 1 min` retries hard launch
  failures.
- The task principal is S4U ("run whether user is logged on or not",
  no stored password), so the python process does not depend on an
  interactive session. Caveat: password-less or Microsoft-account sign-ins
  sometimes refuse S4U at run time (task `LastTaskResult` `0x80070569` or
  `0x41303`). Fallback: open Task Scheduler, task properties, select
  "Run whether user is logged on or not", supply the Windows password when
  prompted (stored by Windows, not by this repo).
- Limit of the design: IB Gateway itself is a GUI application. The recorder
  process survives headless, but it only collects data while a Gateway that
  someone logged into interactively is up. Gateway durability is section 4;
  full headless is section 8.

## 4. Weekly Gateway re-login procedure

- Claim: with Gateway set to "Auto restart", the session survives the
  nightly restart without re-entering credentials, but IBKR still requires
  a full re-authentication roughly weekly (the restart nearest the weekly
  boundary, typically Sunday, comes up at the login screen and waits for a
  human).
- Evidence: IBKR Gateway documented behavior (Configure > Lock and Exit >
  Auto restart). NOT yet observed in our logs -- auto-restart was enabled
  2026-08-16 after the stall incident; confirm against the first full week
  and update this entry.
- Date verified: -- (vendor-documented only)
- Invalidation: IBKR changes session/authentication policy; switching to
  IBC (section 8).

Procedure (do this Sundays, before US market hours, or when the health
check pages you and the Gateway shows a login screen):

1. At the machine (or via remote desktop), bring up the IB Gateway window.
2. If it shows the login screen: log in (live account), complete the IBKR
   Mobile 2FA prompt on the phone.
3. Confirm the API is back: `Test-NetConnection 127.0.0.1 -Port 4002`
   reports `TcpTestSucceeded : True`.
4. Confirm collection resumed: heartbeat `ts` refreshes within one sweep
   cycle, i.e. within ~35 minutes (section 6 one-liners). Do not restart
   the recorder task for this -- it retries on its own with backoff.

## 5. The nightly restart window (expected disconnects)

- Claim: with auto-restart enabled, the Gateway drops the API for a short
  window around midnight US time each night. During that window the
  recorder logs a burst of retries and then resumes on its own; this is
  expected and needs no action.
- Evidence: recorder reconnect design is verified in code and unit tests
  (`src/venues/ibkr/recorder.py`: `_connect_with_backoff` retries with 1s
  doubling to a 60s cap; a failed sweep triggers `_reconnect`). The nightly
  window itself is vendor-documented; measure the first week's logs for the
  actual local-time window and duration, then update this entry.
- Date verified: -- (design verified 2026-08-16; window timing unmeasured)
- Invalidation: Gateway restart time reconfigured; IBC adoption.

What it looks like in `logs\ibkr_recorder.log`:

```
... WARNING ... sweep failed (...); reconnecting
... WARNING ... connect failed (ConnectionRefusedError ...); retrying in 1s
... WARNING ... connect failed (...); retrying in 2s
...                                              ... retrying in 60s
... WARNING ... ibkr session: account=U0000000 mode=live role=data_collector read_only=True
... INFO    ... sweep done: rows=...
```

Reading the burst:

- A few minutes of `retrying in Ns` around midnight US time, then a clean
  reconnect: expected=true. The health check should not page for this
  (hourly cadence plus heartbeat threshold rides over a short window).
- The same lines repeating every ~60s for HOURS: not a restart window.
  That is the failure mode of 2026-08-16 (below) or a stuck login screen.
  Go to section 7 triage.

The stall incident that motivated all of this:

- Claim: with the default nightly auto-LOGOFF (not auto-restart), the
  Gateway exits at the cutoff and nothing brings the API back; the recorder
  retried every ~60s for ~15 hours (connection refused), heartbeat frozen,
  zero parquet written in the window. Collection silently stalled.
- Evidence: recorder log + heartbeat timestamps, night of 2026-08-15 to
  2026-08-16.
- Date verified: 2026-08-16
- Invalidation: fixed by switching Gateway to Auto restart; would recur if
  that setting is ever reset (e.g. Gateway reinstall/update).

## 6. Checking data freshness

Three layers, cheapest first:

1. Heartbeat: `recorder_state.json` holds `{ts, rows, sweep_seconds,
   universe_size}`, rewritten atomically after every sweep
   (`src/venues/ibkr/recorder.py: write_state`). Healthy numbers:
   `ts` at most ~20-25 min old, `rows` close to `universe_size` (~2,900),
   `sweep_seconds` ~780. `ts` older than ~45 min means sweeps stopped.
2. Parquet: today's UTC `date=` directory should gain roughly 3 files per
   hour (one per 20-min sweep).
3. `python scripts\check_ibkr_recorder.py` -- the same check the hourly
   task runs; run it by hand to see exactly what the alerting sees.

One-liners (from the repo root):

```powershell
# Task status: last/next run and result codes (0 = success)
Get-ScheduledTask -TaskPath '\IBKR\' | Get-ScheduledTaskInfo |
    Format-Table TaskName, LastRunTime, LastTaskResult, NextRunTime

# Last 20 recorder log lines (structured log)
Get-Content logs\ibkr_recorder.log -Tail 20

# Task launcher capture (catches crashes that happen before logging starts)
Get-Content logs\ibkr_recorder_task.log -Tail 20

# Heartbeat contents + age
$s = Get-Content "$env:USERPROFILE\quant_data\ibkr_ticks\state\recorder_state.json" -Raw | ConvertFrom-Json
ecorder_state.json" -Raw | ConvertFrom-Json
$s
[DateTime]::UtcNow - [DateTime]::Parse($s.ts).ToUniversalTime()

# Today's (UTC date) row count via pyarrow
python -c "import os,datetime,pyarrow.dataset as ds; day=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d'); p=os.path.join(os.path.expanduser('~'),'quant_data','ibkr_ticks','quotes','date='+day); print(p); print(ds.dataset(p).count_rows() if os.path.isdir(p) else 'NO DATA for '+day)"

# Is the Gateway API port up?
Test-NetConnection 127.0.0.1 -Port 4002
```

## 7. Telegram STALE / DOWN alerts: what they mean, triage order

The hourly health task (`scripts\check_ibkr_recorder.py`) reads the
heartbeat and alerts via Telegram. Semantics (see the script for exact
thresholds):

- STALE: the heartbeat file exists but `ts` is older than the threshold.
  The recorder process may be alive but not completing sweeps (Gateway
  down/logged out, or sweeps erroring), or the process died between
  keep-alive ticks and has not caught up yet.
- DOWN: no readable heartbeat at all, or the check itself cannot run --
  collection is definitely not happening.

Triage in this order; each layer depends on the one above it, so fix the
first broken layer and re-check before touching the next:

1. Gateway process up? Look for the IB Gateway window / javaw process on
   the desktop session. Not running: start it, log in, done.
2. Logged in? Gateway showing the login screen means the weekly
   re-authentication is due (section 4) or auto-restart came up
   unauthenticated. Log in with IBKR Mobile 2FA.
3. Port listening? `Test-NetConnection 127.0.0.1 -Port 4002`. Gateway up
   and logged in but port closed: check Gateway API settings (socket port
   4002, API enabled), then restart the Gateway.
4. Recorder task running?
   `(Get-ScheduledTask -TaskPath '\IBKR\' -TaskName 'IBKR Recorder').State`
   should be `Running`. If `Ready` (i.e. not running), the keep-alive tick
   restarts it within 5 minutes -- or force it:
   `Start-ScheduledTask -TaskPath '\IBKR\' -TaskName 'IBKR Recorder'`.
   Then tail both logs (section 6) and watch for `sweep done`.

If all four layers look healthy but STALE persists, read the last
exceptions in `logs\ibkr_recorder.log` and check `sweep_seconds` in the
heartbeat -- a sweep time creeping toward the 20-min interval means the
universe grew or the Gateway is pacing us.

## 8. Future option: fully headless Gateway (IBC)

IBC (IBC-Alpha) automates Gateway start, login, and the restart clicks,
which would remove the weekly manual step and the interactive-session
dependency entirely.

Deliberate caveat, which is why this repo does NOT configure it:

- IBC stores the Gateway username and password in a local plaintext config
  file (`config.ini`). That is a real credential-at-rest tradeoff on a
  desktop machine.
- Two-factor auth still exists: it needs an IBKR-Mobile-2FA arrangement
  (e.g. the seamless-reauthentication weekend flow, or a dedicated
  secondary username with different 2FA settings) that the account holder
  must set up with IBKR.

Adopting IBC is a user decision to make explicitly, outside this repo. If
adopted: keep the API read-only setting on in the Gateway, keep the
client-id map unchanged, and re-verify section 4/5 claims since the
restart/login behavior changes.

## 9. Registration maintenance

```powershell
# (Re)register both tasks -- idempotent, replaces existing definitions
.\scripts\scheduling\register_ibkr_tasks.ps1

# Remove both tasks (stops a running recorder first)
.\scripts\scheduling\unregister_ibkr_tasks.ps1
```

After any re-registration, confirm with the task-status one-liner in
section 6 and expect the recorder to be `Running` within 5 minutes.

## 10. Monthly rituals

Three checks belong in the same monthly sitting because the first one has a
hard deadline: `.env` currently holds `IBKR_COMMISSION_PER_CONTRACT=0.01`
and `IBKR_FEES_VERIFIED_ON=2026-08-25` (as of this writing, 2026-08-27), and
`FlatCommissionFeeModel`'s 30-day staleness window (section below) turns
that into a hard failure on **2026-09-24** if nobody re-verifies first. Do
all three before the 20th of each month so there is buffer before the
cliff.

| Ritual | Cadence | First due |
| --- | --- | --- |
| Fee re-verify + `.env` date bump | Monthly, before the 20th | **2026-09-20** |
| Universe snapshot refresh | Monthly, same sitting | 2026-09-20 |
| Alert-channel test | Monthly, same sitting | 2026-09-20 |

### 10.1 Fee re-verify + `.env` date bump

- Claim: `FlatCommissionFeeModel.__init__` raises `StaleFeeError` once
  `verified_on` is more than `max_age_days` (30) days old
  (`src/domain/fees.py:61-67`). `IBKRAdapter.fee_model()` builds exactly
  this model from `IBKRSettings.commission_per_contract` /
  `fees_verified_on` (`src/venues/ibkr/adapter.py:332-343`), which in turn
  are populated from `.env`'s `IBKR_COMMISSION_PER_CONTRACT` /
  `IBKR_FEES_VERIFIED_ON` via pydantic-settings'
  `env_file=".env"` (`src/venues/ibkr/settings.py:28,36-37`).
  `TradingExecutor._evaluate` calls `fee_model()` **uncaught**, once per
  intent, inside `execute_intents`'s loop
  (`src/trading/executor.py:204-208,263`), and
  `scripts/run_ibkr_paper.py`'s only exception handler around that call
  catches `LockHeldError`, not `StaleFeeError`
  (`scripts/run_ibkr_paper.py:197-224`). Net effect: a stale fee model
  crashes the whole `run_ibkr_paper.py` invocation with an uncaught
  `StaleFeeError` before a single intent is evaluated -- not a partial run,
  a hard refusal.
- Evidence: as cited above (code review).
- Date verified: 2026-08-27 (this section's authoring).
- Invalidation: any of the cited files changing shape (an exception handler
  added around `fee_model()`/`execute_intents`, or `max_age_days` changed
  from 30).

How to re-verify, in order of preference:

1. **Preferred once a paper fill exists:** read the commission off the
   fill's account statement. Also record the observed value in
   `docs/venue_semantics.md`'s "Additional verified facts" section (not
   just `.env` -- that section is the durable evidence trail; see the
   existing $0.01 entry there for the format,
   `docs/venue_semantics.md:199-206`).
2. **Otherwise, re-run the whatIf probe** with the paper Gateway
   logged in:

   ```powershell
   python scripts/verify_ibkr_entitlements.py --port 4002
   ```

   Read the commission off the `LMT DAY` (or any `ACCEPTED`) line in
   section 5 of its report, e.g. `commission=0.01
   commissionCurrency='USD'` (`scripts/verify_ibkr_entitlements.py:506-511`
   is where that line is built).

Then update **both** `.env` values:

- `IBKR_COMMISSION_PER_CONTRACT=<observed>` (only if it changed).
- `IBKR_FEES_VERIFIED_ON=<today>` (always -- this is what resets the
  30-day clock).

The value has been `0.01` at every probe since 2026-08-16
(`docs/venue_semantics.md:199-206`, `docs/venue_semantics.md:225-237`). A
**changed** value is itself an alert-worthy finding, not routine drift: do
not silently bump the date and move on -- record the discrepancy (old
value, new value, which probe/statement) in `docs/venue_semantics.md`
before updating `.env`, since every net-EV and Kelly calculation downstream
prices off this one number.

### 10.2 Universe snapshot refresh

- Claim: `IBKRRecorder.universe()` seeds the tradable contract cache only
  from `data/ibkr_universe_live.json`, and only when the cache is empty
  (`src/venues/ibkr/recorder.py:183-201`); there is no live re-discovery of
  new listings yet, so a new expiry or a new GTM contract is invisible to
  the recorder until the snapshot is refreshed
  (`src/venues/ibkr/recorder.py:24-28`). Once the snapshot file's mtime
  exceeds `SNAPSHOT_STALE_DAYS` (35, `src/venues/ibkr/recorder.py:58`), the
  next seed logs a WARNING naming the refresh command
  (`src/venues/ibkr/recorder.py:193-196`, `SNAPSHOT_REFRESH_CMD` defined at
  `src/venues/ibkr/recorder.py:59-62`). The health probe surfaces the same
  age, informationally, as `snapshot_age_days=` on its OK message
  (`scripts/check_ibkr_recorder.py:201-214,295-303`) -- it is never a
  failure axis, so nothing pages for this; the operator has to notice it.
- Evidence: as cited above (code review).
- Date verified: 2026-08-27.
- Invalidation: Stage 5 live re-discovery landing (flagged as a known
  future change in `src/venues/ibkr/recorder.py:24-28`) would remove the
  snapshot dependency this ritual exists for.

How:

```powershell
python scripts/verify_ibkr_entitlements.py --port 4002 --snapshot data/ibkr_universe_live.json
git diff data/ibkr_universe_live.json
```

Review the diff -- expect additions (new listings, new GTM expiries) and
removals (contracts that rolled off; the 2026-08-25 paper/live parity check
found 112 of 140 differences were plain expiries between two snapshots
9 days apart, `docs/venue_semantics.md:225-237`, so a busy-looking diff is
normal, not a red flag by itself). Commit it with a descriptive message,
e.g. `Refresh the IBKR universe snapshot (monthly ritual)`.

### 10.3 Alert-channel test

How:

```powershell
python scripts/check_ibkr_recorder.py --state-path C:\nonexistent.json
```

This hits the "state file missing" branch
(`scripts/check_ibkr_recorder.py:236-240`), which returns `CRITICAL`. Any
non-`OK` status triggers both alert channels: the durable log sink
(`_append_health_log`, appends to `logs\ibkr_health.log` by default --
`scripts/check_ibkr_recorder.py:79,397-416`) and `send_alert`
(`monitoring/heartbeat.py:79-113`, imported at
`scripts/check_ibkr_recorder.py:74`). If Telegram alerting has not yet been
configured (see `MIGRATION_CHECKPOINT.md` open action #4), `ALERT_METHOD`
is unset and `send_alert` silently degrades to `log_only`
(`monitoring/heartbeat.py:83,112-113`) -- only the `logs\ibkr_health.log`
line will appear on this first pass, which is expected, not a failure; set
the three HKCU vars below first, then re-run the test and expect both.
Otherwise expect **both**: a new line in `logs\ibkr_health.log`, and a
Telegram message.

If neither arrives, re-check the user-level environment variables
`ALERT_METHOD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` -- set with
`[Environment]::SetEnvironmentVariable('<NAME>', '<value>', 'User')` so
they land in HKCU and are visible to Task-Scheduler-launched processes
without any shell that sources `.env`.

**These three must NEVER go in `.env`.** `send_alert` reads `os.environ`
directly (`monitoring/heartbeat.py:83,86-87`) and, unlike `IBKRSettings`
(which loads `.env` via pydantic-settings' `env_file=".env"`,
`src/venues/ibkr/settings.py:28`), neither `monitoring/heartbeat.py` nor
`scripts/check_ibkr_recorder.py` ever reads `.env` at all -- putting the
alert vars there would silently do nothing; they have to be real process
environment variables. And never paste the bot token into logs, docs, or
chat: `send_alert` itself is careful never to log it
(`monitoring/heartbeat.py:103-107`, "Never log the token; just the
status.") -- follow the same rule everywhere else.
