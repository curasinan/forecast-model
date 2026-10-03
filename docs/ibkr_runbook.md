# IBKR recorder runbook

Operational procedures for the ForecastEx quote recorder. Factual entries
follow the claim/evidence convention of `docs/venue_semantics.md`: claim,
evidence, date verified, invalidation condition. Anything sourced from IBKR
documentation rather than our own logs/probes is labeled as such.

Quick facts:

| Item | Value |
| --- | --- |
| Account | **Paper session** (paper-trading since 2026-08-25). Gateway login uses the live username with the **Paper Trading** toggle selected -- a separate paper username is not needed. Connected account is `DU0000000` (DU-prefix; mode is inferred from the account prefix at connect, `src/venues/ibkr/adapter.py:119`). The live account `U0000000` (U-prefix) exists but is never the session the recorder/paper-trading scripts connect to. |
| Read-Only API | **Cleared** in Gateway -> Configure -> Settings -> API -> Settings on 2026-08-25 so paper orders can transmit (`docs/venue_semantics.md:248-253`, `docs/MIGRATION_CHECKPOINT.md:187-188`). Note `readonly=True` on the ib_async connect call does NOT by itself gate order transmission -- the real gates are `BaseVenue.place_order`'s `read_only` check (`src/venues/base.py:87-92`) and the adapter's paper-account check (`src/venues/ibkr/adapter.py:278-283`); see `src/venues/ibkr/clients.py` module docstring for why. |
| Gateway | IB Gateway on `127.0.0.1:4002` (port does NOT imply mode) |
| Recorder client id | 2 (`data_collector` -- the only sanctioned tick writer) |
| Sweep cadence | every 20 min; one sweep takes ~13 min over ~2,900 contracts |
| Data root | `~/quant_data/ibkr_ticks` (outside OneDrive AND outside AppData -- see MIGRATION_CHECKPOINT section 3) |
| Heartbeat | `~/quant_data/ibkr_ticks/state/recorder_state.json` |
| Recorder log | `logs\ibkr_recorder.log` (structured), `logs\ibkr_recorder_task.log` (task launcher capture) |
| Scheduled tasks | `\IBKR\IBKR Recorder` (logon + 5-min keep-alive, clientId 2), `\IBKR\IBKR Recorder Health` (hourly via `run_ibkr_health.ps1`; alerts on health state changes, not on every unhealthy run -- section 7), `\IBKR\IBKR Paper Sync` (daily 17:30 via `run_ibkr_sync.ps1`, clientId 4) -- all three registered by `register_ibkr_tasks.ps1` (section 1). Elsewhere on this machine, outside `\IBKR\` and outside this runbook's scope: `\Climate\Climate Labels` (daily 16:30) and `\Polymarket\backup_db` (daily 21:00, kept deliberately even though the Polymarket runtime itself is gated off). |

## 1. What runs where

- `scripts\scheduling\register_ibkr_tasks.ps1` registers three Task
  Scheduler jobs under `\IBKR\` (run it elevated; registration only, nothing
  starts immediately, replaces any existing registration by the same name).
  `unregister_ibkr_tasks.ps1` removes them.
- `\IBKR\IBKR Recorder` runs `scripts\scheduling\run_ibkr_recorder.ps1`,
  which sets `IBKR_PORT=4002` / `IBKR_TRADING_MODE=paper` (only if unset;
  scheduled tasks have no .env-loading shell) and launches
  `python -u scripts\record_ibkr_ticks.py --interval-minutes 20`
  (`scripts\scheduling\run_ibkr_recorder.ps1:21-22,50`). Paper shares the
  live account's market-data subscriptions, so the quotes are the same feed
  either way.
- `\IBKR\IBKR Recorder Health` runs `scripts\scheduling\run_ibkr_health.ps1`
  hourly (5-minute execution cap on the task; the launcher itself has no
  IBKR_PORT/IBKR_TRADING_MODE to set because the probe is Gateway-free --
  filesystem only). Re-registered onto this launcher 2026-09-28 (previously
  a bare `python.exe` action with no log-rotation/launch-marker wrapper).
  The launcher tees `python -u
  scripts\check_ibkr_recorder.py` to `logs\ibkr_health_task.log`; the probe
  alerts via Telegram when the health state CHANGES (plus one reminder per
  24 h of an unchanged outage, and a retry after a failed send) -- not on
  every unhealthy run -- and appends its own durable lines to
  `logs\ibkr_health.log` (section 7, section 10.3).
- `\IBKR\IBKR Paper Sync` runs `scripts\scheduling\run_ibkr_sync.ps1` daily
  at 17:30, sets `IBKR_PORT=4002` / `IBKR_TRADING_MODE=paper` (only if
  unset) and launches `python -u scripts\sync_ibkr_paper.py`, connecting as
  role `paper_sync` / clientId 4, read-only. It asks the venue what
  happened to every resting GTC order and reconciles the outcome into
  `ibkr_paper.db` -- nothing that places an order stays alive long enough to
  see a late fill land, so this daily job is what closes that gap
  (`scripts\sync_ibkr_paper.py` module docstring). Output goes to
  `logs\ibkr_sync_task.log`.
- Parquet layout: `quotes\date=YYYY-MM-DD\quotes-HHMMSSZ.parquet`, one file
  per sweep, UTC date partition, `con_id` as an ordinary column
  (`src/venues/ibkr/recorder.py` module docstring explains the deviation
  from per-contract partitioning).

## 2. Client-id map and the single-writer rule

- Claim: each process role owns exactly one Gateway client id, ids are never
  auto-incremented on collision, and a collision raises
  `ClientIdInUseError` instead of silently taking the next id.

  | Role | clientId | Machine | Notes |
  | --- | --- | --- | --- |
  | `live_trading` | 1 | Zenbook | order-submission role. `scripts\run_ibkr_paper.py` connects as this (any run NOT using `--simulate`), `read_only=False`, then refuses to proceed unless the connected account is paper (`scripts/run_ibkr_paper.py:156-163`). Despite the name, this is the role that has placed every real order so far -- all of them paper. |
  | `data_collector` | 2 | Zenbook | the recorder; the ONLY sanctioned writer of ticks |
  | `research` | 3 | Zenbook | ad-hoc notebooks/probes; read-only. Also `scripts/verify_ibkr_entitlements.py`'s `--client-id` default (`scripts/verify_ibkr_entitlements.py:594`). |
  | `paper_sync` | 4 | Zenbook | `\IBKR\IBKR Paper Sync` (`scripts\sync_ibkr_paper.py`), read-only; daily reconciliation of resting GTC orders (section 1). |

  The DGX Spark holds no client id (docs/spark_runbook.md section 1).

- Evidence: `src/venues/ibkr/clients.py` (`CLIENT_IDS`, `connect_ib`).
- Date verified: 2026-08-16 (code review; collision path unit-tested with fakes); `paper_sync` role added since (Task 1.6/2.2).
- Invalidation: adding a new process role; changing the map.

Rules of thumb:

- Never start a second recorder by hand. If you must run a one-off sweep,
  stop the task first (`Stop-ScheduledTask -TaskPath '\IBKR\' -TaskName 'IBKR Recorder'`).
