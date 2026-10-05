# Spark runtime move -- design

**Date:** 2026-10-05. **Status:** every section approved in conversation by the
account holder on 2026-10-05; this document awaits their review.
**Plan:** to be written from this document (`docs/plans/`), after the review.
**Relation to Option B:** `2026-09-30-spark-research-split-design.md` called a
Gateway on the Spark "Phase 2 if ever; own spec". This is that spec. At
cutover it replaces Option B's invariants 1, 2, 4 and 5 (section 12 below);
the parity tooling, the LLM client and the LLM boundary stay as they are.

## 0. One-paragraph goal

Move everything that needs IB Gateway -- the Gateway itself, the quote
recorder, the health probe and its Telegram alerts, the paper sync, the
climate label collector and the live `~/quant_data` -- from the Zenbook to
the DGX Spark, so that recording no longer stops when the laptop sleeps,
travels in a bag or has its Gateway closed for the night. The Zenbook becomes
the development machine and a terminal. Nothing in this move changes what the
system trades, how it prices, or the paper-only status of the account.

Why now (measured 2026-10-03/04): sweeps exist on 8 UTC days between
2026-08-16 and 2026-10-03. The causes are the laptop's battery lid-close
sleep, the Gateway being closed at bedtime, and the weekly re-login being
missed. The Spark is always on and was going to host the deep-learning and
simulation work anyway.

## 1. Decisions taken

| # | Decision | Taken |
| --- | --- | --- |
| D1 | Everything that needs the Gateway moves; the Zenbook keeps development, git and remote access | account holder, 2026-10-04 |
| D2 | Gateway login is manual, through a remote screen; no IBKR password is stored on the Spark | account holder, 2026-10-04 |
| D3 | Remote access away from home is Tailscale | account holder, 2026-10-05 |
| D4 | A security layer in front of the Gateway (order guard, access control, request log, kill switch) is a separate project with its own spec | account holder, 2026-10-05 |
| D5 | Order: this move first, the security layer after it, and before any real money | account holder, 2026-10-05 |
| D6 | Approach 1: the Gateway in a container, the services on the Spark host as systemd units | account holder, 2026-10-05 |
| D7 | Interim protection: a firewall rule lets only the operator's own processes reach the Gateway API port | account holder, 2026-10-05 |
| D8 | An external heartbeat service reports when the Spark itself goes silent | account holder, 2026-10-05 |

