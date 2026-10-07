# Spark runtime move -- design

**Date:** 2026-10-05. **Status:** approved by the account holder on
2026-10-05: the seven sections in conversation, then nine review points the
same evening, all of which are worked in below (section 15 maps them).
**Plan:** `docs/plans/` -- written from this document.
**Relation to Option B:** `2026-09-30-spark-research-split-design.md` called a
Gateway on the Spark "Phase 2 if ever; own spec". This is that spec. At
cutover it replaces Option B's invariants 1, 2, 4 and 5 (section 12 below);
the parity tooling, the LLM client and the LLM boundary stay as they are.

## 0. One-paragraph goal

Move everything that needs IB Gateway -- the Gateway itself, the quote
recorder, the health probe and its Telegram alerts, the paper sync, the
climate label collector and the live data root -- from the Zenbook to the DGX
Spark, so that recording no longer stops when the laptop sleeps, travels in a
bag or has its Gateway closed for the night. On the Spark the runtime runs
under its own Linux user, walled off from the research code and from the LLM
stack. The Zenbook becomes the development machine and a terminal. Nothing in
this move changes what the system trades, how it prices, or the paper-only
status of the account.

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
| D6 | The Gateway runs in a container; the runtime's programs run on the Spark host under systemd | account holder, 2026-10-05 |
| D7 | Loopback isolation: an owner rule lets only the runtime's user reach the Gateway API port, and the Gateway keeps "localhost only" on | account holder, 2026-10-05 |
| D8 | An external check reports when the Spark itself goes silent | account holder, 2026-10-05 |
| D9 | The runtime runs as its own Linux user, `trading`; the operator's user, the research code and the LLM stack are other users | account holder, 2026-10-05 |
| D10 | Kill order under memory pressure: research first, then the LLM, the trading runtime last | account holder, 2026-10-05 |
| D11 | The Spark becomes the only owner of the data, so there is a scheduled copy off the machine and a restore that has been tried | account holder, 2026-10-05 |
| D12 | Schedules are systemd timers that name their time zone | account holder, 2026-10-05 |

D9 turns the "systemd user units" first agreed under D6 into system units
with `User=trading`, and narrows D7 from "the operator's processes" to "the
`trading` user's processes".

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
  `docker compose pull`, no `apt upgrade`. Section 5 shows that the kill
  order of D10 needs no change there.

## 3. Target layout

Two users on the Spark, three data roots in all.

| User | Runs | Can reach the Gateway API |
| --- | --- | --- |
| `trading` (new; key-only ssh, no password, not in the `docker` group) | recorder, health probe, paper sync, label collector, backup, and every hand-run entry point that talks to the venue or writes live state | yes, and nobody else |
| `scura` (the operator, as today) | the research clone, training and simulation, the LLM stack, the Gateway's compose project, the remote-screen tunnel | no |

`scura` has `sudo` and Docker, which are root in all but name; the wall is
against accidents and against unprivileged code that runs as `scura`
(research jobs, tools inside the LLM containers), not against the operator.

**On the Spark**

| Piece | Where | Notes |
| --- | --- | --- |
| IB Gateway (paper) | compose project `~/ibgw/` of `scura`, apart from `~/stack` | API on `127.0.0.1:4002`, remote screen on `127.0.0.1:5900`, nothing else published |
| Runtime code | `/home/trading/forecast-model` and its `.venv` | receives `main` only, by `git push` from the Zenbook to a second remote |
| Live data root | `/srv/quant_data`, owned by `trading`, group-readable | `/home/trading/quant_data` is a symlink to it, so the code's default root is unchanged and no env var names it |
| Backups | `/srv/quant_data_backups`, owned by `trading` | daily, 30 kept |
| Research code | `~/work/forecast-model` of `scura`, as today | branches and experiments; never talks to the venue |
| Research mirror | `~/quant_data` of `scura` | a copy of the live root, refreshed by a script that reads it through the group; carries the mirror marker (section 6) |
| Secrets | four files of `trading`, mode 600, and the Gateway project's `.env` of `scura` (section 5) | copied over ssh, never through git |

Hand-run entry points (paper order approval, GTM signal run, refit, hourly
backfill, universe refresh, climate forecast fetch) are run as `trading`,
over `ssh trading@spark`, the way they are run by hand on the Zenbook today.

**On the Zenbook**