- Ad-hoc Gateway poking uses role `research` (clientId 3), never 2.
- The Gateway enforces id exclusivity, so even a Task Scheduler double-start
  cannot produce two writers: the second connect fails loudly.

## 3. Durability: how the recorder survives reboots and process death

- Claim: the registered principal is **Interactive** (`-LogonType
  Interactive -RunLevel Limited`, running as the operator's own Windows
  account), not S4U ("run whether user is logged on or not"). S4U was tried
  first and empirically never executed the action on this machine: instant
  `0x0` "runs" with zero side effects and a stuck `Queued` state, while the
  same launcher ran fine outside the scheduler -- isolating the failure to
  the S4U task context. This is not a durability loss in practice: IB
  Gateway is itself a GUI application that only exists inside a logged-on
  session, so a recorder that could run without a login would still have
  nothing to connect to.
- Evidence: `scripts\scheduling\register_ibkr_tasks.ps1:33-43` (the
  `$principal` definition) and its preceding comment block, which records
  the S4U failure and the 2026-08-17 verification date.
- Date verified: 2026-08-17 (S4U dead end found and replaced).
- Invalidation: switching back to S4U; adopting IBC's own process model
  (section 8), which would change this tradeoff.

- `AtLogOn` starts the recorder when the operator's session logs on;
  a 5-minute repeating trigger (seeded at registration time) is the
  keep-alive, with `StartWhenAvailable` covering a tick missed while the
  machine was asleep (`register_ibkr_tasks.ps1:78-85`). There is no
  `AtStartup` trigger -- a reboot alone does not start the recorder, only
  the next interactive logon does, which follows directly from the
  Interactive principal above.