Approaches considered and not taken: a native ARM64 install of the Gateway
(needs system packages on a box whose driver packages are on hold, and leaves
the Gateway's restart and dialog handling to us), and everything in
containers (strongest isolation, but it means an image for this repo, a
scheduler inside containers and a break with how the Spark already runs the
research code; the longest path).

## 2. Non-goals

- The security layer of D4. This move only leaves the seam for it (section 7).
- Real-money trading. The only session on the Spark is the paper account.
- An LLM anywhere in the pricing or ordering path; the boundary test stays.
- Automatic refits, automatic universe refreshes, a different sweep interval.
- Retiring the Zenbook. It stays the place where code is written and tested.
- Any change to `~/stack` on the Spark: no edit of its compose file, no
  `docker compose pull`, no `apt upgrade`.

## 3. Target layout

**On the Spark**

| Piece | Where | Notes |
| --- | --- | --- |
| IB Gateway (paper) | compose project `~/ibgw/`, apart from `~/stack` | API on `127.0.0.1:4002`, remote screen on `127.0.0.1:5900`, nothing else published |
| Code | `~/work/forecast-model` and its `.venv` | arrives by `git push spark main` from the Zenbook, as today |
| Live data root | `~/quant_data` | today's mirror becomes the live root at cutover; no env-var override, same rule as before |
| Services | systemd user units (section 5) | definitions live in the repo under `deploy/spark/` |
| Backups | `~/quant_data_backups/` | daily, 30 kept |
| Secrets | four files, each mode 600 (section 5) | copied over ssh, never through git |

Hand-run entrypoints (paper order approval, GTM signal run, refit, hourly
backfill, universe refresh, climate forecast fetch) are run over ssh on the
Spark, the way they are run by hand on the Zenbook today.

**On the Zenbook**

- Development, tests, git, `ssh spark`, a VNC client for the Gateway login.
- `~/quant_data` becomes a mirror of the Spark's root, filled by a new
  `pull_snapshot.sh`. The old live root is kept for two weeks as
  `~/quant_data_zenbook_final`.
- The four scheduled tasks (`\IBKR\IBKR Recorder`, `\IBKR\IBKR Recorder
  Health`, `\IBKR\IBKR Paper Sync`, `\Climate\Climate Labels`) are disabled at
  cutover, kept for a week as the rollback path, then unregistered.
  `\Polymarket\backup_db` backs up the retired Polymarket database and is not
  part of this move.

## 4. Gateway container and login

- **Image:** `ghcr.io/gnzsnz/ib-gateway` (IB Gateway, IBC, a virtual display
  and a VNC server in one image; published for arm64). The version that
  passes the spike is pinned by digest in `~/ibgw/docker-compose.yml`. After
  that the `~/stack` discipline applies: no unattended update, no `pull`.
- **Compose project:** `restart: always`; a 4 GiB memory limit; a lowered
  `oom_score_adj`, as `qwenflash` has; a TCP health check on the API port.
  Gateway settings persist in `~/ibgw/jts/`, so the weekly login does not
  redo them.
- **Published ports:** `127.0.0.1:4002` (API, paper) and `127.0.0.1:5900`
  (VNC). Neither is reachable from outside the Spark.
- **Environment:** paper mode, `TZ=Europe/Istanbul`, daily auto-restart at
  23:45 local (as on the Zenbook), Read-Only API off (paper orders need it,
  as today). The IBKR user and password fields are EMPTY: IBC leaves the
  login dialog up and the account holder types the credentials. The remote
  screen has its own password, which is not an IBKR credential.
- **Login** (every Sunday after 01:00 US Eastern, when IBKR expires the
  weekly session -- 08:00 Istanbul time, 09:00 in the US winter -- and after
  any other drop): `ssh -L
  5900:127.0.0.1:5900 spark` over Tailscale, a VNC client on
  `localhost:5900`, the credentials typed into the Gateway's own dialog. The
  recorder reconnects by itself; the health probe reports the recovery.
- **Knowing a login is due:** the existing alert on a change of the
  (recorder, sync) status pair sends DOWN when the Gateway drops and repeats
  once a day while it lasts.
- **One login at a time:** a second login with the same paper user drops the
  first. After cutover the paper user logs in on the Spark only.

If the image cannot be started with empty credentials (assumption A2), the
fallbacks are, in order: the same image without IBC's auto-login, then the
native install that was approach 2.

## 5. Services, scheduling, secrets

**systemd user units** (`loginctl enable-linger` once, so they run without a
login session). Timers use `Persistent=true`: a run missed while the Spark was
off fires at the next boot.

| Unit | Runs | When |
| --- | --- | --- |
| `ibkr-recorder.service` | `scripts/record_ibkr_ticks.py --interval-minutes 20` | always; `Restart=always`, 30 s back-off |
| `ibkr-health.timer` | `scripts/check_ibkr_recorder.py`, then the heartbeat ping (section 8) | hourly, on the hour |
| `ibkr-paper-sync.timer` | `scripts/sync_ibkr_paper.py` | daily 17:30 |
| `climate-labels.timer` | `scripts/collect_climate_labels.py`, then its heartbeat ping | daily 16:30 |
| `paper-book-backup.timer` | the new backup script, then its heartbeat ping | daily 21:00 |

The times are today's; the Spark's clock is already Europe/Istanbul.

**Deploy:** one script run from the Zenbook pushes `main` to the Spark,
installs or refreshes the unit files from `deploy/spark/`, reloads systemd
and restarts the recorder right after a sweep has finished: it waits, for at
most one sweep interval, until the recorder's heartbeat file is less than two
minutes old, so a deploy does not cost a sweep. It has a dry-run mode that
prints what it would do, and an option to leave the recorder running.

**Secrets: each unit sees only what it needs**

| File (mode 600) | Holds | Loaded by |
| --- | --- | --- |
| `~/work/forecast-model/.env` | `IBKR_PORT`, `IBKR_TRADING_MODE`, `IBKR_RESTART_WINDOW_LOCAL`, the fee settings, `LLM_MODEL` | every entrypoint, through the settings classes |
| `~/.config/forecast-model/alerts.env` | `ALERT_METHOD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | the health unit only |
| `~/.config/forecast-model/heartbeat.env` | the three ping URLs of section 8 | the health, labels and backup units |
| `~/.cdsapirc` | the CDS key | `fetch_climate_forecasts.py`, run by hand |

`ANTHROPIC_API_KEY`, `NEWSAPI_KEY` and `GITHUB_TOKEN` do not go to the Spark:
nothing in the runtime reads them.

**Backup script** (`monitoring/backup_paper_book.py`): copies the operational
files that `analysis/spark_parity.py` already lists as `MANIFEST_FILES` into
`~/quant_data_backups/YYYYMMDD/`, taking the paper book through SQLite's
online backup API so a copy made during a sync is consistent; keeps the 30
newest days; exits non-zero on any failure.

**Protecting the recorder from training jobs:** the Gateway container has a
memory limit and a lowered kill priority; the recorder and the timers are a
few hundred MB together. Under memory pressure Linux kills the largest
unprotected process, which is the training job. A launcher for training and
simulation runs sets a memory ceiling (`systemd-run --user --scope -p
MemoryMax=`): 15 GiB by default, the budget the stack leaves with the LLM
loaded, and a larger value given explicitly when `qwenflash` has been stopped
for a big run, as the existing rule requires.

## 6. Data: cutover, mirror, backups, rollback

**Cutover** (one weekday evening, away from 16:30, 17:30 and 23:45; about
30 to 45 minutes without recording, which a later hourly backfill fills):

1. Preconditions: units installed and stopped on the Spark, the Gateway
   container up and logged out, both machines at the same commit, suite green
   on the Spark.
2. Freeze the Zenbook: the account holder disables the four scheduled tasks
   (elevated PowerShell), the recorder process ends, the Zenbook Gateway is
   logged out.
3. Final push: `push_snapshot.sh --include-raw`. Then the operational
   manifest (`scripts/spark/live_manifest.py`) is taken on both machines and
   must agree in sha256 and size, file for file.
4. Start the Spark: the account holder logs in through the remote screen; the
   units are enabled and started.
5. Gate -- every line must hold, otherwise roll back:
   - a first sweep file under the Spark's `quotes/` with at least 2,000 rows;
   - the health probe exits 0 and a `--test-alert` from the Spark arrives;
   - a hand-run paper sync reports the same book as the Zenbook's last run;
   - the GTM signal run passes the parity gate of Option B against the
     frozen snapshot.
6. The outcome is written to a cutover report under `docs/`, in the manner of
   the parity report.

**After cutover the direction of data reverses.** The Spark's `~/quant_data`
is the live root. On the Zenbook the old root is renamed to
`~/quant_data_zenbook_final`, and a new `~/quant_data` is created holding a
marker file `.mirror-of-spark`. `scripts/spark/pull_snapshot.sh` (rsync over
ssh, the excludes of the push script) fills it and refuses to run unless the
marker is there, so it can never overwrite a live root. Entry points that
write live state -- the recorder, the paper sync, `run_ibkr_paper.py`,
`run_gtm_paper.py`, the hourly backfill, the label collector, the forecast
fetch and `fit_gtm_emos.py --live` -- refuse to run on a root that carries
the marker.

**Backups.** Daily on the Spark, 30 kept: this covers a bad write, not a dead
disk. The copy off the box is the Zenbook's pulled mirror; a pull at least
once a week is the recommendation. If the Spark's disk died between pulls,
the sync records since the last pull would be lost; the orders themselves are
at the venue and can be synced again.

**Rollback** (within the first week, one script with a dry-run mode): stop
the Spark units and log the Spark Gateway out; copy the operational files,
`quotes/`, `quotes_hourly/` and `backfill_runs.jsonl` from the Spark into
`~/quant_data_zenbook_final`; the account holder renames it back to
`~/quant_data`, re-enables the tasks and logs in on the Zenbook. This script
is the only place where data is copied from the Spark into a Zenbook live
root.

## 7. Network, access, interim protections, the seam

**Tailscale** on the Spark and the Zenbook (the phone if wanted). The
account holder installs it and signs in; on the Spark it is a new package and
touches none of the held driver packages. Key expiry is switched off for the
Spark node in the Tailscale admin console, or it leaves the tailnet after 180
days. The Zenbook's `ssh spark` shortcut points at the Tailscale name, so the
same command works at home and away, and the LAN address stops mattering when
the Spark moves country. `ufw` keeps allowing ssh only, now also on the
Tailscale interface.

**Access paths:** `ssh spark` with a key, as today; the Gateway screen
through an ssh tunnel (section 4). Rule: scripts that talk to the venue run
on the Spark only. The Zenbook does not connect to the Gateway after cutover.

**Interim protections, until the security layer exists**

- The Gateway accepts API connections from the Spark itself only (the
  published port is bound to 127.0.0.1).
- One client id per role, defined in one place (`CLIENT_IDS`): recorder 2,
  research 3, paper sync 4, order placement 1. The only path that can send an
  order is the paper-order script the account holder runs by hand.
- **D7, the owner rule:** the LLM stack's containers use the host network and
  could reach `127.0.0.1:4002`. One firewall rule (an `iptables` owner match
  on loopback traffic to that port) rejects connections from any process
  that does not run as the operator's user. It is installed once by the
  account holder with `sudo`, as a small system unit so that it survives a
  reboot, and the spike proves it (section 9, check 7).
- **Kill switch:** `ssh spark 'cd ~/ibgw && docker compose stop'` closes the
  Gateway and with it every connection to the venue. Starting it again needs
  a manual login.

**The seam the move leaves for the security layer**

1. Every program reads the Gateway address from settings; none hard-codes it.
2. Client ids are defined in one place, so the layer can grant by id.
3. Publishing the Gateway port to the host is one line of the compose file;
   with the layer in place that line becomes "published to the layer only".
4. The code paths that can send an order are already marked apart
   (`role="live_trading"`, `read_only=False`); an approval step attaches there.

## 8. External heartbeat

The alerts run on the Spark, so a Spark that is off or cut from the network
reports nothing. A free external heartbeat service (healthchecks.io or an
equivalent; the account holder opens the account) expects a ping on a
schedule and notifies by e-mail or Telegram when one is late. Three checks:

| Check | Pinged by | Expected | Tells |
| --- | --- | --- | --- |
| `spark-health` | the health unit, after the probe ran (whatever it found) | hourly | the Spark is up and the probe runs |
| `climate-labels` | the labels unit, on success | daily | the label collector worked |
| `paper-backup` | the backup unit, on success | daily | the backup worked |

"The probe ran" means it exited 0 (healthy) or 1 (it found a problem and the
Telegram alert path handled it); any other exit is a probe that broke, and no
ping is sent, so the check goes late and the service reports it.

Only "alive" signals leave the Spark; no data does. A ping URL is a
capability: whoever holds it can fake a signal. The URLs therefore live in
`heartbeat.env` (mode 600) and must never reach a log: the ping wrapper
prints an exit status only, and a test asserts that its output never contains
the URL. (This repo already paid for a secret in a URL once; see the alert
change of 2026-10-03.)

## 9. Verification

**A. Spike** (first, about one hour, leaves nothing running; the Zenbook
Gateway is logged out for its duration). Each check has a pass criterion:

1. The image starts on the Spark; health check green; memory under 4 GiB.
2. The remote screen opens through an ssh tunnel and shows the login dialog
   of a Gateway started with empty credentials.
3. A manual paper login succeeds; no second factor is asked for.
4. From the repo's venv on the Spark, the API answers a current-time request.
5. The recorder runs one sweep into a scratch root: about 2,400 rows.
6. An auto-restart set ten minutes ahead comes back within two minutes
   without a new login.
7. The owner rule works: a connection to the API port from inside an LLM
   container is rejected, the recorder's is accepted.
8. The kill switch cuts the session; what a restart needs is written down.

The spike ends in a short report: go, or stop and which fallback.

**B. Automated tests** (written first, as everywhere in this repo):

- backup script: a consistent copy while a writer is active; rotation at 30;
  non-zero exit on failure;
- unit files: every `ExecStart` names a script that exists, every timer has
  its service, no secret value in any file under `deploy/`;
- the mirror marker: the pull script refuses without it, the live-writing
  entry points refuse with it;
- the ping wrapper never prints its URL;
- shell scripts (deploy, pull, rollback): a dry-run mode each, and a syntax
  check in the suite;
- the full suite and ruff green on both machines.

**C. Cutover gate:** section 6, step 5.

**D. First week and acceptance**

- seven consecutive days with at least 70 sweeps a day (a full day is 72),
  not counting the weekly-login and planned-restart windows;
- the weekly login done at least once from outside the home network;
- a training job run through the memory-capped launcher while the recorder
  kept its cadence;
- the rollback script's dry run verified;
- runbooks updated and the old invariants replaced.

When these hold, the Zenbook's tasks are unregistered.

## 10. Risks

| Risk | Answer |
| --- | --- |
| The image will not start with empty credentials | found in the spike; fallbacks in section 4 |
| The weekly login is late | recording stops; DOWN alert, repeated daily |
| The Spark is off or offline | external heartbeat (section 8); the account holder also checks that the Spark powers on by itself after an outage |
| A training job eats the memory | capped launcher; `qwenflash` rule |
| One disk | daily local backup, weekly pull to the Zenbook |
| The paper user logs in elsewhere | it drops the Spark's session; rule: paper user on the Spark only |
| Someone with access to the Spark trades the paper account | Tailscale, key-only ssh, the owner rule; the lasting answer is the security layer |
| The move to the US changes the time zone | job times and the restart window are reviewed that day; noted in the runbook |

## 11. Sequence, and what the account holder does by hand

1. Independent of this move and before 2026-10-09: the remaining hourly
   backfill run and the universe refresh, on the Zenbook.
2. The spike (section 9A). By hand: the Gateway login.
3. The implementation plan, written from this document and reviewed.
4. The build: repo changes and tests. The running system is not touched.
5. One-time setup by hand: Tailscale on both machines, `loginctl
   enable-linger`, the owner-rule unit, the heartbeat account and its three
   URLs, the secret files of section 5.
6. Cutover evening (section 6). By hand: disabling the Zenbook tasks, the
   logout on the Zenbook, the login on the Spark.
7. The first week, acceptance, unregistering the Zenbook tasks.
8. Afterwards: the security layer, with its own spec.

## 12. Invariants after cutover

Each with the mechanism that enforces it. They replace invariants 1, 2, 4 and
5 of the Option B design; its invariant 3 (the same default data root on both
machines, no env-var override) stands unchanged.

1. **One live root, on the Spark.** The Zenbook's `~/quant_data` carries the
   marker `.mirror-of-spark`; live-writing entry points refuse to run on a
   marked root, and the pull script refuses to write into an unmarked one.
2. **Data flows Spark to Zenbook only.** `pull_snapshot.sh` is the only
   routine copy; `rollback.sh` is the single, deliberate exception.
3. **The Gateway API is reachable only on the Spark and only by the
   operator's processes.** The compose file binds the port to 127.0.0.1; the
   owner rule rejects every other user; `ufw` allows ssh only.
4. **No IBKR password at rest on the Spark.** The compose environment leaves
   the credential fields empty, and a test fails if a file under `deploy/`
   gives them a value.
5. **Secrets live in the four files of section 5, mode 600, outside git.**
   The health unit alone loads the Telegram token.
6. **One process per client id.** Unchanged (`src/venues/ibkr/clients.py`).
7. **No LLM in the pricing or ordering path.** Unchanged
   (`tests/test_architecture/test_llm_boundary.py`).

## 13. Assumptions to confirm

- A1. The image runs on the Spark (aarch64, Docker 29.6). Spike check 1.
- A2. Empty credentials leave the login dialog up for a manual login. Check 2.
- A3. The paper login asks for no second factor. Check 3.
- A4. The image's paper API port can be published as `127.0.0.1:4002`; the
  exact container port and environment variable names are read from the
  image's documentation during the spike and recorded in its report.
- A5. The image's auto-restart keeps the session through the day. Check 6.
- A6. The LLM containers do not run as the operator's numeric user id, so the
  owner rule separates them from the recorder. Check 7.
- A7. The Spark stays on and connected around the clock and powers on by
  itself after an outage (the account holder confirms the firmware setting).
- A8. The account holder opens the heartbeat account and installs Tailscale.

## 14. Repo changes this design implies

New: `deploy/spark/` (unit files, the Gateway compose file, the owner-rule
unit), `scripts/spark/deploy.sh`, `scripts/spark/pull_snapshot.sh`,
`scripts/spark/rollback.sh`, the ping wrapper, the training launcher,
`monitoring/backup_paper_book.py`, a cutover check that evaluates the gate of
section 6, and the tests of section 9B. Changed: the live-writing entry
points (the mirror-marker refusal), `docs/spark_runbook.md` and
`docs/ibkr_runbook.md` (new roles, login, deploy, rollback),
`scripts/scheduling/README.md`, `docs/MIGRATION_CHECKPOINT.md`, and an
amendment note in the Option B design pointing here. The Windows launchers
and task registration scripts stay until the Zenbook tasks are unregistered.
