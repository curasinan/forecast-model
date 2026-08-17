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
| Data root | `%LOCALAPPDATA%\ibkr_ticks` (deliberately outside OneDrive) |
| Heartbeat | `%LOCALAPPDATA%\ibkr_ticks\state\recorder_state.json` |
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
$s = Get-Content "$env:LOCALAPPDATA\ibkr_ticks\state\recorder_state.json" -Raw | ConvertFrom-Json
$s
[DateTime]::UtcNow - [DateTime]::Parse($s.ts).ToUniversalTime()

# Today's (UTC date) row count via pyarrow
python -c "import os,datetime,pyarrow.dataset as ds; day=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d'); p=os.path.join(os.environ['LOCALAPPDATA'],'ibkr_ticks','quotes','date='+day); print(p); print(ds.dataset(p).count_rows() if os.path.isdir(p) else 'NO DATA for '+day)"

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