- The every-5-minutes repeating trigger plus `MultipleInstances IgnoreNew`
  is the keep-alive: while the recorder runs, each tick is a no-op; when it
  dies, the next tick restarts it within 5 minutes.
- `ExecutionTimeLimit` is set to 3650 days, not `[TimeSpan]::Zero` /
  `PT0S`: `PT0S` was observed on this machine to mean "stop immediately"
  (instant `0x0` runs, 2026-08-17), not "unlimited"
  (`register_ibkr_tasks.ps1:87-98`). `RestartCount 3` / `RestartInterval
  1 min` retries hard launch failures.
- Limit of the design: because the principal is Interactive, both the
  recorder task and the Gateway it depends on require an interactive logon
  session to exist at all -- there is no reboot-survives-without-login
  story here by design, and that is an accepted tradeoff, not a bug.
  Gateway durability across its own restarts/re-logins is section 4; full
  headless (which would revisit the Interactive-vs-S4U choice) is
  section 8.

## 4. Weekly Gateway re-login procedure

- Claim: with Gateway set to "Auto restart", the session survives the
  nightly restart without re-entering credentials, but IBKR still requires
  a full re-authentication roughly weekly (the restart nearest the weekly
  boundary comes up at the login screen and waits for a human).
- Evidence: IBKR Gateway documented behavior (Configure > Lock and Exit >
  Auto restart), plus one real occurrence: exercised end-to-end on
  2026-09-28, ending a Gateway outage that had run since the previous
  manual login -- materially longer than "roughly weekly," most likely
  because nothing paged for it until Telegram alerting was configured that
  same day (section 10.3). The step-by-step procedure below (username,
  Paper toggle, disclaimer popup, the error-10197 false alarm) reflects
  what that 2026-09-28 re-login actually involved, not just the vendor
  description.
- Date verified: 2026-09-28 (first real re-login exercised end to end; the
  vendor's "roughly weekly" cadence is still otherwise unconfirmed against
  our own logs -- one data point is not a cadence).
- Invalidation: IBKR changes session/authentication policy; switching to
  IBC (section 8).

Procedure (when the health check pages you, or the Gateway shows a login
screen):

1. At the machine (or via remote desktop), bring up the IB Gateway window.
2. Log in with the **live username**, with the **Paper Trading** toggle on
   the login screen selected -- there is no separate paper username to
   remember (`docs/MIGRATION_CHECKPOINT.md:177-181`). Complete the IBKR
   Mobile 2FA prompt on the phone.
3. **Accept the paper-trading disclaimer popup before any API client
   connects.** If the recorder's keep-alive tick, the health check, or an
   ad-hoc probe tries to connect while that popup is still open, the
   attempt is rejected as a half-open handshake that briefly looks like a
   competing session. The observable symptom (2026-09-28) is a burst of
   API error 10197 right after login -- this is a documented IBKR false
   positive that self-heals once the disclaimer is dismissed and the
   session settles. Do not go hunting for a phantom competing session over
   a brief 10197 burst; only treat it as a real problem if it persists well
   past login (then go to section 7 triage).
4. Confirm the API is back: `Test-NetConnection 127.0.0.1 -Port 4002`
   reports `TcpTestSucceeded : True`.