- Development, tests, git, ssh to both users, a VNC client for the login.
- `~/quant_data` becomes a mirror of the Spark's live root, filled on a
  schedule by a new `pull_snapshot.sh`; this is the copy off the machine
  (section 6). The old live root is kept for two weeks as
  `~/quant_data_zenbook_final`.
- The four scheduled tasks (`\IBKR\IBKR Recorder`, `\IBKR\IBKR Recorder
  Health`, `\IBKR\IBKR Paper Sync`, `\Climate\Climate Labels`) are disabled at
  cutover, kept for a week as the rollback path, then unregistered.
  `\Polymarket\backup_db` backs up the retired Polymarket database and is not
  part of this move.

## 4. Gateway container and login

- **Image:** `ghcr.io/gnzsnz/ib-gateway` (IB Gateway, IBC, a virtual display
  (Xvfb) and a VNC server in one image; published for arm64). The version
  that passes the spike is pinned by digest in `~/ibgw/docker-compose.yml`.
  After that the `~/stack` discipline applies: no unattended update, no
  `pull`.
- **Compose project:** `restart: unless-stopped` (it comes back after a
  crash or a reboot, while a deliberate stop stays in force across a
  reboot, so the kill switch and a rollback survive one); a 4 GiB memory limit;
  `oom_score_adj: -900` (section 5); a TCP health check on the API port.
  Gateway settings persist in `~/ibgw/jts/`, so the weekly login does not
  redo them.
- **Published ports:** `127.0.0.1:4002` (API, paper) and `127.0.0.1:5900`
  (VNC). Neither is reachable from outside the Spark, and section 7 says who
  on the Spark may connect to each.
- **Environment:** paper mode, `TZ=Europe/Istanbul`, daily auto-restart at
  23:45 Europe/Istanbul (as on the Zenbook), Read-Only API off (paper orders
  need it, as today), "allow connections from localhost only" on. The IBKR
  user and password fields are EMPTY: IBC leaves the login dialog up and the
  account holder types the credentials. The remote screen has its own
  password, which is not an IBKR credential.
- **Login** (every Sunday after 01:00 US Eastern, when IBKR expires the
  weekly session -- 08:00 Istanbul time, 09:00 in the US winter -- and after
  any other drop): `ssh -L 5900:127.0.0.1:5900 spark` over Tailscale, a VNC
  client on `localhost:5900`, the credentials typed into the Gateway's own
  dialog. The recorder reconnects by itself; the health probe reports the
  recovery.
- **Knowing a login is due:** the existing alert on a change of the
  (recorder, sync) status pair sends DOWN when the Gateway drops and repeats
  once a day while it lasts.

**One session, one machine.** A second login with the same paper user drops
the first, and two recorders would then fight over it. At no moment may the
Gateway be logged in on both machines. Every procedure that moves the session
-- the spike, the cutover, the rollback -- is ordered so that the machine
giving the session up is logged out and checked (no Gateway process, nothing
listening on its API port) before the other machine logs in; a small check
script does that test and the procedures call it between the two steps.

If the image cannot be started with empty credentials (assumption A2), the
fallbacks are, in order: the same image without IBC's auto-login, then the
native install that was approach 2.

## 5. Services, scheduling, secrets, kill order

**systemd system units**, each with `User=trading`, `UMask=0027`,
`NoNewPrivileges=yes` and `OOMScoreAdjust=-900`. Timers name their time zone
in `OnCalendar` and use `Persistent=true`: a run missed while the Spark was
off fires at the next boot.

| Unit | Runs | `OnCalendar` |
| --- | --- | --- |
| `ibkr-recorder.service` | `scripts/record_ibkr_ticks.py --interval-minutes 20` | none: always on, `Restart=always`, 30 s back-off |
| `ibkr-health.timer` | `scripts/check_ibkr_recorder.py`, then the heartbeat ping (section 8) | `*-*-* *:00:00 Europe/Istanbul` |
| `ibkr-paper-sync.timer` | `scripts/sync_ibkr_paper.py` | `*-*-* 17:30:00 Europe/Istanbul` |
| `climate-labels.timer` | `scripts/collect_climate_labels.py`, then its heartbeat ping | `*-*-* 16:30:00 Europe/Istanbul` |
| `paper-book-backup.timer` | the new backup script, then its heartbeat ping | `*-*-* 21:00:00 Europe/Istanbul` |