5. Confirm collection resumed: heartbeat `ts` refreshes within one sweep
   cycle, i.e. within ~35 minutes (section 6 one-liners). Do not restart
   the recorder task for this -- it reconnects on its own with backoff
   (`GatewaySession`, section 5).

## 5. The nightly restart window (expected disconnects)

- Claim: with auto-restart enabled, the Gateway drops the API for a short
  window around midnight US-Eastern time each night, which on this
  machine's local clock is configured as `23:30-00:15`
  (`IBKR_RESTART_WINDOW_LOCAL` in `.env`). During that window the recorder
  logs a burst of retries and then resumes on its own; this is expected and
  needs no action.
- Evidence: recorder reconnect design is verified in code and unit tests
  (`src/venues/ibkr/gateway.py`: `GatewaySession._reconnect_loop` retries
  with 1s doubling to a 60s cap plus jitter; `ensure_connected` drives the
  retry from an active `reqCurrentTime()` heartbeat, not bare socket
  liveness). The window itself comes from `IBKR_RESTART_WINDOW_LOCAL`
  (`src/venues/ibkr/settings.py:43-49`, parsed by
  `parse_restart_window`/`in_restart_window` in the same `gateway.py`
  module) -- events inside it are tagged `expected=True`
  (`ConnectionEvent.expected`) so monitoring can separate planned restarts
  from real outages; `.env` currently sets it to `23:30-00:15` local
  (`docs/MIGRATION_CHECKPOINT.md:287-288`), a measured value, not the
  settings default.
- Date verified: 2026-08-16 (reconnect design); `IBKR_RESTART_WINDOW_LOCAL`
  measured and set 2026-08-25 per MIGRATION_CHECKPOINT.
- Invalidation: Gateway restart time reconfigured (update
  `IBKR_RESTART_WINDOW_LOCAL`); IBC adoption.

What it looks like in `logs\ibkr_recorder.log`:

```
... WARNING ... sweep failed (...); reconnecting
... WARNING ... connect failed (ConnectionRefusedError ...); retrying in 1s
... WARNING ... connect failed (...); retrying in 2s
...                                              ... retrying in 60s
... WARNING ... ibkr session: account=DU0000000 mode=paper role=data_collector read_only=True
... INFO    ... sweep done: rows=...
```

Reading the burst:

- A few minutes of `retrying in Ns` inside the `23:30-00:15` local window,
  then a clean reconnect: expected=true. The health check should not page
  for this (hourly cadence plus heartbeat threshold rides over a short
  window).
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
   task runs; run it by hand to see exactly what the alerting sees. A hand
   run is a REAL run of the alert state machine (section 7): it can send a
   transition or recovery message and it updates the alert state, so the next
   hourly run stays quiet about it. Run it from a shell that has the three
   alert variables (section 10.3; HKCU variables reach only shells opened
   after they were set): without them the alert degrades to `log_only`, which
   counts as delivered (its line was written), and the Telegram message that
   run would have sent is never sent -- the `ALERT_SENT ... log_only` line it
   leaves in `logs\ibkr_health.log` is how you can tell. To test the alert
   channel use `--test-alert` instead.

   Do not explore options on the live state: a hand run with a non-default
   threshold can disagree with the hourly task. `--max-age-minutes 90` during
   a 70-minute STALE outage sees OK, sends a false `[STALE -> OK] recovered`,
   and the next hourly run sends `[OK -> STALE]` with the outage clock reset.
   For exploratory runs, in a throwaway shell, point the run at scratch files
   and the log_only method, so that it neither drives the live state nor
   messages the phone (a fresh state file would announce the current outage
   as `[OK -> ...]`):

   ```powershell
   $env:ALERT_METHOD = 'log_only'
   python scripts\check_ibkr_recorder.py --max-age-minutes 90 `
       --alert-state-path $env:TEMP\scratch_state.json `
       --health-log-path $env:TEMP\scratch_health.log
   ```

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