The times are today's. Writing the zone into each timer means that changing
the Spark's clock zone, as the move to the US will, does not move a job by
itself; whether the jobs should then move is decided that day.

Unit files are installed and changed by the account holder with `sudo`
(they live in the repo under `deploy/spark/`, so the change is reviewed
first). A sudoers drop-in, also reviewed and installed by hand, lets `scura`
start and stop the units above and the services behind the timers, and restart
`ibkr-recorder.service`, without a password, and nothing else and no other
verb; that is what a deploy needs.

**Deploy:** one script run from the Zenbook pushes `main` to the runtime
clone (`trading@spark`), reports whether any unit file differs from the
installed one (and stops for the manual `sudo` step if so), and restarts the
recorder right after a sweep has finished: it waits, for at most one sweep
interval, until the recorder's heartbeat file is less than two minutes old,
so a deploy does not cost a sweep. It has a dry-run mode that prints what it
would do, and an option to leave the recorder running.

**Credential files: where, whose, who loads them**

| File | Owner, mode | Holds | Loaded by |
| --- | --- | --- | --- |
| `/home/trading/forecast-model/.env` | `trading`, 600 | `IBKR_PORT`, `IBKR_TRADING_MODE`, `IBKR_RESTART_WINDOW_LOCAL`, the fee settings | every runtime entry point, through the settings classes |
| `/home/trading/.config/forecast-model/alerts.env` | `trading`, 600 | `ALERT_METHOD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | the health unit only |
| `/home/trading/.config/forecast-model/heartbeat.env` | `trading`, 600 | the ping URLs of section 8 | the health, labels and backup units |
| `/home/trading/.cdsapirc` | `trading`, 600 | the CDS key | `fetch_climate_forecasts.py`, run by hand |
| `~/ibgw/.env` of `scura` | `scura`, 600 | the remote screen's password; the IBKR fields empty | the Gateway compose project |

None of these is in git: `.env` is ignored already, the others live outside
any clone, and a test fails if a file under `deploy/` gives a value to a
secret key. They are copied from the Zenbook over ssh. The research clone's
`.env` keeps holding `LLM_MODEL` and nothing else, so research code has no
Gateway settings to start from. `ANTHROPIC_API_KEY`, `NEWSAPI_KEY` and
`GITHUB_TOKEN` do not go to the Spark: nothing in the runtime reads them.

**Backup script** (`monitoring/backup_paper_book.py`): copies the operational
files that `analysis/spark_parity.py` already lists as `MANIFEST_FILES` into
`/srv/quant_data_backups/YYYYMMDD/`, taking the paper book through SQLite's
online backup API so a copy made during a sync is consistent; writes a
manifest (sha256, size) beside the copy; keeps the 30 newest days; exits
non-zero on any failure.

**Kill order under memory pressure (D10): research, then the LLM, the
runtime last.** Linux kills the process with the highest badness, and
`oom_score_adj` shifts it.

| Who | `oom_score_adj` | Set by |
| --- | --- | --- |
| Gateway container | -900 | `~/ibgw/docker-compose.yml` (ours) |
| recorder, paper sync, health, labels, backup | -900 | `OOMScoreAdjust=` in the unit files; only system units can lower it, which is one reason they are system units |
| `qwenflash` (vLLM) | -500 | `~/stack/docker-compose.yml`, as it is today |
| training and simulation runs | +500 | the research launcher |

So the existing `-500` of the LLM already sits between the two; nothing in
`~/stack` has to change, and no change to its compose file is proposed. The
research launcher (`systemd-run --user --scope`) also sets a memory ceiling:
15 GiB by default, the budget the stack leaves with the LLM loaded, and a
larger value given explicitly when `qwenflash` has been stopped for a big
run, as the existing rule requires.

## 6. Data: cutover, mirrors, backups, restore, rollback

**Cutover** (one weekday evening, away from 16:30, 17:30 and 23:45; about
30 to 45 minutes without recording, which a later hourly backfill fills).
The session moves exactly once and is never on both machines:

1. Preconditions: units installed and stopped on the Spark, the Gateway
   container up and logged out, both machines at the same commit, suite green
   on the Spark.
2. Freeze the Zenbook: the account holder disables the four scheduled tasks
   (elevated PowerShell), the recorder process ends, the Zenbook Gateway is
   logged out and closed. The check script confirms: no Gateway process,
   nothing listening on 4002. Nothing below starts until it passes.
3. Final push: `push_snapshot.sh --include-raw` into the live root. Then the
   operational manifest (`scripts/spark/live_manifest.py`) is taken on both
   machines and must agree in sha256 and size, file for file.
4. Start the Spark: the account holder logs in through the remote screen; the
   units are enabled and started.
5. Gate -- every line must hold, otherwise roll back:
   - a first sweep file under the live root's `quotes/` with at least 2,000
     rows;
   - the health probe exits 0 and a `--test-alert` from the Spark arrives;
   - a hand-run paper sync reports the same book as the Zenbook's last run
     (a count that differs because the venue itself filled or cancelled an
     order in between is recorded with both values and is not a failure);
   - the GTM signal run passes the parity gate of Option B against the
     frozen snapshot.
6. The outcome is written to a cutover report under `docs/`, in the manner of
   the parity report.

**After cutover the direction of data reverses.** `/srv/quant_data` on the
Spark is the live root and the only place where live state is written. Both
other roots are mirrors and carry a marker file, `.mirror-of-spark`:

- the research mirror, `~/quant_data` of `scura` on the Spark, refreshed by
  `scripts/spark/mirror_live.sh`;
- the Zenbook's `~/quant_data`, filled by `scripts/spark/pull_snapshot.sh`
  (rsync over ssh, the excludes of the push script). On the Zenbook the old
  root is first renamed to `~/quant_data_zenbook_final`.

Both scripts refuse to write into a root without the marker, so neither can
overwrite a live root. Entry points that write live state -- the recorder,
the paper sync, `run_ibkr_paper.py`, `run_gtm_paper.py`, the hourly backfill,
the label collector, the forecast fetch and `fit_gtm_emos.py --live` --
refuse to run on a root that carries the marker.

**Backups (D11).** Three layers, because the Spark now holds the only
original:

1. Daily on the Spark, 30 kept (`paper-book-backup.timer`): covers a bad
   write, not a dead disk.
2. Off the machine: the Zenbook pulls the live root on a schedule -- a
   Windows scheduled task, at logon and every six hours while the laptop is
   on. Each successful pull pings an external check (section 8) whose grace
   is three days, so a laptop that has not pulled for three days is reported.
3. A restore that has been tried: `scripts/spark/restore_check.py` restores
   the operational files from a named copy (the Zenbook mirror or a daily
   backup) into a scratch root and verifies them -- the paper book passes
   SQLite's integrity check and opens with the repo's own reader, every file
   matches the manifest written at backup time, every parquet file reads. It
   is run from the Zenbook mirror before the Zenbook tasks are unregistered
   (an acceptance criterion) and once a month afterwards.

If the Spark's disk died between pulls, the sync records since the last pull
would be lost; the orders themselves are at the venue and can be synced
again.

**Rollback** (within the first week, one script with a dry-run mode; the
mirror image of the cutover, and again the session is never on both
machines): stop the Spark units; stop the Gateway container, and the check
script confirms nothing listens on the Spark's 4002; copy the operational
files, `quotes/`, `quotes_hourly/` and `backfill_runs.jsonl` from the Spark
into `~/quant_data_zenbook_final`; the account holder renames it back to
`~/quant_data`, re-enables the tasks and logs in on the Zenbook. This script
is the only place where data is copied from the Spark into a Zenbook live
root.

## 7. Network, access, loopback isolation, the seam

**Tailscale** on the Spark and the Zenbook (the phone if wanted). The
account holder installs it and signs in; on the Spark it is a new package and
touches none of the held driver packages. Key expiry is switched off for the
Spark node in the Tailscale admin console, or it leaves the tailnet after 180
days. The Zenbook's ssh shortcuts (`spark` for `scura`, `spark-rt` for
`trading`) point at the Tailscale name, so the same commands work at home and
away, and the LAN address stops mattering when the Spark moves country. `ufw`
keeps allowing ssh only, now also on the Tailscale interface. Tailscale is
installed before the spike, because the spike has to prove the remote screen
over it.

**Access paths:** ssh with a key to either user; the Gateway screen through
an ssh tunnel as `scura` (section 4). Rule: scripts that talk to the venue
run on the Spark only, as `trading`. The Zenbook does not connect to the
Gateway after cutover.

**Loopback isolation (D7, D9).** Open WebUI and vLLM run on the host
network, so "bound to 127.0.0.1" does not keep them away from the Gateway.
Three things do, and the security layer will later replace them with
something finer:

- The Gateway keeps "allow connections from localhost only" on.
- An `iptables` owner match on loopback traffic rejects every connection to
  port 4002 that does not come from a process of the `trading` user, and
  every connection to port 5900 that does not come from a process of `scura`
  (the tunnel). It is installed once by the account holder with `sudo`, as a
  small system unit so that it survives a reboot.
- One client id per role, defined in one place (`CLIENT_IDS`): recorder 2,
  research 3, paper sync 4, order placement 1. The only path that can send an
  order is the paper-order script the account holder runs by hand as
  `trading`.

The spike proves the rule from three sides (section 9, check 7): `trading`
connects; `scura` is rejected; a process inside an LLM container is rejected.

**Kill switch:** `ssh spark 'cd ~/ibgw && docker compose stop'` closes the
Gateway and with it every connection to the venue. Starting it again needs
a manual login.

**The seam the move leaves for the security layer**

1. Every program reads the Gateway address from settings; none hard-codes it.
2. Client ids are defined in one place, so the layer can grant by id.
3. Publishing the Gateway port to the host is one line of the compose file;
   with the layer in place that line becomes "published to the layer only".
4. The code paths that can send an order are already marked apart
   (`role="live_trading"`, `read_only=False`); an approval step attaches there.
5. The runtime already has a user of its own, so the layer can run as a
   third one and the owner rule can be turned to admit only it.

## 8. External check: who reports a silent Spark

The alerts run on the Spark, so a Spark that is off or cut from the network
reports nothing. A free external heartbeat service (healthchecks.io or an
equivalent; the account holder opens the account) expects a ping on a
schedule and notifies by e-mail or Telegram when one is late. Four checks:

| Check | Pinged by | Expected | Tells |
| --- | --- | --- | --- |
| `spark-health` | the health unit, after the probe ran (whatever it found) | hourly | the Spark is up and the probe runs |
| `climate-labels` | the labels unit, on success | daily | the label collector worked |
| `paper-backup` | the backup unit, on success | daily | the local backup worked |
| `offbox-pull` | the Zenbook's pull, on success | every three days at the latest | a copy off the machine is recent |

"The probe ran" means it exited 0 (healthy) or 1 (it found a problem and the
Telegram alert path handled it); any other exit is a probe that broke, and no
ping is sent, so the check goes late and the service reports it.

Only "alive" signals leave the machines; no data does. A ping URL is a
capability: whoever holds it can fake a signal. The URLs therefore live in
`heartbeat.env` (mode 600; the Zenbook's one in a user environment variable)
and must never reach a log: the ping wrapper prints an exit status only, and
a test asserts that its output never contains the URL. (This repo already
paid for a secret in a URL once; see the alert change of 2026-10-03.)

## 9. Verification

**A. Spike** (the first step; about one hour; leaves nothing running). Its
question: does IB Gateway run on aarch64 with IBC and a virtual display, and
can it be logged in to through VNC over Tailscale? Before it, by hand:
Tailscale on both machines, the `trading` user and `/srv/quant_data`, the
owner-rule unit. The runtime clone and its venv are created as the spike's
first step, over `ssh trading@spark`, and stay for the build. During it the
Zenbook Gateway is logged out and checked
first, and at its end the Spark's Gateway container is stopped and checked
before the Zenbook logs in again. Each check has a pass criterion:

1. The image starts on the Spark; health check green; memory under 4 GiB.
2. From the Zenbook, over Tailscale, the remote screen opens through an ssh
   tunnel and shows the login dialog of a Gateway started with empty
   credentials.
3. A manual paper login succeeds; whether a second factor was asked for is
   recorded (assumption A3).
4. As `trading`, from the runtime clone's venv, the API answers a
   current-time request.
5. The recorder runs one sweep into a scratch root: about 2,400 rows.
6. An auto-restart set ten minutes ahead comes back within two minutes
   without a new login.
7. The owner rule: `trading` connects to 4002; `scura` is rejected; a
   process inside an LLM container is rejected.
8. The kill switch cuts the session; what a start after it needs is seen
   (the login dialog, not a resumed session) and written down.
9. `oom_score_adj` reads -900 for the Gateway's processes and -500 for
   `qwenflash`.

The spike ends in a short report: go, or stop and which fallback.

**B. Automated tests** (written first, as everywhere in this repo):

- backup script: a consistent copy while a writer is active; the manifest
  matches the copy; rotation at 30; non-zero exit on failure;
- restore check: a good copy passes; a truncated paper book, a changed file
  and a missing file each fail it;
- unit files: every `ExecStart` names a script that exists, every timer has
  its service and names a time zone, every unit runs as `trading` with the
  agreed `OOMScoreAdjust`, no secret value in any file under `deploy/`;
- the mirror marker: the pull and mirror scripts refuse without it, the
  live-writing entry points refuse with it;
- the ping wrapper never prints its URL;
- the session check reports a listening port and a missing one correctly;
- shell scripts (deploy, pull, mirror, rollback): a dry-run mode each, and a
  syntax check in the suite;
- the full suite and ruff green on both machines.

**C. Cutover gate:** section 6, step 5.

**D. First week and acceptance**

- seven consecutive days with at least 70 sweeps a day (a full day is 72),
  not counting the weekly-login and planned-restart windows;
- the weekly login done at least once from outside the home network;
- a training job run through the research launcher while the recorder kept
  its cadence, with the kill order read back from `/proc`;
- the restore check passed against the Zenbook mirror;
- the rollback script's dry run verified;
- runbooks updated and the old invariants replaced.

When these hold, the Zenbook's tasks are unregistered.

## 10. Risks

| Risk | Answer |
| --- | --- |
| The image will not start with empty credentials | found in the spike; fallbacks in section 4 |
| The weekly login is late | recording stops; DOWN alert, repeated daily |
| The Spark is off or offline | external check (section 8); the account holder also checks that the Spark powers on by itself after an outage |
| A training job eats the memory | the kill order of section 5 and the launcher's ceiling; `qwenflash` rule |
| One disk, one owner of the data | daily local backup, scheduled pull to the Zenbook with its own external check, a tried restore |
| The paper user logs in elsewhere | it drops the Spark's session; rule of section 4: one session, one machine |
| Code running as `scura`, or inside an LLM container, reaches the Gateway | the owner rule and "localhost only"; the lasting answer is the security layer |
| The move to the US changes the time zone | the timers name their zone, so nothing moves by itself; job times and the restart window are reviewed that day |

## 11. Sequence, and what the account holder does by hand

1. Independent of this move and before 2026-10-09: the remaining hourly
   backfill run and the universe refresh, on the Zenbook.
2. By hand, once: Tailscale on both machines; the `trading` user, its ssh
   key and `/srv/quant_data`; the owner-rule unit.
3. The spike (section 9A). By hand: the Gateway logout on the Zenbook and
   the login on the Spark.
4. The implementation plan, written from this document and reviewed.
5. The build: repo changes and tests. The running system is not touched.
6. By hand, once: the unit files and the sudoers drop-in; the heartbeat
   account and its four URLs; the credential files of section 5; the
   Zenbook's pull task.
7. Cutover evening (section 6). By hand: disabling the Zenbook tasks, the
   logout on the Zenbook, the login on the Spark.
8. The first week, the restore check, acceptance, unregistering the Zenbook
   tasks.
9. Afterwards: the security layer, with its own spec.

## 12. Invariants after cutover

What happens to each invariant of the Option B design (its section 2), and
the mechanism that enforces the new one.

| Option B invariant | After cutover |
| --- | --- |
| 1. "The Spark cannot reach the Gateway." | **Replaced.** On the Spark, the `trading` user's processes reach the Gateway and nothing else does: the compose file binds the port to 127.0.0.1, the Gateway keeps "localhost only", the owner rule rejects every other user, `ufw` allows ssh only. |
| 2. "Data flows one way", Zenbook to Spark, and "the operational `models/gtm_emos.json` and `paper/gtm_signals.json` are produced on the Zenbook only." | **Reversed.** `/srv/quant_data` on the Spark is the one live root. The operational model and the signals are produced there only, by `trading`. Data flows from it to the two mirrors; `rollback.sh` is the single, deliberate exception. Enforced by the mirror marker: live-writing entry points refuse a marked root, the copy scripts refuse an unmarked one. |
| 3. Same default data root, `~/quant_data`, no env-var override. | **Stands.** For `trading` it is a symlink to `/srv/quant_data`, which the invariant already allowed. |
| 4. "No secrets on the Spark." | **Replaced.** Secrets on the Spark live only in the files of section 5, owned by `trading`, mode 600, outside git; the health unit alone loads the Telegram token. No IBKR password is at rest anywhere on the Spark: the compose environment leaves the credential fields empty, and a test fails if a file under `deploy/` gives them a value. |
| 5. "Scripts that must never run on the Spark." | **Replaced.** Those scripts run on the Spark only, as `trading`. They no longer run on the Zenbook (its root carries the marker) nor as `scura` (no Gateway settings in the research clone's `.env`; the owner rule). |

Unchanged from elsewhere: one process per client id
(`src/venues/ibkr/clients.py`), and no LLM in the pricing or ordering path
(`tests/test_architecture/test_llm_boundary.py`). New with this move: the
Gateway session is on one machine at a time (section 4).

## 13. Assumptions to confirm

- A1. The image runs on the Spark (aarch64, Docker 29.6) with IBC and Xvfb.
  Spike check 1.
- A2. Empty credentials leave the login dialog up for a manual login. Check 2.
- A3. The paper login asks for no second factor, as web sources say; the
  Zenbook's re-login of 2026-09-28 did get an IBKR Mobile prompt
  (`docs/ibkr_runbook.md` section 4). The spike records which is true. With a
  manual login (D2) either is workable: a prompt only means the phone must be
  at hand for the weekly login. Check 3.
- A4. The image's paper API port can be published as `127.0.0.1:4002` with
  the Gateway's "localhost only" on; the exact container port and
  environment variable names are read from the image's documentation during
  the spike and recorded in its report.
- A5. The image's auto-restart keeps the session through the day. Check 6.
- A6. The owner rule sees the real connecting user for traffic to a
  published container port, so it admits `trading` and rejects `scura` and
  the host-network containers. Check 7.
- A7. The Spark stays on and connected around the clock and powers on by
  itself after an outage (the account holder confirms the firmware setting).
- A8. The account holder opens the heartbeat account and installs Tailscale.

## 14. Repo changes this design implies

New: `deploy/spark/` (unit files, the Gateway compose file, the owner-rule
unit, the sudoers drop-in), `scripts/spark/deploy.sh`,
`scripts/spark/pull_snapshot.sh`, `scripts/spark/mirror_live.sh`,
`scripts/spark/rollback.sh`, `scripts/spark/restore_check.py`, the session
check, the ping wrapper, the research launcher,
`monitoring/backup_paper_book.py`, the registration script for the Zenbook's
pull task, a cutover check that evaluates the gate of section 6, and the
tests of section 9B. Changed: the live-writing entry points (the
mirror-marker refusal), `docs/spark_runbook.md` and `docs/ibkr_runbook.md`
(new roles, login, deploy, restore, rollback), `scripts/scheduling/README.md`,
`docs/MIGRATION_CHECKPOINT.md`, and an amendment note in the Option B design
pointing here. The Windows launchers and task registration scripts stay until
the Zenbook tasks are unregistered.

## 15. The nine review points of 2026-10-05, and where each is met

1. One Gateway session; sequential cutover and rollback; never both machines
   connected -- section 4 ("One session, one machine"), section 6 (cutover
   step 2, rollback), the session check in sections 9B and 14.
2. Loopback isolation: a separate Linux user, the owner rule on 4002, the
   Gateway's "localhost only" -- D7, D9, sections 3, 4 and 7.
3. Kill order trading, then the LLM, then research -- D10, section 5. No
   change to the `~/stack` compose file is needed, so none is proposed.
4. A copy off the machine and a tried restore -- D11, section 6, the
   `offbox-pull` check of section 8, the acceptance criteria of section 9D.
5. The spike first: the Gateway on aarch64 with IBC and Xvfb, VNC over
   Tailscale -- section 9A; Tailscale moved ahead of it in section 11.
6. Schedules as systemd timers with an explicit time zone -- D12, section 5.
7. Credential files: locations, permissions, out of git -- section 5.
8. The changed invariants written out, the operational-model one included --
   section 12.
9. Who reports a silent Spark: a check from outside -- D8, section 8.