**When a message is sent (since 2026-10-03).** The probe used to message you
on every unhealthy run -- about 15 identical DOWN messages a day during the
2026-09-29..10-03 outage -- and said nothing on recovery. It now alerts on
state changes. The state is the pair (recorder status, sync status), kept in
`logs\ibkr_health_alert_state.json` (statuses, UTC times and one flag; never a
token or chat id). Each message starts with a short prefix:

| Prefix | Meaning |
| --- | --- |
| `[OK -> DOWN]`, `[DOWN+SYNC_STALE -> SYNC_STALE]` | the pair changed: an outage began or changed kind |
| `[DOWN -> OK] recovered after 4d 12h:` | the pair is all-OK again (the recovery message; the duration is the outage length, and a retry of a lost one says exactly the same) |
| `[still DOWN, 2d 3h]` | unchanged and still unhealthy, sent either as the daily reminder (24 h after the last DELIVERED message) or as the retry after a send that failed; the duration runs from the start of the outage |

An unchanged healthy run sends nothing -- except after a lost message. A
failed send is retried on every hourly run until one gets through, whatever
state the probe is in by then (the network is often down exactly during an
outage, and still coming back when it ends): a lost *recovery* message is sent
again with the same `[DOWN -> OK] recovered after ...` text (if the system
fails again before it gets through, the new `[OK -> ...]` alert replaces it).
A channel that stays broken therefore leaves one `ALERT_FAILED` line per run
in the log below. The state is saved before each send (marked "not
delivered") and again after it, so one failed write -- a Defender or OneDrive
lock -- or a kill between the send and the save costs at most a duplicate
message and cannot swallow the next alert. A missing or corrupt state file
reads as "healthy, nothing alerted yet": an unhealthy run alerts again (so
deleting the file, or the first run after this change, re-announces an
ongoing outage once as `[OK -> ...]`), and a healthy run stays silent (so a
lost file also drops the recovery message of an outage that was open when it
was lost).

**If the same message arrives every hour**, and `logs\ibkr_health_task.log`
shows `alert state write failed (PermissionError)` twice per run, the alert
state file can be read but not replaced (a handle held by an antivirus scan or
the OneDrive client, or a read-only flag), so the stored pair stops advancing.
Measured here with a handle held open on the file: every healthy run after an
outage re-sent `[STALE -> OK] recovered ...` (4 runs, 4 messages), and one
more when the handle was released. The save before each send covers ONE
failed write; a lock that lasts through both saves of a transition run still
leaves the old pair on disk, so if the outage has ended by the next run its
recovery message is not sent (and if it is still on, the transition is
announced a second time). Release the lock or clear the read-only flag.
Deleting the file is not a way around it: while the file is locked or
read-only the delete fails for the same reason the replace does (once the
file really is gone, an ongoing outage is re-announced once as `[OK -> ...]`).

`logs\ibkr_health.log` keeps one status line per unhealthy run
(`<utc> <STATUS> <message>`) and now also one line per send attempt:
`<utc> ALERT_SENT <kind> <STATUS> <method>` or
`<utc> ALERT_FAILED <kind> <STATUS> <method> <reason>`, with kind `transition`,
`reminder`, `retry` or `test`, method `telegram` or `log_only`, and reason a
fixed phrase or an exception class name (never exception text, which can quote
the bot token). A recovery appends only its ALERT line. A `log_only` line means
the text went to the log (stderr), not to Telegram: the probe counts such a
run as delivered, but nothing reached the phone. So a successful send is no
longer invisible, and `Select-String ALERT_ logs\ibkr_health.log` shows every
message the probe tried to send and which way it went.

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
hard deadline. **The authoritative source for whether this is current is
the live `.env` value, not this paragraph or the table below** -- this
section is rewritten only when someone happens to touch it, so read
`IBKR_FEES_VERIFIED_ON` directly out of `.env` before trusting any date
written here. As of this writing (2026-09-28, Date verified: 2026-09-28),
`.env` holds `IBKR_COMMISSION_PER_CONTRACT=0.01` and
`IBKR_FEES_VERIFIED_ON=2026-09-28` (that month's ritual completed that day;
commission re-verified unchanged at 0.01), and `FlatCommissionFeeModel`'s
30-day staleness window (section below) turns that into a hard failure
around **2026-10-28** if nobody re-verifies first. Do all three before the
20th of each month so there is buffer before the cliff.

| Ritual | Cadence | Next due |
| --- | --- | --- |
| Fee re-verify + `.env` date bump | Monthly, before the 20th | **2026-10-20** |
| Universe snapshot refresh | Monthly, same sitting | 2026-10-20 |
| Alert-channel test | Monthly, same sitting | 2026-10-20 |

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
(`docs/venue_semantics.md:199-206`, `docs/venue_semantics.md:225-237`),
including the 2026-09-28 monthly re-verify (unchanged; not yet logged in
`docs/venue_semantics.md` as of this writing -- do that when next touching
this ritual). A
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
  (`scripts/check_ibkr_recorder.py:273-286,367-375`) -- it is never a
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

Status: `ALERT_METHOD` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` have been
set in HKCU on this machine since 2026-09-28, and this test reached the
phone that day. That is a dated fact about this one machine, not a standing
guarantee -- the unset/log-only path described below still applies verbatim
on any other machine, or here again if these vars are ever cleared (account
migration, OS reinstall, HKCU wiped). Re-run the check below any time you
need to confirm the CURRENT state rather than trusting this note.

How:

```powershell
python scripts/check_ibkr_recorder.py --test-alert
```

This sends one clearly labelled message (`TEST ibkr health alert, sent <utc>
-- no action needed`) through the same `send_alert` path a real alert takes
(`_run_test_alert` in `scripts/check_ibkr_recorder.py`, which calls
`send_alert` in `monitoring/heartbeat.py`), appends `ALERT_SENT test - telegram`
(or `ALERT_FAILED test - telegram <reason>`) to `logs\ibkr_health.log`, prints
`test alert delivered via telegram` or `test alert NOT delivered via telegram
(<reason>)`, and exits 0 or 1. It runs no health check and never reads or
writes `logs\ibkr_health_alert_state.json`, so it is safe at any time,
mid-outage included: no phantom transition, no reset reminder clock. When the
three vars are set, expect **both**: a new `ALERT_SENT test - telegram` line in
`logs\ibkr_health.log`, and a Telegram message. If `ALERT_METHOD` is unset (a
fresh machine, or these vars cleared), `send_alert` degrades to `log_only` and
the test says so: it prints `test alert delivered via log_only -- nothing was
sent to Telegram (ALERT_METHOD is not telegram)`, logs `ALERT_SENT test -
log_only` and still exits 0 (the configured path worked: its "delivery" is the
log line, which appears on stderr), but nothing reaches the phone. That is
expected in that state, and it is NOT a Telegram test; set the three HKCU vars
below first, then re-run and expect the message and `via telegram`.

Do not use the old recipe (`--state-path C:\nonexistent.json`) as an alert
test any more: it is a real unhealthy run, so it drives the alert state -- a
`[OK -> CRITICAL]` message now and a "recovered" message on the next hourly
run.

If neither arrives, re-check the user-level environment variables
`ALERT_METHOD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` -- set with
`[Environment]::SetEnvironmentVariable('<NAME>', '<value>', 'User')` so
they land in HKCU and are visible to Task-Scheduler-launched processes
without any shell that sources `.env`.

**These three must NEVER go in `.env`.** `send_alert` reads `os.environ`
directly (`monitoring/heartbeat.py:115,144-145`) and, unlike `IBKRSettings`
(which loads `.env` via pydantic-settings' `env_file=".env"`,
`src/venues/ibkr/settings.py:28`), neither `monitoring/heartbeat.py` nor
`scripts/check_ibkr_recorder.py` ever reads `.env` at all -- putting the
alert vars there would silently do nothing; they have to be real process
environment variables. And never paste the bot token into logs, docs, or
chat: `send_alert` itself never logs it (`monitoring/heartbeat.py:131-141`, the
SECRET HYGIENE docstring: only the exception's class name is ever logged, and
every line on the Telegram path is scrubbed) -- follow the same rule
everywhere else.
