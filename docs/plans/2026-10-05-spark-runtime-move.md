# Spark Runtime Move Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move IB Gateway and the live runtime from the Zenbook to the DGX Spark, under a Linux user of its own, with a tried restore and a rollback, without changing what the system trades.

**Architecture:** The Gateway runs in a digest-pinned container whose API and VNC ports are bound to the Spark's loopback. The recorder, the health probe, the paper sync, the label collector and a backup run as systemd system units of the user `trading`, from a runtime clone that receives `main` only; an `iptables` owner rule admits only `trading` to the API port. The live root is `/srv/quant_data`; the research clone's root and the Zenbook's root become mirrors that carry a marker file, which every live-writing entry point refuses.

**Tech Stack:** Python 3.14, pytest, ruff; bash and rsync (inside WSL Ubuntu on the Zenbook); systemd, Docker Compose and iptables on the Spark; Tailscale; a healthchecks-style external heartbeat.

**Spec:** `docs/plans/2026-10-05-spark-runtime-move-design.md`

## Global Constraints

- Commits and PRs carry no `Co-Authored-By` trailer and no tool footer; the account holder is the sole author.
- Account ids, tokens, ping URLs and passwords never enter git, a test fixture or a report. They travel over ssh. A report that must mention an account names it as "the paper account".
- `~/stack` on the Spark is never touched: no edit of its compose file, no `docker compose pull`, no `apt upgrade`.
- Until cutover the main checkout `C:\Users\asus\OneDrive\Desktop\new_work` is the live runtime: never switch its branch. Build on branch `spark-runtime-move` in the worktree `.claude/worktrees/spark-research-split`; land with `git -C <main checkout> merge --ff-only`.
- Steps marked **(account holder)** are done by the account holder and nobody else: every `sudo` command, every Gateway login and logout, every image pull and first container start, Tailscale, the heartbeat account, enabling and disabling Windows scheduled tasks. The executor prepares the exact command, hands it over and waits for the outcome.
- One Gateway session, one machine: before a login on one machine, the other is logged out and `scripts/spark/session_check.py` passes there.
- Unit files: `User=trading`, `Group=trading`, `UMask=0027`, `NoNewPrivileges=yes`, `OOMScoreAdjust=-900`. Timers: `Persistent=true`, and `OnCalendar` ends with ` Europe/Istanbul`.
- Kill order: Gateway container and runtime units `-900`; `qwenflash` stays `-500`; the research launcher sets `+500` and `MemoryMax=15G` unless told otherwise.
- Published ports: API `127.0.0.1:4002`, VNC `127.0.0.1:5900`. Nothing else.
- Paths on the Spark: runtime clone `/home/trading/forecast-model`; live root `/srv/quant_data` with `/home/trading/quant_data` a symlink to it; backups `/srv/quant_data_backups`; Gateway project `/home/scura/ibgw`. Marker file name: `.mirror-of-spark`.
- Credential files, each mode 600: `/home/trading/forecast-model/.env`, `/home/trading/.config/forecast-model/alerts.env`, `/home/trading/.config/forecast-model/heartbeat.env`, `/home/trading/.cdsapirc` (all owned by `trading`), `/home/scura/ibgw/.env` (owned by `scura`).
- Tests first. The home redirect of `tests/conftest.py` is the only way a test touches a home directory. `python -m ruff check .` is clean and the full suite is green on both machines before cutover.
- Files under `deploy/` and `scripts/spark/*.sh` are LF in every checkout (`.gitattributes`).
- Every other tracked text file is CRLF in the Zenbook's working tree (`core.autocrlf` is on). An edit keeps the file's line endings: `git diff --stat` shows only the lines a step changed, never a whole file.
- No venue-touching script is run outside the steps that say so. The health probe is never hand-run with non-default options without scratch `--alert-state-path` and `--health-log-path` files.
- A command whose output can carry the account id (the session line `account=...` of the recorder and of the paper scripts, a line of the sync ledger) is run with `grep -v "account="` in front of anything that is shown or kept.
- A shell script is started as `bash scripts/spark/<name>` on every machine: the checkout carries no executable bits.
- A Git Bash block that uses `$SSH`, `$SCP` or `$REPO_WSL` needs `source scripts/spark/env.sh` in the same shell. An executor whose shell does not last from one call to the next puts that line first in every block and takes no variable from an earlier block: a value that must survive (the cutover's evidence directory, `SINCE`) is kept in a file.
- `wsl.exe -d Ubuntu -- ...` does not keep argument boundaries: use it with plain words only (a script path, flags, `env NAME=value`), never with `bash -c '...'`. A bare POSIX path or `NAME=/path` among those words needs `MSYS_NO_PATHCONV=1` in front when the line is typed in Git Bash.
- Dated artifacts (`docs/spark_runtime_spike_<date>.md`, `docs/spark_runtime_cutover_<date>.md`) take the date of the run that produces them.

## File Structure

| Path | Responsibility | Task |
| --- | --- | --- |
| `docs/spark_runtime_spike_<date>.md` | what the spike proved: image digest, variable names, pass or fail per check | 0 |
| `src/utils/live_root.py` | the mirror marker: name, test, refusal | 1 |
| `climate/paths.py` (modify) | `root_path()`: the climate root without creating it | 1 |
| eight entry points under `scripts/` (modify) | refuse a marked root before writing | 1 |
| `scripts/spark/session_check.py` | is a Gateway listening or running on this machine | 2 |
| `scripts/spark/heartbeat_ping.py` | run a command, ping a URL from the environment, never print it | 3 |
| `monitoring/backup_paper_book.py` | daily copy of the operational files with a manifest, 30 kept | 4 |
| `scripts/spark/restore_check.py` | restore a copy into a scratch root and verify it | 5 |
| `deploy/spark/systemd/*.service`, `*.timer` | the runtime's units and the owner-rule unit | 6 |
| `deploy/spark/sudoers/forecast-runtime` | lets `scura` start and stop the nine units and restart the recorder only | 6 |
| `deploy/spark/ibgw/docker-compose.yml`, `env.example` | the Gateway project, pinned by the digest the spike tried | 0 |
| `.gitattributes` (modify) | `deploy/**` is LF | 6 (or 0, whichever lands first) |
| `scripts/spark/deploy.sh` | push `main` to the runtime clone, restart the recorder after a sweep | 7 |
| `scripts/spark/pull_snapshot.sh` | Spark live root to the Zenbook mirror | 7 |
| `scripts/spark/mirror_live.sh` | live root to the research mirror, on the Spark | 7 |
| `scripts/spark/rollback.sh` | Spark back to the Zenbook's kept root | 7 |
| `scripts/spark/research_run.sh` | the research launcher: memory ceiling and kill priority | 7 |
| `scripts/spark/push_snapshot.sh` (modify) | refuses a source that is a mirror; `--cutover`, `--dry-run` | 7 |
| `scripts/scheduling/run_spark_pull.ps1`, `register_spark_pull_task.ps1` | the Zenbook's scheduled pull and its ping | 8 |
| `scripts/scheduling/unregister_ibkr_tasks.ps1` (modify) | removes all three `\IBKR\` tasks, not two | 8 |
| `scripts/scheduling/swap_live_root_for_mirror.ps1` | the checked swap of the Zenbook's live root for an empty, marked mirror, and back | 8 |
| `scripts/spark/cutover_check.py` | evaluates the cutover gate | 9 |
| `docs/spark_runbook.md`, `docs/ibkr_runbook.md`, `scripts/scheduling/README.md`, `docs/MIGRATION_CHECKPOINT.md`, the Option B design (modify) | the new roles, login, deploy, restore, rollback | 10, then 12 and 13 |
| `docs/spark_runtime_cutover_<date>.md` | the cutover's evidence | 12 |

Tests live beside their kind: `tests/test_utils/test_live_root.py`, the entry-point refusals in `tests/test_architecture/test_mirror_refusal.py`, `tests/test_spark/` for the scripts under `scripts/spark/`, `tests/test_monitoring/test_backup_paper_book.py`, `tests/test_deploy/` for the unit files, the compose file, the shell scripts and the PowerShell scripts, `tests/test_architecture/test_runbook_names.py` for the runbook.

Order and dependencies: Task 0 (by hand, then the spike) decides go or stop and, on go, commits the Gateway project pinned to the image it tried. Tasks 1 to 10 need nothing from the Spark and can be built while Task 0 waits for the account holder; among them Task 7 needs Task 6 (the unit names and the sudoers file), Task 8 needs Tasks 3 and 7, and Task 10 documents what the others built. Tasks 11 to 13 are the installation, the cutover and the first week; they need Task 0's GO and Tasks 1 to 10.

---

### Task 0: By-hand prerequisites, then the spike

The spike answers one question: does IB Gateway run on the Spark (aarch64) with IBC and a virtual display, and can the account holder log in to it through VNC over Tailscale? It leaves nothing running. During it the Gateway session moves to the Spark and back; the session is never on both machines. Whenever the spike ends early, for whatever reason, the container is stopped and checked as in step 15 before anything else, and before the Zenbook logs in again.

**Files:**
- Create: `docs/spark_runtime_spike_<date>.md`
- Create on the Spark, outside git: `/home/scura/ibgw/docker-compose.yml`, `/home/scura/ibgw/.env`
- Create, on GO: `deploy/spark/ibgw/docker-compose.yml`, `deploy/spark/ibgw/env.example`
- Modify, on GO: `.gitattributes` (unless Task 6 has done it)
- Test: `tests/test_deploy/test_gateway_project.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the image digest (`ghcr.io/gnzsnz/ib-gateway@sha256:...`) and the confirmed environment variable names and container ports, recorded in the spike report and pinned in `deploy/spark/ibgw/docker-compose.yml`, which Task 11 installs. The runtime clone `/home/trading/forecast-model` with its `.venv`, the git remote `spark-rt`, and the ssh shortcut `spark-rt`; Tasks 7, 11 and 12 use them.

- [ ] **Step 1 (account holder): Tailscale on both machines**

Found in place on 2026-10-06, checked from the Zenbook: Tailscale runs on both machines in one tailnet, `Host spark` in `C:\Users\asus\.ssh\config` names the Spark's Tailscale host (`spark-8f47`), and a second entry, `Host spark-lan`, keeps the LAN address. What is left:

1. In the Tailscale admin console, switch key expiry off for the Spark node; otherwise it leaves the tailnet after 180 days.
2. On the Spark, let ssh in on the Tailscale interface by a rule of its own, so that reaching the Spark does not depend on the LAN rule:

```bash
sudo ufw allow in on tailscale0 to any port 22 proto tcp
sudo ufw status | grep -i tailscale0
```

3. Add the second shortcut to `C:\Users\asus\.ssh\config` (it starts to work when step 2 has created the user):

```
Host spark-rt
    HostName spark-8f47
    User trading
    IdentityFile <the same line as under Host spark>
    IdentitiesOnly yes
```

4. A VNC viewer on the Zenbook (TigerVNC's viewer, for example), installed and started once. None is installed today, and it is first needed in step 9, when the Zenbook's Gateway is already logged out: that is not the moment to look for one.

Expected: `tailscale status` on each machine lists the other; `ssh spark hostname` prints `spark-8f47` with the laptop on a phone hotspot (off the home LAN).

- [ ] **Step 2 (account holder): the `trading` user and the live root**

On the Spark:

```bash
sudo useradd --create-home --shell /bin/bash trading
sudo usermod -aG trading scura
sudo install -d -o trading -g trading -m 2750 /srv/quant_data /srv/quant_data_backups
sudo -u trading ln -s /srv/quant_data /home/trading/quant_data
sudo install -d -o trading -g trading -m 700 /home/trading/.ssh
sudo install -o trading -g trading -m 600 /home/scura/.ssh/authorized_keys /home/trading/.ssh/authorized_keys
sudo install -m 755 /home/scura/.local/bin/uv /usr/local/bin/uv
```

`trading` has no password and is not added to the `docker` group. `scura` must log out and in once for the new group to apply.

- [ ] **Step 3: Verify the user from the Zenbook**

Run (Git Bash, from the worktree, after `source scripts/spark/env.sh`):

```bash
"$SSH" spark-rt 'id -un; id -Gn; readlink ~/quant_data; stat -c "%U:%G %a" /srv/quant_data /srv/quant_data_backups; command -v uv'
"$SSH" spark 'id -Gn | tr " " "\n" | grep -x trading; ls /srv/quant_data >/dev/null && echo "scura can read the live root"; touch /srv/quant_data/x 2>&1 | tail -n 1'
```

Expected: first command prints `trading`, a group list without `docker` and without `sudo`, `/srv/quant_data`, `trading:trading 2750` twice, `/usr/local/bin/uv`. Second prints `trading`, `scura can read the live root`, and a `Permission denied` line from `touch`.

- [ ] **Step 4: The runtime clone and its venv**

```bash
"$SSH" spark-rt 'git init -q forecast-model && git -C forecast-model config receive.denyCurrentBranch updateInstead'
git remote add spark-rt spark-rt:forecast-model
git push spark-rt main
"$SSH" spark-rt 'cd forecast-model && git checkout -q main && git log --oneline -1'
"$SSH" spark-rt 'cd forecast-model && uv python install 3.14.2 && uv venv --python 3.14.2 .venv && uv pip install --python .venv/bin/python -e ".[dev,ibkr,climate,mcp,web,trading]" -c constraints/zenbook-2026-10-03.txt 2>&1 | tail -n 3'
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python -m pytest -q 2>&1 | tail -n 1 && .venv/bin/python -m ruff check . | tail -n 1'
```

Expected: the log line is the Zenbook's `main` head; the suite line reads the same count as on the Zenbook's `main` (`N passed, 1 skipped`); `All checks passed!`. The repo's own `core.sshCommand` already points git at the Windows ssh client.

- [ ] **Step 5: The Gateway project for the spike**

Create `/home/scura/ibgw/docker-compose.yml` (over `"$SSH" spark`, with `mkdir -p ~/ibgw/jts`):

```yaml
name: ibgw
services:
  ibgw:
    image: ghcr.io/gnzsnz/ib-gateway:stable
    container_name: ibgw
    restart: unless-stopped
    mem_limit: 4g
    oom_score_adj: -900
    environment:
      TWS_USERID: ${TWS_USERID:-}
      TWS_PASSWORD: ${TWS_PASSWORD:-}
      TRADING_MODE: paper
      READ_ONLY_API: "no"
      TWS_ACCEPT_INCOMING: reject
      EXISTING_SESSION_DETECTED_ACTION: manual
      TWOFA_TIMEOUT_ACTION: restart
      AUTO_RESTART_TIME: ${AUTO_RESTART_TIME:-11:45 PM}
      TIME_ZONE: Europe/Istanbul
      TZ: Europe/Istanbul
      TWS_SETTINGS_PATH: /home/ibgateway/tws_settings
      VNC_SERVER_PASSWORD: ${VNC_SERVER_PASSWORD:?set VNC_SERVER_PASSWORD in .env}
    volumes:
      - ./jts:/home/ibgateway/tws_settings
    ports:
      - "127.0.0.1:4002:4004"
      - "127.0.0.1:5900:5900"
    healthcheck:
      test: ["CMD-SHELL", "bash -c 'exec 3<>/dev/tcp/127.0.0.1/4004'"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 180s
```

`restart: unless-stopped` brings the container back after a crash or a reboot, but not after a deliberate `docker compose stop`: the kill switch and a rollback stay in force across a reboot. `TWS_ACCEPT_INCOMING: reject` is the Gateway's "allow connections from localhost only": a connection that does not come from the container's own loopback is refused instead of raising a dialog. `EXISTING_SESSION_DETECTED_ACTION: manual` keeps IBC from taking a session over on its own. The container port `4004` and every variable name above are the image's documented ones (README of `gnzsnz/ib-gateway-docker`, read 2026-10-05); if the running image disagrees, the spike report records what is true and step 19 follows the report.

The account holder creates `/home/scura/ibgw/.env` with `chmod 600`:

```
VNC_SERVER_PASSWORD=<a password of their choosing, not an IBKR one>
TWS_USERID=
TWS_PASSWORD=
AUTO_RESTART_TIME=<twenty-five minutes after the planned login, as "hh:mm AM" or "hh:mm PM">
```

- [ ] **Step 6 (account holder): the owner rule, by hand for the spike**

```bash
sudo iptables -I OUTPUT -o lo -p tcp --dport 4002 -m owner ! --uid-owner trading -j REJECT --reject-with tcp-reset
sudo iptables -I OUTPUT -o lo -p tcp --dport 5900 -m owner ! --uid-owner scura -j REJECT --reject-with tcp-reset
sudo iptables -S OUTPUT | grep -E "dport (4002|5900)"
```

Expected: the two rules are listed. They do not survive a reboot; Task 6 builds the unit that makes them permanent.

- [ ] **Step 7: Move the session off the Zenbook**

**(account holder)** log out of the Zenbook's Gateway and close it. Then, on the Zenbook (PowerShell):

```powershell
Get-NetTCPConnection -LocalPort 4002 -State Listen -ErrorAction SilentlyContinue | Measure-Object | ForEach-Object Count
Get-Process | Where-Object ProcessName -match 'ibgateway' | Measure-Object | ForEach-Object Count
```

Expected: `0` and `0`. Nothing below starts until both are zero. The Zenbook's recorder keeps retrying and the hourly probe will send one DOWN message and later one recovery message; both are expected.

- [ ] **Step 8 (account holder): pull and start the container**

```bash
cd ~/ibgw && docker compose pull && docker compose up -d
docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/gnzsnz/ib-gateway:stable
```

Record the printed digest; it goes into the report and into step 19.

- [ ] **Step 9: Checks 1 and 2 -- the image runs, the remote screen opens**

```bash
"$SSH" spark 'docker ps --filter name=ibgw --format "{{.Status}}"; docker stats --no-stream --format "{{.MemUsage}}" ibgw; uname -m'
```

Pass (check 1): the status says `Up` and, after the start period, `(healthy)`; memory is under 4 GiB; `aarch64`.

Read back what is published, from both sides (Git Bash, then PowerShell on the Zenbook):

```bash
"$SSH" spark 'ss -ltnH | grep -E ":(4002|5900) "'
```

```powershell
(Test-NetConnection spark-8f47 -Port 4002 -WarningAction SilentlyContinue).TcpTestSucceeded
(Test-NetConnection spark-8f47 -Port 5900 -WarningAction SilentlyContinue).TcpTestSucceeded
```

Pass: two listening sockets, both on `127.0.0.1` (none on `0.0.0.0`, `[::]` or another address), and `False` twice from the Zenbook. Docker publishes ports past `ufw`, so this read-back, not the firewall, is what shows that nothing is open to the tailnet or the LAN.

**(account holder)** on the Zenbook, with the laptop on Tailscale: `ssh -L 5900:127.0.0.1:5900 spark`, then a VNC client on `localhost:5900` with the screen password.

Pass (check 2): the Gateway's login dialog is on the screen with empty user and password fields. If instead the container exits or restarts in a loop because the credentials are empty, stop here: this is assumption A2 failing, and the report says so (fallbacks: section 4 of the spec).

- [ ] **Step 10 (account holder): check 3 -- the manual paper login**

Type the login into the dialog the way it is done on the Zenbook (the live user name with the Paper Trading toggle; `docs/ibkr_runbook.md` section 4), and accept the paper-trading disclaimer before anything connects.

Pass: the Gateway's main window appears. Record in the report whether the phone was asked for a second factor: web sources say a paper login needs none, the Zenbook's re-login of 2026-09-28 did get a prompt (assumption A3). With a manual login either answer works; a prompt only means the phone must be at hand for the weekly login.

- [ ] **Step 11: Checks 4 and 5 -- the API answers `trading`, one sweep lands**

```bash
"$SSH" spark-rt 'cd forecast-model && IBKR_PORT=4002 IBKR_TRADING_MODE=paper .venv/bin/python -c "
from src.venues.ibkr.clients import connect_ib
from src.venues.ibkr.settings import IBKRSettings
ib = connect_ib(\"research\", IBKRSettings())
print(\"server time:\", ib.reqCurrentTime())
ib.disconnect()
"'
"$SSH" spark-rt 'cd forecast-model && IBKR_PORT=4002 IBKR_TRADING_MODE=paper IBKR_DATA_ROOT=/home/trading/spike_ticks .venv/bin/python scripts/record_ibkr_ticks.py --once 2>&1 | grep -v "account=" | tail -n 2'
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python -c "
import glob, pyarrow.parquet as pq
files = glob.glob(\"/home/trading/spike_ticks/quotes/date=*/*.parquet\")
print(len(files), \"file(s),\", sum(pq.read_metadata(f).num_rows for f in files), \"rows\")
"'
```

Pass (check 4): a `server time:` line with today's date. Pass (check 5): `1 file(s), N rows` with N within ten percent of the Zenbook's last sweep (about 2,400). `IBKR_DATA_ROOT` is set here and only here, so the sweep cannot land in the live root.

- [ ] **Step 12: Check 7 -- the owner rule, from three sides**

```bash
"$SSH" spark-rt 'timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/4002" && echo "trading: connected"'
"$SSH" spark 'timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/4002"; echo "scura: rc=$?"'
"$SSH" spark 'docker exec openwebui bash -c "timeout 3 bash -c \"exec 3<>/dev/tcp/127.0.0.1/4002\"; echo container: rc=\$?"'
```

Pass: `trading: connected`; `scura: rc=1`; `container: rc=1`. (`openwebui` is the Open WebUI container's name as `docker ps` printed it on 2026-10-06; if it has changed, use the name it prints now.) If `scura` or the container connects, assumption A6 fails: stop the container first (the first command of step 15), because a logged-in Gateway that accepts orders is then reachable by them, and only then report.

- [ ] **Step 13: Check 9 -- the kill order is what the spec says**

```bash
"$SSH" spark 'for c in ibgw qwenflash; do pid=$(docker inspect -f "{{.State.Pid}}" "$c"); echo "$c $(cat /proc/$pid/oom_score_adj)"; done'
```

Pass: `ibgw -900` and `qwenflash -500`.

- [ ] **Step 14: Check 6 -- the auto-restart keeps the session**

Wait for the `AUTO_RESTART_TIME` set in step 5. Two minutes after it:

```bash
"$SSH" spark-rt 'cd forecast-model && IBKR_PORT=4002 IBKR_TRADING_MODE=paper .venv/bin/python -c "
from src.venues.ibkr.clients import connect_ib
from src.venues.ibkr.settings import IBKRSettings
ib = connect_ib(\"research\", IBKRSettings())
print(\"after restart:\", ib.reqCurrentTime(), ib.managedAccounts() != [])
ib.disconnect()
"'
```

Pass: an `after restart:` line ending in `True`, with no login typed since step 10.

- [ ] **Step 15: Check 8 -- the kill switch, and ending the spike**

```bash
"$SSH" spark 'cd ~/ibgw && docker compose stop && docker ps --filter name=ibgw --format "{{.Status}}" | wc -l'
"$SSH" spark-rt 'timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/4002"; echo "after stop: rc=$?"; rm -rf /home/trading/spike_ticks'
"$SSH" spark 'ss -ltn | grep -c ":4002 "'
```

Pass: `0` running containers, `after stop: rc=1`, `0` listeners.

What a start after the kill switch needs is looked at, not assumed. **(account holder)** `cd ~/ibgw && docker compose up -d`, wait for the start period, and look at the remote screen. Pass: the LOGIN DIALOG with empty fields, not the Gateway's main window. A stopped container does not come back logged in, so starting it can never take the session from another machine by itself; the cutover's order (Task 12 step 4) rests on this. If the main window appears instead, stop the container at once and write a STOP into the report. Then run the three commands above again; same pass. The container stays stopped. Set `AUTO_RESTART_TIME=11:45 PM` in `/home/scura/ibgw/.env` for later. Only now **(account holder)** opens the Zenbook's Gateway and logs in again; the Zenbook recorder reconnects by itself.

- [ ] **Step 16: Write the spike report and commit it**

Create `docs/spark_runtime_spike_<date>.md` with: the date and duration; the image tag and digest from step 8; the Gateway and IBC versions the container logs print (`docker logs ibgw 2>&1 | grep -iE "version|IBC" | head`; read the lines first and paste version lines only: a line that carries a user name or an account id does not go into the report); a table of the nine checks with the command's output line and pass or fail; the environment variable names and the container port that actually worked; the `userland-proxy` setting of Docker (`"$SSH" spark 'cat /etc/docker/daemon.json 2>/dev/null || echo "no daemon.json: the default, true"'`; paste the `userland-proxy` line only), because the owner rule's `-o lo` match and the session check's port leg both depend on docker-proxy listening on the Gateway's port; what a start after the kill switch showed in step 15 (the login dialog, so a manual login); the account holder's answer on assumption A7 (does the Spark power on by itself when power returns, and which firmware setting says so); and the verdict, `GO` or `STOP: <which assumption failed, which fallback>`. No account id, no password, no screen password.

```bash
git add docs/spark_runtime_spike_*.md
git commit -m "Record the Gateway spike on the Spark"
```

If the verdict is STOP, the plan stops here and the account holder decides on a fallback; steps 17 to 20 are not done.

- [ ] **Step 17: Write the failing tests for the Gateway project**

On GO, the compose file that passed the spike goes into the repo, pinned by the digest of step 8, so that what is installed later is exactly what was tried. Create `tests/test_deploy/test_gateway_project.py` (and an empty `tests/test_deploy/__init__.py`, unless Task 6 has created it already):

```python
"""deploy/spark/ibgw: the Gateway's compose project, pinned and closed."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = REPO_ROOT / "deploy" / "spark" / "ibgw"


def _service() -> dict:
    compose = yaml.safe_load((PROJECT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert list(compose["services"]) == ["ibgw"]
    return compose["services"]["ibgw"]


def test_the_image_is_pinned_by_a_digest_and_never_by_a_tag():
    image = _service()["image"]
    assert re.fullmatch(r"ghcr\.io/gnzsnz/ib-gateway@sha256:[0-9a-f]{64}", image), image


def test_the_pinned_digest_is_the_one_the_spike_report_records():
    digest = _service()["image"].split("@", 1)[1]
    reports = sorted((REPO_ROOT / "docs").glob("spark_runtime_spike_*.md"))
    assert reports, "the spike report is missing"
    assert digest in reports[-1].read_text(encoding="utf-8")


def test_only_the_api_and_the_screen_are_published_and_only_on_loopback():
    assert sorted(_service()["ports"]) == ["127.0.0.1:4002:4004", "127.0.0.1:5900:5900"]


def test_the_container_is_protected_and_bounded():
    service = _service()
    assert service["restart"] == "unless-stopped"  # a deliberate stop survives a reboot
    assert service["oom_score_adj"] == -900
    assert service["mem_limit"] == "4g"
    assert service["container_name"] == "ibgw"
    assert "healthcheck" in service


def test_the_session_is_paper_manual_and_closed_to_other_hosts():
    env = _service()["environment"]
    assert env["TRADING_MODE"] == "paper"
    assert env["TWS_ACCEPT_INCOMING"] == "reject"        # "localhost only"
    assert env["EXISTING_SESSION_DETECTED_ACTION"] == "manual"
    assert env["READ_ONLY_API"] == "no"
    assert env["TIME_ZONE"] == "Europe/Istanbul" and env["TZ"] == "Europe/Istanbul"


def test_no_ibkr_credential_is_written_in_the_project():
    env = _service()["environment"]
    assert env["TWS_USERID"] == "${TWS_USERID:-}"
    assert env["TWS_PASSWORD"] == "${TWS_PASSWORD:-}"
    example = (PROJECT / "env.example").read_text(encoding="utf-8")
    assert re.search(r"^TWS_USERID=$", example, flags=re.MULTILINE)
    assert re.search(r"^TWS_PASSWORD=$", example, flags=re.MULTILINE)
    assert re.search(r"^VNC_SERVER_PASSWORD=$", example, flags=re.MULTILINE)


def test_the_settings_survive_a_restart():
    service = _service()
    assert service["environment"]["TWS_SETTINGS_PATH"] == "/home/ibgateway/tws_settings"
    assert service["volumes"] == ["./jts:/home/ibgateway/tws_settings"]
```

- [ ] **Step 18: Run them and watch them fail**

Run: `python -m pytest tests/test_deploy/test_gateway_project.py -q`
Expected: seven failures on the missing `deploy/spark/ibgw/docker-compose.yml`.

- [ ] **Step 19: Write the Gateway project**

`deploy/spark/ibgw/docker-compose.yml` is the file the spike ran (step 5) with one change: the image is named by the digest step 8 printed, in place of the tag. If the spike report says a variable name or the container port differs from what is written here, the report is right: follow it, and adjust the test in the same commit, quoting the report's line.

```yaml
# IB Gateway (paper) for the Spark. Copy to /home/scura/ibgw/docker-compose.yml.
# Nothing here is updated unattended: the image is pinned by digest, and a new version
# is a deliberate change of this file, tried first the way the spike tried this one.
name: ibgw
services:
  ibgw:
    image: ghcr.io/gnzsnz/ib-gateway@sha256:<the 64 hex digits step 8 printed>
    container_name: ibgw
    restart: unless-stopped
    mem_limit: 4g
    oom_score_adj: -900
    environment:
      # Empty on purpose: the account holder types the credentials into the Gateway's
      # own dialog over the remote screen. No IBKR password is at rest on this machine.
      TWS_USERID: ${TWS_USERID:-}
      TWS_PASSWORD: ${TWS_PASSWORD:-}
      TRADING_MODE: paper
      READ_ONLY_API: "no"
      # "Allow connections from localhost only": anything else is refused, no dialog.
      TWS_ACCEPT_INCOMING: reject
      # Never take a session over on our own: one session, one machine.
      EXISTING_SESSION_DETECTED_ACTION: manual
      TWOFA_TIMEOUT_ACTION: restart
      AUTO_RESTART_TIME: ${AUTO_RESTART_TIME:-11:45 PM}
      TIME_ZONE: Europe/Istanbul
      TZ: Europe/Istanbul
      TWS_SETTINGS_PATH: /home/ibgateway/tws_settings
      VNC_SERVER_PASSWORD: ${VNC_SERVER_PASSWORD:?set VNC_SERVER_PASSWORD in .env}
    volumes:
      - ./jts:/home/ibgateway/tws_settings
    ports:
      - "127.0.0.1:4002:4004"
      - "127.0.0.1:5900:5900"
    healthcheck:
      test: ["CMD-SHELL", "bash -c 'exec 3<>/dev/tcp/127.0.0.1/4004'"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 180s
```

`deploy/spark/ibgw/env.example`:

```
# Copy to /home/scura/ibgw/.env and chmod 600. Never commit the copy.
# The screen password is for the remote screen only; it is not an IBKR credential.
VNC_SERVER_PASSWORD=
# Leave both empty: the login is typed into the Gateway's dialog (spec D2).
TWS_USERID=
TWS_PASSWORD=
AUTO_RESTART_TIME=11:45 PM
```

Unless Task 6 has added it already, append to `.gitattributes`, so that the files Docker reads on the Spark are LF in every checkout:

```
# Unit files, the compose file and the sudoers drop-in are read by systemd, Docker and
# sudo on the Spark; they must be LF in every checkout.
deploy/** text eol=lf
```

- [ ] **Step 20: Run them and watch them pass, then commit**

Run: `python -m pytest tests/test_deploy/test_gateway_project.py -q`
Expected: `7 passed`.

```bash
git add .gitattributes deploy/spark/ibgw tests/test_deploy/__init__.py tests/test_deploy/test_gateway_project.py
git commit -m "Pin the Gateway project to the image the spike tried"
```

---

### Task 1: The mirror marker and the refusal to write live state into a mirror

After cutover one root is live and every other `quant_data` is a copy that carries the file `.mirror-of-spark`. Eight entry points write live state; each must refuse a marked root before it creates or changes anything under a data root, and before it touches the venue. (An entry point may have opened its log file in the working directory by then; a log is not live state.) A root is a mirror when it, or any directory above it, carries the marker, so a paper book named by hand deep inside a mirror is refused like the mirror itself.

**Files:**
- Create: `src/utils/live_root.py`
- Modify: `climate/paths.py` (add `root_path`, make `data_root` use it)
- Modify: `scripts/record_ibkr_ticks.py`, `scripts/sync_ibkr_paper.py`, `scripts/run_ibkr_paper.py`, `scripts/run_gtm_paper.py`, `scripts/backfill_ibkr_hourly.py`, `scripts/collect_climate_labels.py`, `scripts/fetch_climate_forecasts.py`, `scripts/fit_gtm_emos.py`
- Modify: `tests/test_climate/test_staleness_gates.py` (its stand-in for the settings gains the attribute the refusal reads)
- Test: `tests/test_utils/__init__.py` (empty), `tests/test_utils/test_live_root.py`, `tests/test_climate/test_root_path.py`, `tests/test_architecture/test_mirror_refusal.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `src.utils.live_root.MIRROR_MARKER: str = ".mirror-of-spark"`; `mirror_marker_of(root: str | Path) -> Path | None`, which looks in `root` and in every directory above it; `refuse_if_mirror(root: str | Path, what: str) -> None` raising `MirrorRootError(RuntimeError)`; `climate.paths.root_path(override: str | Path | None = None) -> Path`. Every refusing entry point prints the error and returns exit status `2`. Tasks 7 and 12 create the marker file by name.

- [ ] **Step 1: Write the failing tests for the marker**

Create `tests/test_utils/__init__.py` (empty) and `tests/test_utils/test_live_root.py`:

```python
"""The mirror marker: which roots are copies, and the refusal to write into one."""

from __future__ import annotations

import pytest

from src.utils.live_root import MIRROR_MARKER, MirrorRootError, mirror_marker_of, refuse_if_mirror


def test_the_marker_name_is_the_one_the_copy_scripts_create():
    assert MIRROR_MARKER == ".mirror-of-spark"


def test_a_root_without_a_marker_is_live(tmp_path):
    root = tmp_path / "quant_data" / "ibkr_ticks"
    root.mkdir(parents=True)
    assert mirror_marker_of(root) is None
    refuse_if_mirror(root, "record quotes")  # does not raise


def test_a_marker_in_the_quant_root_marks_every_lane_under_it(tmp_path):
    quant = tmp_path / "quant_data"
    (quant / "ibkr_ticks").mkdir(parents=True)
    (quant / "climate_data").mkdir()
    marker = quant / MIRROR_MARKER
    marker.write_text("mirror of spark:/srv/quant_data\n", encoding="utf-8")

    assert mirror_marker_of(quant) == marker
    assert mirror_marker_of(quant / "ibkr_ticks") == marker
    assert mirror_marker_of(quant / "climate_data") == marker


def test_a_marker_marks_everything_below_it_however_deep(tmp_path):
    quant = tmp_path / "quant_data"
    quant.mkdir()
    marker = quant / MIRROR_MARKER
    marker.write_text("", encoding="utf-8")
    book = quant / "ibkr_ticks" / "paper" / "archive" / "ibkr_paper.db"  # does not exist

    assert mirror_marker_of(book) == marker
    assert mirror_marker_of(book.parent) == marker


def test_a_marker_beside_a_root_does_not_mark_it(tmp_path):
    (tmp_path / "mirror").mkdir()
    (tmp_path / "mirror" / MIRROR_MARKER).write_text("", encoding="utf-8")
    live = tmp_path / "live" / "ibkr_ticks"
    live.mkdir(parents=True)

    assert mirror_marker_of(live) is None
    refuse_if_mirror(live, "record quotes")  # does not raise


def test_a_directory_with_the_marker_name_is_not_a_marker(tmp_path):
    quant = tmp_path / "quant_data"
    (quant / MIRROR_MARKER).mkdir(parents=True)
    assert mirror_marker_of(quant) is None


def test_the_refusal_names_the_root_and_what_was_refused(tmp_path):
    quant = tmp_path / "quant_data"
    quant.mkdir()
    (quant / MIRROR_MARKER).write_text("", encoding="utf-8")

    with pytest.raises(MirrorRootError) as excinfo:
        refuse_if_mirror(quant / "ibkr_ticks", "record quotes")
    message = str(excinfo.value)
    assert "record quotes" in message and "ibkr_ticks" in message and MIRROR_MARKER in message


def test_looking_for_a_marker_creates_nothing(tmp_path):
    missing = tmp_path / "nowhere" / "quant_data" / "ibkr_ticks"
    assert mirror_marker_of(missing) is None
    assert not (tmp_path / "nowhere").exists()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_utils/test_live_root.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'src.utils.live_root'`.

- [ ] **Step 3: Write `src/utils/live_root.py`**

```python
"""The mirror marker: which data roots are copies, and the refusal to write into one.

After the runtime moved to the Spark (docs/plans/2026-10-05-spark-runtime-move-design.md,
section 6) exactly one root is live -- /srv/quant_data on the Spark -- and every
other ``quant_data`` is a mirror of it that carries a marker file. A program
that writes live state calls :func:`refuse_if_mirror` on the root it is about
to write under, before it creates or changes anything there and before it
talks to the venue. The copy scripts under scripts/spark/ do the opposite
check: they refuse to write into a root that does NOT carry the marker.
"""

from __future__ import annotations

from pathlib import Path

MIRROR_MARKER = ".mirror-of-spark"


class MirrorRootError(RuntimeError):
    """The root is a mirror of the live root; live state must not be written into it."""


def mirror_marker_of(root: str | Path) -> Path | None:
    """The marker file that makes ``root`` part of a mirror, or None when it is not.

    A mirror is a quant-data root that carries the marker, and everything
    below it: the marker is looked for in ``root`` and in every directory
    above it. So a lane (``ibkr_ticks``, ``climate_data``), a ``paper``
    directory and a file named deep inside a mirror are all recognised.
    ``root`` need not exist, and nothing is created.
    """
    root = Path(root).expanduser().absolute()
    for directory in (root, *root.parents):
        candidate = directory / MIRROR_MARKER
        if candidate.is_file():
            return candidate
    return None


def refuse_if_mirror(root: str | Path, what: str) -> None:
    """Raise :class:`MirrorRootError` when ``root`` is a mirror. ``what`` completes
    the sentence "refusing to ..." in the message."""
    marker = mirror_marker_of(root)
    if marker is not None:
        raise MirrorRootError(
            f"refusing to {what}: {Path(root).expanduser()} is a mirror of the live root "
            f"(marker {marker}). Live state is written on the Spark only, by the trading user."
        )
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/test_utils/test_live_root.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Write the failing test for `root_path`**

Create `tests/test_climate/test_root_path.py`:

```python
"""climate.paths.root_path: the root data_root would return, without creating it."""

from __future__ import annotations

from pathlib import Path

from climate.paths import ENV_VAR, data_root, root_path


def test_root_path_follows_the_override_then_the_variable_then_the_home(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert root_path() == Path.home() / "quant_data" / "climate_data"

    monkeypatch.setenv(ENV_VAR, str(tmp_path / "from_env"))
    assert root_path() == (tmp_path / "from_env").resolve()
    assert root_path(str(tmp_path / "named")) == (tmp_path / "named").resolve()
    assert root_path("   ") == (tmp_path / "from_env").resolve()  # a blank override is no override


def test_root_path_creates_nothing_and_data_root_agrees_with_it(tmp_path):
    target = tmp_path / "climate_data"
    assert root_path(str(target)) == target.resolve()
    assert not target.exists()

    assert data_root(str(target)) == root_path(str(target))
    assert (target / "labels").is_dir() and (target / "raw").is_dir()
```

- [ ] **Step 6: Run it and watch it fail**

Run: `python -m pytest tests/test_climate/test_root_path.py -q`
Expected: `ImportError: cannot import name 'root_path' from 'climate.paths'`.

- [ ] **Step 7: Add `root_path` to `climate/paths.py`**

Replace the function `data_root` with these two functions (`warn_if_onedrive`, below it, stays as it is):

```python
def root_path(override: str | Path | None = None) -> Path:
    """The climate data directory :func:`data_root` returns, without creating it.

    Priority:
      1. explicit override argument
      2. CLIMATE_DATA_ROOT environment variable
      3. ~/quant_data/climate_data
    """
    if override is not None and str(override).strip():
        return Path(override).expanduser().resolve()
    if os.environ.get(ENV_VAR, "").strip():
        return Path(os.environ[ENV_VAR]).expanduser().resolve()
    return Path.home() / "quant_data" / "climate_data"


def data_root(override: str | Path | None = None) -> Path:
    """Return the local climate data directory, creating it if needed (see :func:`root_path`)."""
    root = root_path(override)
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("labels", "raw"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
```

Run: `python -m pytest tests/test_climate/test_root_path.py tests/test_climate -q`
Expected: all pass (the two new tests and every existing climate test).

- [ ] **Step 8: Write the failing test for the eight entry points**

Create `tests/test_architecture/test_mirror_refusal.py`:

```python
"""Every entry point that writes live state refuses a root that carries the mirror marker.

Each case booby-traps the first thing the entry point would do after the
refusal (build the venue client, create the data root, start the fit), so a
missing refusal fails here without reaching the Gateway, the network or the
disk -- and cannot hang in a reconnect loop. The same trap, sprung on a root
that is NOT marked, is the control: the refusal depends on the marker and on
nothing else.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from src.utils.live_root import MIRROR_MARKER


class ReachedTheWork(AssertionError):
    """The entry point went past the point where it had to refuse."""


def _trap(*_args, **_kwargs):
    raise ReachedTheWork("the entry point did not refuse the marked root")


# (module, argv, attribute the refusal must come before)
CASES = [
    ("scripts.record_ibkr_ticks", ["--once"], "QuoteRecorder"),
    ("scripts.sync_ibkr_paper", [], "IBKRAdapter"),
    ("scripts.run_ibkr_paper", ["--simulate"], "data_root"),
    ("scripts.run_gtm_paper", [], "data_root"),
    ("scripts.backfill_ibkr_hourly", ["--dry-run", "--port", "4002", "--mode", "paper"],
     "ContractResolver"),
    ("scripts.collect_climate_labels", [], "data_root"),
    ("scripts.fetch_climate_forecasts", ["--pilot"], "climate_data_root"),
    ("scripts.fit_gtm_emos", ["--trend", "--live"], "fit_gtm"),
]
IDS = [case[0].split(".")[-1] for case in CASES]


def _wire(monkeypatch, tmp_path, default_root) -> None:
    """Make ``default_root`` both lanes' default root, with a paper session's settings."""
    monkeypatch.setenv("IBKR_DATA_ROOT", str(default_root / "ibkr_ticks"))
    monkeypatch.setenv("CLIMATE_DATA_ROOT", str(default_root / "climate_data"))
    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.setenv("IBKR_TRADING_MODE", "paper")
    monkeypatch.chdir(tmp_path)  # no repo .env, no logs/ in the repo


@pytest.fixture
def marked_root(tmp_path, monkeypatch):
    """A quant-data root that is a mirror, wired in as both lanes' default root."""
    quant = tmp_path / "quant_data"
    quant.mkdir()
    (quant / MIRROR_MARKER).write_text("mirror of spark:/srv/quant_data\n", encoding="utf-8")
    _wire(monkeypatch, tmp_path, quant)
    return quant


@pytest.fixture
def live_root_and_mirror(tmp_path, monkeypatch):
    """An unmarked root wired in as the default, and a marked one beside it."""
    live = tmp_path / "live" / "quant_data"
    live.mkdir(parents=True)
    mirror = tmp_path / "mirror" / "quant_data"
    mirror.mkdir(parents=True)
    (mirror / MIRROR_MARKER).write_text("mirror of spark:/srv/quant_data\n", encoding="utf-8")
    _wire(monkeypatch, tmp_path, live)
    return live, mirror


def _booby_trap(module_name: str, trapped: str, monkeypatch):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, trapped, _trap)
    monkeypatch.setattr(module, "setup_logging", lambda *a, **k: None, raising=False)
    return module


def _run(module_name: str, argv: list[str], monkeypatch) -> int:
    module = importlib.import_module(module_name)
    monkeypatch.setattr(sys, "argv", [module_name, *argv])
    try:
        # record_ibkr_ticks.main() takes no argv; the others take a list
        result = module.main() if module_name.endswith("record_ibkr_ticks") else module.main(argv)
    except SystemExit as exc:
        result = exc.code
    return int(result)


@pytest.mark.parametrize(("module_name", "argv", "trapped"), CASES, ids=IDS)
def test_the_entry_point_refuses_a_mirror_before_doing_anything(
        module_name, argv, trapped, marked_root, monkeypatch, capsys):
    _booby_trap(module_name, trapped, monkeypatch)

    rc = _run(module_name, argv, monkeypatch)
    captured = capsys.readouterr()

    assert rc == 2
    assert "mirror of the live root" in captured.out + captured.err
    assert sorted(p.name for p in marked_root.rglob("*")) == [MIRROR_MARKER]  # nothing was created


@pytest.mark.parametrize(("module_name", "argv", "trapped"), CASES, ids=IDS)
def test_the_same_entry_point_goes_to_work_on_a_root_without_the_marker(
        module_name, argv, trapped, live_root_and_mirror, monkeypatch):
    """The control: without the marker the trap is reached, so the refusal is not unconditional."""
    _booby_trap(module_name, trapped, monkeypatch)

    with pytest.raises(ReachedTheWork):
        _run(module_name, argv, monkeypatch)


def test_a_paper_book_named_by_hand_inside_a_mirror_is_refused_however_deep(
        live_root_and_mirror, monkeypatch, capsys):
    _live, mirror = live_root_and_mirror
    sync_cli = _booby_trap("scripts.sync_ibkr_paper", "IBKRAdapter", monkeypatch)
    book = mirror / "ibkr_ticks" / "paper" / "archive" / "ibkr_paper.db"

    assert sync_cli.main(["--db", str(book)]) == 2
    assert "mirror of the live root" in capsys.readouterr().err


def test_placing_orders_refuses_a_book_or_a_climate_root_inside_a_mirror(
        live_root_and_mirror, monkeypatch, capsys):
    _live, mirror = live_root_and_mirror
    paper_cli = _booby_trap("scripts.run_ibkr_paper", "data_root", monkeypatch)
    book = mirror / "ibkr_ticks" / "paper" / "ibkr_paper_sim.db"

    assert paper_cli.main(["--simulate", "--db", str(book)]) == 2
    monkeypatch.setenv("CLIMATE_DATA_ROOT", str(mirror / "climate_data"))
    assert paper_cli.main(["--simulate"]) == 2
    assert capsys.readouterr().err.count("mirror of the live root") == 2


def test_a_comparison_refit_on_a_named_mirror_is_still_allowed(marked_root, monkeypatch):
    """`--root <mirror>` is the documented research use; only `--live` is refused."""
    import scripts.fit_gtm_emos as fit_cli

    monkeypatch.setattr(fit_cli, "fit_gtm", _trap)
    with pytest.raises(ReachedTheWork):
        fit_cli.main(["--trend", "--root", str(marked_root / "climate_data")])
```

- [ ] **Step 9: Run it and watch it fail**

Run: `python -m pytest tests/test_architecture/test_mirror_refusal.py -q`
Expected: ten failures -- the eight refusal cases and the two side-door tests -- each with `ReachedTheWork: the entry point did not refuse the marked root`. The eight control cases and the refit test pass already: they are what will show, once the refusal exists, that it depends on the marker and on nothing else. No test hangs and none reaches the Gateway.

- [ ] **Step 10: Add the refusal to each entry point**

In every file below add the import next to the other first-party imports:

```python
from src.utils.live_root import MirrorRootError, refuse_if_mirror  # noqa: E402
```

`scripts/record_ibkr_ticks.py` -- after `settings.record_interval_minutes = args.interval_minutes` and before `setup_logging()`:

```python
    try:
        refuse_if_mirror(settings.resolved_data_root, "record quotes")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2
```

`scripts/sync_ibkr_paper.py` -- after the `trading_mode != "paper"` refusal and before `adapter = IBKRAdapter(...)`; move the `db_path` line up so the check sees the book it will write:

```python
    db_path = Path(args.db) if args.db else settings.resolved_data_root / "paper" / "ibkr_paper.db"
    try:
        refuse_if_mirror(db_path.parent, "reconcile the paper book")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2
```

(and delete the later, now duplicate, `db_path = ...` assignment; `runs_path = db_path.parent / RUNS_FILENAME` stays where it is).

`scripts/run_ibkr_paper.py` -- import `root_path` beside `data_root` (`from climate.paths import data_root, root_path`), and after the `trading_mode != "paper"` refusal, before `signals_path = ...` (which calls `data_root()`), check every place this script writes to: the IBKR lane, a paper book named with `--db`, and the climate lane, where its run ledger goes:

```python
    try:
        refuse_if_mirror(settings.resolved_data_root, "place or simulate paper orders")
        if args.db:
            refuse_if_mirror(Path(args.db).parent, "place or simulate paper orders")
        refuse_if_mirror(root_path(), "place or simulate paper orders")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2
```

Two existing tests of this script replace `IBKRSettings` with a stand-in that has no `resolved_data_root`, and would now fail with an `AttributeError`. In `tests/test_climate/test_staleness_gates.py`, inside `_run_ibkr_paper_with_inits`, the stand-in becomes:

```python
    class _FakeSettings:
        trading_mode = "paper"
        resolved_data_root = tmp_path / "ibkr_ticks"  # no marker above it: a live root
```

`scripts/run_gtm_paper.py` -- import `root_path` beside `data_root` (`from climate.paths import data_root, root_path, warn_if_onedrive`), and before `root = data_root(args.root)`:

```python
    try:
        refuse_if_mirror(root_path(args.root), "write GTM paper signals")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2
```

`scripts/backfill_ibkr_hourly.py` -- in `main`, right after `data_root = settings.resolved_data_root`:

```python
    try:
        refuse_if_mirror(data_root, "backfill hourly bars")
    except MirrorRootError as exc:
        print(exc)
        return 2
```

`scripts/collect_climate_labels.py` -- import `root_path` beside `data_root`, and before `root = data_root(args.data_root)`:

```python
    try:
        refuse_if_mirror(root_path(args.data_root), "append to the label ledger")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2
```

`scripts/fetch_climate_forecasts.py` -- add `from climate.paths import root_path  # noqa: E402` after the `from climate.cds import (...)` block (this script takes `climate_data_root` from `climate.cds` and imports nothing from `climate.paths` yet), and before `root = climate_data_root(args.root)`:

```python
    try:
        refuse_if_mirror(root_path(args.root), "download climate forecasts")
    except MirrorRootError as exc:
        print(exc, file=sys.stderr)
        return 2
```

`scripts/fit_gtm_emos.py` -- after the "no root named" refusal and before `root = data_root(args.root)`; only `--live` is refused, a comparison refit on a named mirror stays allowed:

```python
    if args.live:
        try:
            refuse_if_mirror(_default_root(), "refit the operational model")
        except MirrorRootError as exc:
            print(exc, file=sys.stderr)
            return 2
```

Two of the eight list their exit statuses in the module docstring; say what `2` now also means. In `scripts/backfill_ibkr_hourly.py`, `2 the window, a --refetch date or the session mode is invalid, 3 no Gateway` becomes

```
2 the window, a --refetch date or the session mode is invalid, or the data
root is a mirror of the live root, 3 no Gateway
```

(the rest of that paragraph is unchanged). In `scripts/collect_climate_labels.py`, the paragraph that begins `Exit codes:` gains a last sentence: `2 when the data root is a mirror of the live root: nothing is fetched and nothing is written.`

- [ ] **Step 11: Run the refusal tests, then everything**

Run: `python -m pytest tests/test_architecture/test_mirror_refusal.py tests/test_utils -q`
Expected: `27 passed`.

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: the suite is green with twenty-nine more tests than before this task (eight, two and nineteen), and `All checks passed!`. The one existing test file the refusal touches is `tests/test_climate/test_staleness_gates.py`, changed above. If any other existing test of these entry points fails now, read the failure; do not weaken the refusal.

- [ ] **Step 12: Commit**

```bash
git add src/utils/live_root.py climate/paths.py scripts/record_ibkr_ticks.py scripts/sync_ibkr_paper.py scripts/run_ibkr_paper.py scripts/run_gtm_paper.py scripts/backfill_ibkr_hourly.py scripts/collect_climate_labels.py scripts/fetch_climate_forecasts.py scripts/fit_gtm_emos.py tests/test_utils tests/test_climate/test_root_path.py tests/test_climate/test_staleness_gates.py tests/test_architecture/test_mirror_refusal.py
git commit -m "Refuse to write live state into a root marked as a mirror"
```

---

### Task 2: The session check

Every procedure that moves the Gateway session (spike, cutover, rollback) tests the machine that gives the session up before the other logs in. The test must not connect to the port: on the Spark the owner rule rejects a connection from anyone but `trading`, which would look like "nothing listens". It asks the operating system which sockets listen.

**Files:**
- Create: `scripts/spark/session_check.py`
- Test: `tests/test_spark/__init__.py` (empty), `tests/test_spark/test_session_check.py`

**Interfaces:**
- Consumes: nothing.
- Produces: CLI `python scripts/spark/session_check.py [--port 4002] [--process NAME] [--container NAME]`, exit `0` = no Gateway here (nothing listens, nothing named runs), exit `1` = a Gateway is here. Functions `listening_ports(run=subprocess.run) -> set[int]`, `process_running(name, run=subprocess.run) -> bool`, `container_running(name, run=subprocess.run) -> bool`, `main(argv=None, run=subprocess.run) -> int`. Tasks 7 and 12 call the CLI.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_spark/__init__.py` (empty) and `tests/test_spark/test_session_check.py`:

```python
"""scripts/spark/session_check.py: is a Gateway session possible on this machine?"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import session_check  # noqa: E402

SS_OUTPUT = """\
LISTEN 0      4096       127.0.0.1:4002       0.0.0.0:*
LISTEN 0      128          0.0.0.0:22         0.0.0.0:*
LISTEN 0      4096            [::]:8000          [::]:*
"""
NETSTAT_OUTPUT = """\
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING
  TCP    [::]:4002              [::]:0                 LISTENING
  TCP    192.0.2.20:52011       192.0.2.31:22          ESTABLISHED
  TCP    127.0.0.1:4002         127.0.0.1:52100        ESTABLISHED
"""


def _runner(outputs: dict[str, tuple[int, str]]):
    """A stand-in for subprocess.run keyed on the program name."""
    calls: list[list[str]] = []

    def run(cmd, **_kwargs):
        calls.append(list(cmd))
        code, text = outputs.get(cmd[0], (127, ""))
        return subprocess.CompletedProcess(cmd, code, stdout=text, stderr="")

    run.calls = calls
    return run


def test_listening_ports_reads_ss_on_posix(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    assert session_check.listening_ports(_runner({"ss": (0, SS_OUTPUT)})) == {4002, 22, 8000}


def test_listening_ports_reads_netstat_on_windows_and_ignores_connections(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "win32")
    ports = session_check.listening_ports(_runner({"netstat": (0, NETSTAT_OUTPUT)}))
    assert ports == {135, 4002}  # the ESTABLISHED lines are not listeners


def test_exit_0_when_nothing_listens_and_nothing_runs(monkeypatch, capsys):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    run = _runner({"ss": (0, "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"), "pgrep": (1, ""),
                   "docker": (0, "")})
    rc = session_check.main(["--port", "4002", "--process", "ibgateway", "--container", "ibgw"],
                            run=run)
    assert rc == 0
    assert "no Gateway" in capsys.readouterr().out


def test_exit_1_when_the_port_listens(monkeypatch, capsys):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    rc = session_check.main(["--port", "4002"], run=_runner({"ss": (0, SS_OUTPUT)}))
    assert rc == 1
    assert "4002" in capsys.readouterr().out


def test_exit_1_when_only_the_process_runs(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "win32")
    run = _runner({"netstat": (0, "  TCP    0.0.0.0:135   0.0.0.0:0   LISTENING\n"),
                   "tasklist": (0, "ibgateway.exe   1234 Console   1   400,000 K\n")})
    assert session_check.main(["--port", "4002", "--process", "ibgateway"], run=run) == 1


def test_exit_1_when_only_the_container_runs(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    run = _runner({"ss": (0, ""), "docker": (0, "3f2a9c1d7b10\n")})
    assert session_check.main(["--port", "4002", "--container", "ibgw"], run=run) == 1


def test_a_tool_that_cannot_be_run_is_not_read_as_all_clear(monkeypatch, capsys):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    rc = session_check.main(["--port", "4002"], run=_runner({"ss": (127, "")}))
    assert rc == 1
    assert "could not" in capsys.readouterr().out


def test_a_real_listening_socket_is_seen():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert port in session_check.listening_ports()
    assert port not in session_check.listening_ports()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_spark/test_session_check.py -q`
Expected: `ModuleNotFoundError: No module named 'session_check'`.

- [ ] **Step 3: Write `scripts/spark/session_check.py`**

```python
"""Is an IB Gateway session possible on this machine right now?

    python scripts/spark/session_check.py                                  # port 4002
    python scripts/spark/session_check.py --process ibgateway              # Zenbook
    python scripts/spark/session_check.py --container ibgw                 # Spark

Exit 0 when nothing listens on the port and nothing named runs: this machine
has given the session up, and the other one may log in. Exit 1 when a Gateway
is here, or when that could not be established -- never "all clear" by default.

A paper user can be logged in from one place at a time, so every procedure
that moves the session (spike, cutover, rollback) calls this between its two
halves (docs/plans/2026-10-05-spark-runtime-move-design.md, section 4).

The port is not probed with a connection: on the Spark an owner rule rejects
connections from every user but the runtime's, which would read as "nothing
listens". The operating system is asked which sockets listen instead.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

DEFAULT_PORT = 4002


class CheckFailed(RuntimeError):
    """The tool that answers the question could not be run."""


def _output(cmd: list[str], run) -> str:
    try:
        done = run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckFailed(f"could not run {cmd[0]}: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise CheckFailed(f"could not run {cmd[0]}: exit {done.returncode}")
    return done.stdout or ""


def listening_ports(run=subprocess.run) -> set[int]:
    """Local TCP ports with a listening socket."""
    if sys.platform == "win32":
        text = _output(["netstat", "-an", "-p", "TCP"], run)
        lines = [line for line in text.splitlines() if "LISTENING" in line]
        pattern = re.compile(r"^\s*TCP\s+\S+:(\d+)\s")
    else:
        text = _output(["ss", "-ltnH"], run)
        lines = text.splitlines()
        pattern = re.compile(r"^\s*LISTEN\s+\d+\s+\d+\s+\S+:(\d+)\s")
    ports: set[int] = set()
    for line in lines:
        match = pattern.match(line)
        if match:
            ports.add(int(match.group(1)))
    return ports


def process_running(name: str, run=subprocess.run) -> bool:
    """True when a process whose name contains ``name`` runs."""
    if sys.platform == "win32":
        return name.lower() in _output(["tasklist"], run).lower()
    try:
        done = run(["pgrep", "-f", name], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckFailed(f"could not run pgrep: {type(exc).__name__}") from exc
    if done.returncode not in (0, 1):  # 1 is pgrep's "no process matched"
        raise CheckFailed(f"could not run pgrep: exit {done.returncode}")
    return done.returncode == 0


def container_running(name: str, run=subprocess.run) -> bool:
    """True when a Docker container with exactly this name runs."""
    cmd = ["docker", "ps", "--quiet", "--filter", f"name=^{name}$", "--filter", "status=running"]
    return bool(_output(cmd, run).strip())


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--process", default=None, help="a process name to look for")
    parser.add_argument("--container", default=None, help="a Docker container name to look for")
    args = parser.parse_args(argv)

    found: list[str] = []
    try:
        if args.port in listening_ports(run):
            found.append(f"something listens on port {args.port}")
        if args.process and process_running(args.process, run):
            found.append(f"a process named {args.process} runs")
        if args.container and container_running(args.container, run):
            found.append(f"the container {args.container} runs")
    except CheckFailed as exc:
        print(f"session check: {exc}; treating the session as possibly here")
        return 1
    if found:
        print("session check: a Gateway is here: " + "; ".join(found))
        return 1
    print(f"session check: no Gateway here (port {args.port} closed"
          + (f", no {args.process} process" if args.process else "")
          + (f", container {args.container} not running" if args.container else "") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/test_spark/test_session_check.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Run the script once for real, then commit**

Run: `python scripts/spark/session_check.py --port 4002 --process ibgateway; echo "exit=$?"`
Expected on the Zenbook while its Gateway is open: `session check: a Gateway is here: ...` and `exit=1`. (It reads the socket table and the process list; it connects to nothing.)

```bash
git add scripts/spark/session_check.py tests/test_spark/__init__.py tests/test_spark/test_session_check.py
git commit -m "Add the check that a machine has given the Gateway session up"
```

---

### Task 3: The heartbeat ping wrapper

An external service expects a signal on a schedule and reports when one is late. The wrapper runs a command and, when the command's exit status says it ran, fetches a URL taken from the environment. The URL is a capability, so it never reaches the output: this repo already leaked a token through a URL once (the Telegram alert path, fixed 2026-10-03).

**Files:**
- Create: `scripts/spark/heartbeat_ping.py`
- Test: `tests/test_spark/test_heartbeat_ping.py`

**Interfaces:**
- Consumes: nothing.
- Produces: CLI `python scripts/spark/heartbeat_ping.py --url-env NAME [--ok-exit N [N ...]] -- COMMAND [ARG ...]`. Its exit status is the command's, always. Function `main(argv=None, run=subprocess.call, opener=urllib.request.urlopen, pause=time.sleep) -> int`. The unit files of Task 6 and the launcher of Task 8 call the CLI with `HEARTBEAT_URL_HEALTH`, `HEARTBEAT_URL_LABELS`, `HEARTBEAT_URL_BACKUP` and `HEARTBEAT_URL_OFFBOX`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_spark/test_heartbeat_ping.py`:

```python
"""scripts/spark/heartbeat_ping.py: run a command, signal that it ran, never print the URL."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import heartbeat_ping  # noqa: E402

URL = "https://hc.example.invalid/ping/11111111-2222-3333-4444-555555555555"


class Opener:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.urls: list[str] = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if len(self.urls) <= self.fail_times:
            raise OSError(f"cannot reach {url}")  # the URL is in the error text on purpose
        return None


def _exit_with(code: int) -> list[str]:
    return [sys.executable, "-c", f"raise SystemExit({code})"]


@pytest.fixture(autouse=True)
def url_in_env(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_URL_TEST", URL)


def test_a_command_that_ran_is_signalled_and_its_exit_status_is_kept(capsys):
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener)
    assert rc == 0
    assert opener.urls == [URL]
    assert "heartbeat: sent" in capsys.readouterr().out


def test_an_exit_status_outside_the_ok_list_sends_nothing(capsys):
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(1)],
                             opener=opener)
    assert rc == 1
    assert opener.urls == []
    assert "not sent (command exit 1)" in capsys.readouterr().out


def test_the_ok_list_can_name_more_than_one_status():
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--ok-exit", "0", "1", "--",
                              *_exit_with(1)], opener=opener)
    assert rc == 1 and opener.urls == [URL]


def test_a_failed_ping_is_retried_and_never_changes_the_exit_status(capsys):
    opener = Opener(fail_times=2)
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener, pause=lambda seconds: None)
    assert rc == 0 and len(opener.urls) == 3
    assert "heartbeat: sent" in capsys.readouterr().out


def test_the_url_never_reaches_the_output_even_when_the_error_text_holds_it(capsys):
    opener = Opener(fail_times=99)
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener, pause=lambda seconds: None)
    captured = capsys.readouterr()
    assert rc == 0
    assert "heartbeat: failed (OSError)" in captured.out
    assert URL not in captured.out + captured.err
    assert "11111111" not in captured.out + captured.err


def test_a_missing_variable_is_said_by_name_and_sends_nothing(monkeypatch, capsys):
    monkeypatch.delenv("HEARTBEAT_URL_TEST")
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener)
    assert rc == 0 and opener.urls == []
    assert "HEARTBEAT_URL_TEST is not set" in capsys.readouterr().out


def test_only_an_https_url_is_fetched(monkeypatch, capsys):
    monkeypatch.setenv("HEARTBEAT_URL_TEST", "file:///etc/passwd")
    opener = Opener()
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--", *_exit_with(0)],
                             opener=opener)
    assert rc == 0 and opener.urls == []
    assert "not an https URL" in capsys.readouterr().out


def test_no_command_is_a_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST"])
    assert excinfo.value.code == 2


def test_a_command_that_carries_its_own_double_dash_is_run_whole():
    """The Zenbook's pull is `... -- wsl.exe -d Ubuntu -- bash ...`: only the first -- is ours."""
    opener = Opener()
    counts_its_arguments = [sys.executable, "-c", "import sys; raise SystemExit(len(sys.argv))",
                            "--", "x"]
    rc = heartbeat_ping.main(["--url-env", "HEARTBEAT_URL_TEST", "--ok-exit", "3", "--",
                              *counts_its_arguments], opener=opener)
    assert rc == 3 and opener.urls == [URL]  # the command saw ['-c', '--', 'x']: nothing was eaten
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_spark/test_heartbeat_ping.py -q`
Expected: `ModuleNotFoundError: No module named 'heartbeat_ping'`.

- [ ] **Step 3: Write `scripts/spark/heartbeat_ping.py`**

```python
"""Run a command, then tell an external check that it ran.

    python scripts/spark/heartbeat_ping.py --url-env HEARTBEAT_URL_HEALTH --ok-exit 0 1 -- \\
        python scripts/check_ibkr_recorder.py

The command's exit status is this script's exit status, always: a ping that
fails never turns a good run into a bad one, nor the reverse. When the status
is one of --ok-exit (default: 0), the URL held by the named environment
variable is fetched (GET, 10 s timeout, three attempts).

The URL is a capability: whoever holds it can fake the signal. It is read
from the environment, never from an argument, and it is NEVER printed -- not
in a message and not through an exception, whose text usually quotes it. Only
"sent", "not sent" and the class name of an error leave this script.
(docs/plans/2026-10-05-spark-runtime-move-design.md, section 8.)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request

ATTEMPTS = 3
TIMEOUT_S = 10.0
RETRY_PAUSE_S = 5.0


def ping(url: str, opener=urllib.request.urlopen, pause=time.sleep) -> str | None:
    """None when the URL answered, else the class name of the last error."""
    error = "NoAttempt"
    for attempt in range(ATTEMPTS):
        try:
            opener(url, timeout=TIMEOUT_S)
            return None
        except Exception as exc:  # noqa: BLE001 -- only its class name may be shown
            error = type(exc).__name__
            if attempt + 1 < ATTEMPTS:
                pause(RETRY_PAUSE_S)
    return error


def main(argv: list[str] | None = None, run=subprocess.call,
         opener=urllib.request.urlopen, pause=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url-env", required=True,
                        help="NAME of the environment variable that holds the ping URL")
    parser.add_argument("--ok-exit", nargs="+", type=int, default=[0],
                        help="exit statuses of the command that count as 'it ran'")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- followed by the command to run")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given; put it after --")

    status = run(command)
    if status not in args.ok_exit:
        print(f"heartbeat: not sent (command exit {status})")
        return status
    url = os.environ.get(args.url_env, "").strip()
    if not url:
        print(f"heartbeat: not sent ({args.url_env} is not set)")
        return status
    if not url.startswith("https://"):
        print(f"heartbeat: not sent ({args.url_env} is not an https URL)")
        return status
    error = ping(url, opener=opener, pause=pause)
    print("heartbeat: sent" if error is None else f"heartbeat: failed ({error})")
    return status


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/test_spark/test_heartbeat_ping.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/spark/heartbeat_ping.py tests/test_spark/test_heartbeat_ping.py
git commit -m "Add the heartbeat ping wrapper, which never prints its URL"
```

---

### Task 4: The daily backup of the operational files

The Spark becomes the only owner of the paper book, the ledgers and the operational model. Once a day they are copied to `/srv/quant_data_backups/YYYYMMDD/` with a manifest, and the thirty newest days are kept. The paper book is copied through SQLite's online backup API, so a copy taken while the sync writes is still a consistent database.

**Files:**
- Create: `monitoring/backup_paper_book.py`
- Test: `tests/test_monitoring/test_backup_paper_book.py`

**Interfaces:**
- Consumes: `analysis.spark_parity.MANIFEST_FILES` (tuples of path parts relative to the quant-data root).
- Produces: `monitoring.backup_paper_book.MANIFEST_NAME = "manifest.json"`, `KEEP_DAYS = 30`, `run_backup(root: Path, dest: Path, today: date, keep: int = KEEP_DAYS) -> Path` (returns the day directory), `file_record(path: Path) -> dict` (`{"sha256": str, "size": int}`), `main(argv=None, today: date | None = None) -> int` (0 done, 1 failed). The manifest maps `"a/b/c"` (the relative path joined with `/`) to a record, or to `null` for a file the root does not hold. Task 5 reads this layout; Task 6's backup unit runs `python -m monitoring.backup_paper_book --root /srv/quant_data --dest /srv/quant_data_backups`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_monitoring/test_backup_paper_book.py`:

```python
"""monitoring/backup_paper_book.py: a daily, consistent, rotated copy of the operational files."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest

from analysis.spark_parity import MANIFEST_FILES
from monitoring.backup_paper_book import KEEP_DAYS, MANIFEST_NAME, main, run_backup

BOOK = ("ibkr_ticks", "paper", "ibkr_paper.db")
MODEL = ("climate_data", "models", "gtm_emos.json")
TODAY = date(2026, 10, 20)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def root(tmp_path) -> Path:
    """A quant-data root holding a paper book with three orders and a model file."""
    root = tmp_path / "quant_data"
    book = root.joinpath(*BOOK)
    book.parent.mkdir(parents=True)
    # closing(): a connection that is only left behind keeps the file locked on Windows
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE trade_log (order_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.executemany("INSERT INTO trade_log VALUES (?, ?)",
                         [("a", "open"), ("b", "open"), ("c", "open")])
        conn.commit()
    model = root.joinpath(*MODEL)
    model.parent.mkdir(parents=True)
    model.write_text('{"trend": true}', encoding="utf-8")
    return root


def test_both_files_under_test_are_operational_files():
    assert BOOK in MANIFEST_FILES and MODEL in MANIFEST_FILES


def test_the_copy_and_its_manifest_agree(root, tmp_path):
    day_dir = run_backup(root, tmp_path / "backups", TODAY)

    assert day_dir == tmp_path / "backups" / "20261020"
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest) == {"/".join(parts) for parts in MANIFEST_FILES}
    model_copy = day_dir.joinpath(*MODEL)
    assert model_copy.read_text(encoding="utf-8") == '{"trend": true}'
    assert manifest["/".join(MODEL)] == {"sha256": _sha256(model_copy),
                                         "size": model_copy.stat().st_size}
    assert manifest["/".join(BOOK)]["sha256"] == _sha256(day_dir.joinpath(*BOOK))
    # an operational file this root does not hold is recorded as absent, not skipped
    assert manifest["climate_data/labels/labels_ledger.parquet"] is None


def test_the_paper_book_copy_is_consistent_while_a_writer_is_mid_transaction(root, tmp_path):
    writer = sqlite3.connect(root.joinpath(*BOOK), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO trade_log VALUES ('d', 'open')")  # not committed

        day_dir = run_backup(root, tmp_path / "backups", TODAY)
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    with closing(sqlite3.connect(day_dir.joinpath(*BOOK))) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert [row[0] for row in copy.execute("SELECT order_id FROM trade_log ORDER BY 1")] \
            == ["a", "b", "c"]


def test_only_the_newest_days_are_kept_and_other_directories_are_left_alone(root, tmp_path):
    dest = tmp_path / "backups"
    for age in range(1, KEEP_DAYS + 3):  # 32 older days
        (dest / (TODAY - timedelta(days=age)).strftime("%Y%m%d")).mkdir(parents=True)
    (dest / "notes").mkdir()

    run_backup(root, dest, TODAY)

    days = sorted(p.name for p in dest.iterdir() if p.name.isdigit())
    assert len(days) == KEEP_DAYS
    assert days[-1] == "20261020"
    assert days[0] == (TODAY - timedelta(days=KEEP_DAYS - 1)).strftime("%Y%m%d")
    assert (dest / "notes").is_dir()


def test_a_second_run_on_the_same_day_replaces_the_copy_and_leaves_no_partial(root, tmp_path):
    dest = tmp_path / "backups"
    run_backup(root, dest, TODAY)
    root.joinpath(*MODEL).write_text('{"trend": false}', encoding="utf-8")

    day_dir = run_backup(root, dest, TODAY)

    assert day_dir.joinpath(*MODEL).read_text(encoding="utf-8") == '{"trend": false}'
    assert sorted(p.name for p in dest.iterdir()) == ["20261020"]


def test_main_exits_0_and_names_the_day_it_was_given(root, tmp_path, capsys):
    rc = main(["--root", str(root), "--dest", str(tmp_path / "backups")], today=TODAY)
    assert rc == 0
    assert (tmp_path / "backups" / "20261020" / MANIFEST_NAME).is_file()
    assert "20261020" in capsys.readouterr().out


def test_main_exits_1_when_the_copy_cannot_be_made(root, tmp_path, capsys):
    blocker = tmp_path / "backups"
    blocker.write_text("a file where the backup directory should be", encoding="utf-8")

    rc = main(["--root", str(root), "--dest", str(blocker)], today=TODAY)

    assert rc == 1
    assert "backup failed" in capsys.readouterr().out
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_monitoring/test_backup_paper_book.py -q`
Expected: `ModuleNotFoundError: No module named 'monitoring.backup_paper_book'`.

- [ ] **Step 3: Write `monitoring/backup_paper_book.py`**

```python
"""Daily backup of a quant-data root's operational files, with a manifest and rotation.

    python -m monitoring.backup_paper_book --root /srv/quant_data --dest /srv/quant_data_backups

Copies the files analysis.spark_parity lists as MANIFEST_FILES -- the paper
book, the sync and run ledgers, the signals, the operational model, its
diagnostics and the label ledger -- into ``<dest>/YYYYMMDD/`` under the same
relative paths, writes ``manifest.json`` (sha256 and size of every copy,
``null`` for a file the root does not hold) beside them, and keeps the newest
KEEP_DAYS day directories.

The paper book is copied with SQLite's online backup API, not as a file: the
paper sync writes it, and a plain copy taken mid-transaction can be a
database that does not open. Every other file is small and written
atomically or append-only by its owner.

The day directory is built under a ``.partial`` name and renamed when it is
complete, so a reader never finds a half-written day. Exit 0 when the day
directory is in place, 1 on any failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from analysis.spark_parity import MANIFEST_FILES

KEEP_DAYS = 30
MANIFEST_NAME = "manifest.json"
_DAY_DIR = re.compile(r"^\d{8}$")


def file_record(path: Path) -> dict:
    """sha256 and size of one file, as the manifest stores them."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size": path.stat().st_size}


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix == ".db":
        uri = f"file:{src.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as source, \
                closing(sqlite3.connect(dst)) as target:
            source.backup(target)
    else:
        shutil.copy2(src, dst)


def _prune(dest: Path, keep: int) -> list[Path]:
    """Remove all but the ``keep`` newest day directories; return what was removed."""
    days = sorted(p for p in dest.iterdir() if p.is_dir() and _DAY_DIR.match(p.name))
    removed = days[:-keep] if keep > 0 else []
    for old in removed:
        shutil.rmtree(old)
    return removed


def run_backup(root: Path, dest: Path, today: date, keep: int = KEEP_DAYS) -> Path:
    """Copy the operational files of ``root`` into ``dest/YYYYMMDD`` and prune old days."""
    day_dir = dest / today.strftime("%Y%m%d")
    partial = dest / (day_dir.name + ".partial")
    dest.mkdir(parents=True, exist_ok=True)
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    manifest: dict[str, dict | None] = {}
    for parts in MANIFEST_FILES:
        src = Path(root).joinpath(*parts)
        key = "/".join(parts)
        if src.exists():
            copy = partial.joinpath(*parts)
            _copy(src, copy)
            manifest[key] = file_record(copy)
        else:
            manifest[key] = None
    (partial / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True),
                                         encoding="utf-8")
    if day_dir.exists():
        shutil.rmtree(day_dir)
    partial.rename(day_dir)
    _prune(dest, keep)
    return day_dir


def main(argv: list[str] | None = None, today: date | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path.home() / "quant_data"),
                        help="quant-data root to back up (default ~/quant_data)")
    parser.add_argument("--dest", default=str(Path.home() / "quant_data_backups"),
                        help="where the day directories go (default ~/quant_data_backups)")
    parser.add_argument("--keep", type=int, default=KEEP_DAYS,
                        help=f"day directories to keep (default {KEEP_DAYS})")
    args = parser.parse_args(argv)
    # One clock for the directory name and for the rotation, and it can be injected.
    day = today or datetime.now(timezone.utc).date()
    try:
        day_dir = run_backup(Path(args.root).expanduser(), Path(args.dest).expanduser(),
                             day, keep=args.keep)
    except Exception as exc:  # noqa: BLE001 -- any failure is the outcome to report
        print(f"backup failed: {type(exc).__name__}: {exc}")
        return 1
    manifest = json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    present = sum(1 for record in manifest.values() if record is not None)
    print(f"backup written: {day_dir} ({present}/{len(manifest)} files present)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/test_monitoring/test_backup_paper_book.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add monitoring/backup_paper_book.py tests/test_monitoring/test_backup_paper_book.py
git commit -m "Add the daily backup of the operational files, with a manifest and rotation"
```

---

### Task 5: The restore check

A backup that has never been restored is a hope. This script restores a copy into a scratch root and verifies it. A copy is a day directory written by Task 4 (it carries `manifest.json`) or a whole quant-data root such as the Zenbook mirror (it carries none, and the check then records what the source held when it started).

**Files:**
- Create: `scripts/spark/restore_check.py`
- Test: `tests/test_spark/test_restore_check.py`

**Interfaces:**
- Consumes: `monitoring.backup_paper_book.MANIFEST_NAME`, `file_record`, `run_backup`; `analysis.spark_parity.MANIFEST_FILES`, `check_parquet`.
- Produces: CLI `python scripts/spark/restore_check.py --from COPY --scratch DIR`; exit `0` every check passed, `1` at least one failed (each printed as `FAIL <file>: <reason>`), `2` the scratch directory is not empty. Function `restore_and_verify(copy: Path, scratch: Path) -> list[str]` (the failures; empty means good). Tasks 11 and 13 run the CLI.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_spark/test_restore_check.py`:

```python
"""scripts/spark/restore_check.py: restore a copy into a scratch root and verify it."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import restore_check  # noqa: E402

from monitoring.backup_paper_book import MANIFEST_NAME, run_backup  # noqa: E402

BOOK = ("ibkr_ticks", "paper", "ibkr_paper.db")
MODEL = ("climate_data", "models", "gtm_emos.json")
LEDGER = ("climate_data", "labels", "labels_ledger.parquet")
RUNS = ("ibkr_ticks", "paper", "ibkr_sync_runs.jsonl")


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "quant_data"
    book = root.joinpath(*BOOK)
    book.parent.mkdir(parents=True)
    # closing(): a connection that is only left behind keeps the file locked on Windows
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE positions (question_id TEXT, size REAL)")
        conn.execute("CREATE TABLE trade_log (order_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO trade_log VALUES ('a', 'open')")
        conn.commit()
    root.joinpath(*RUNS).write_text('{"ok": true}\n{"ok": false}\n', encoding="utf-8")
    model = root.joinpath(*MODEL)
    model.parent.mkdir(parents=True)
    model.write_text('{"trend": true}', encoding="utf-8")
    ledger = root.joinpath(*LEDGER)
    ledger.parent.mkdir(parents=True)
    pd.DataFrame({"series": ["gtm"], "value": [1.18]}).to_parquet(ledger, index=False)
    return root


@pytest.fixture
def day_dir(root, tmp_path) -> Path:
    return run_backup(root, tmp_path / "backups", date(2026, 10, 20))


def test_a_good_day_directory_restores_and_verifies(day_dir, tmp_path, capsys):
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "RESTORE OK" in out
    assert (tmp_path / "scratch").joinpath(*BOOK).is_file()


def test_a_whole_root_without_a_manifest_can_be_checked_too(root, tmp_path, capsys):
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    assert rc == 0, capsys.readouterr().out


def test_a_file_changed_after_the_manifest_was_written_fails(day_dir, tmp_path, capsys):
    day_dir.joinpath(*MODEL).write_text('{"trend": false}', encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL climate_data/models/gtm_emos.json" in out and "sha256" in out


def test_a_file_the_manifest_lists_but_the_copy_lacks_fails(day_dir, tmp_path, capsys):
    day_dir.joinpath(*RUNS).unlink()
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_sync_runs.jsonl: missing" in out


def test_a_truncated_paper_book_fails_even_when_the_manifest_was_written_from_it(
        root, tmp_path, capsys):
    book = root.joinpath(*BOOK)
    book.write_bytes(book.read_bytes()[:100])  # the header only: not a database any more
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL ibkr_ticks/paper/ibkr_paper.db" in out


def test_a_paper_book_without_its_tables_fails(root, tmp_path, capsys):
    book = root.joinpath(*BOOK)
    book.unlink()
    with closing(sqlite3.connect(book)) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "trade_log" in out


def test_an_unreadable_parquet_and_a_broken_ledger_line_fail(root, tmp_path, capsys):
    root.joinpath(*LEDGER).write_bytes(b"not parquet")
    root.joinpath(*RUNS).write_text('{"ok": true}\nnot json\n', encoding="utf-8")
    rc = restore_check.main(["--from", str(root), "--scratch", str(tmp_path / "scratch")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL climate_data/labels/labels_ledger.parquet" in out
    assert "FAIL ibkr_ticks/paper/ibkr_sync_runs.jsonl: line 2" in out


def test_a_scratch_directory_that_holds_something_is_refused(day_dir, tmp_path, capsys):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "keep.txt").write_text("mine", encoding="utf-8")
    rc = restore_check.main(["--from", str(day_dir), "--scratch", str(scratch)])
    assert rc == 2
    assert (scratch / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert json.loads((day_dir / MANIFEST_NAME).read_text(encoding="utf-8"))  # source untouched
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_spark/test_restore_check.py -q`
Expected: `ModuleNotFoundError: No module named 'restore_check'`.

- [ ] **Step 3: Write `scripts/spark/restore_check.py`**

```python
"""Restore a copy of the operational files into a scratch root and verify it.

    python scripts/spark/restore_check.py --from /srv/quant_data_backups/20261020 --scratch /tmp/restore
    python scripts/spark/restore_check.py --from ~/quant_data --scratch ~/restore_scratch   # a mirror

A copy is a day directory written by monitoring/backup_paper_book.py (it
carries manifest.json) or a whole quant-data root (it carries none; what the
source holds when the check starts is then the reference). The operational
files are copied into the scratch root, and the restored files are verified:

  * every file the reference lists is there, with the listed sha256 and size;
  * the paper book passes SQLite's integrity check and has its tables;
  * every .parquet file reads, every .json file parses, every .jsonl line parses.

The source is only read. The scratch root must not exist or must be empty.
Exit 0 all good, 1 at least one failure (each printed), 2 scratch not empty.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import MANIFEST_FILES, check_parquet  # noqa: E402
from monitoring.backup_paper_book import MANIFEST_NAME, file_record  # noqa: E402

REQUIRED_TABLES = ("positions", "trade_log")


def _reference(copy: Path) -> dict[str, dict | None]:
    manifest = copy / MANIFEST_NAME
    if manifest.is_file():
        return json.loads(manifest.read_text(encoding="utf-8"))
    reference: dict[str, dict | None] = {}
    for parts in MANIFEST_FILES:
        src = copy.joinpath(*parts)
        reference["/".join(parts)] = file_record(src) if src.is_file() else None
    return reference


def _check_book(path: Path) -> str | None:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()
            tables = {row[0] for row in
                      conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error as exc:
        return f"not a readable database ({type(exc).__name__}: {exc})"
    if verdict != ("ok",):
        return f"integrity check says {verdict!r}"
    missing = [table for table in REQUIRED_TABLES if table not in tables]
    if missing:
        return f"tables missing: {', '.join(missing)}"
    return None


def _check_content(key: str, path: Path) -> str | None:
    if path.suffix == ".db":
        return _check_book(path)
    if path.suffix == ".parquet":
        return check_parquet(path)
    if path.suffix == ".json":
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return f"not JSON ({exc})"
    if path.suffix == ".jsonl":
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError:
                return f"line {number} is not JSON"
    return None


def restore_and_verify(copy: Path, scratch: Path) -> list[str]:
    """Restore ``copy`` into ``scratch``; return the failures (empty when it is good)."""
    reference = _reference(copy)
    failures: list[str] = []
    for key, expected in sorted(reference.items()):
        if expected is None:
            continue
        src = copy.joinpath(*key.split("/"))
        if not src.is_file():
            failures.append(f"FAIL {key}: missing from the copy")
            continue
        restored = scratch.joinpath(*key.split("/"))
        restored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, restored)
        actual = file_record(restored)
        if actual != expected:
            failures.append(f"FAIL {key}: sha256 or size differs from the reference "
                            f"({actual['size']} bytes restored, {expected['size']} expected)")
            continue
        problem = _check_content(key, restored)
        if problem:
            failures.append(f"FAIL {key}: {problem}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="copy", required=True,
                        help="a backup day directory, or a quant-data root")
    parser.add_argument("--scratch", required=True,
                        help="where to restore to; must not exist or must be empty")
    args = parser.parse_args(argv)
    copy = Path(args.copy).expanduser()
    scratch = Path(args.scratch).expanduser()
    if scratch.exists() and any(scratch.iterdir()):
        print(f"refusing: {scratch} is not empty; name a fresh scratch directory")
        return 2
    scratch.mkdir(parents=True, exist_ok=True)
    reference = _reference(copy)
    listed = sum(1 for record in reference.values() if record is not None)
    failures = restore_and_verify(copy, scratch)
    for failure in failures:
        print(failure)
    if failures:
        print(f"RESTORE FAILED: {len(failures)} of {listed} files ({copy} -> {scratch})")
        return 1
    print(f"RESTORE OK: {listed} files restored and verified ({copy} -> {scratch})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/test_spark/test_restore_check.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/spark/restore_check.py tests/test_spark/test_restore_check.py
git commit -m "Add the restore check: a copy is restored into a scratch root and verified"
```

---

### Task 6: Unit files, the owner rule and the sudoers drop-in

Everything the account holder will install with `sudo` lives in the repo first, where it is reviewed and tested: the runtime's units, the unit that makes the owner rule permanent, and the sudoers drop-in a deploy relies on. (The fourth such file, the Gateway's compose file, is pinned to the image the spike tried and is therefore committed with the spike, in Task 0.)

**Files:**
- Create: `deploy/spark/systemd/ibkr-recorder.service`, `ibkr-health.service`, `ibkr-health.timer`, `ibkr-paper-sync.service`, `ibkr-paper-sync.timer`, `climate-labels.service`, `climate-labels.timer`, `paper-book-backup.service`, `paper-book-backup.timer`, `ibgw-owner-rule.service` (all under `deploy/spark/systemd/`)
- Create: `deploy/spark/sudoers/forecast-runtime`
- Modify: `.gitattributes` (unless Task 0 has added the line already)
- Test: `tests/test_deploy/__init__.py` (empty; unless Task 0 has created it), `tests/test_deploy/test_unit_files.py`

**Interfaces:**
- Consumes: `scripts/spark/heartbeat_ping.py` (Task 3); `monitoring/backup_paper_book.py` (Task 4).
- Produces: the unit names `ibkr-recorder.service`, `ibkr-health.service`/`.timer`, `ibkr-paper-sync.service`/`.timer`, `climate-labels.service`/`.timer`, `paper-book-backup.service`/`.timer`, `ibgw-owner-rule.service`; the sudoers command list (start and stop for every runtime unit, restart for the recorder only). Tasks 7, 9, 10, 11 and 12 use these names.

- [ ] **Step 1: Write the failing tests for the units and the sudoers file**

Create `tests/test_deploy/__init__.py` (empty, unless it exists) and `tests/test_deploy/test_unit_files.py`:

```python
"""deploy/spark/systemd and deploy/spark/sudoers: what the account holder installs with sudo."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_DIR = REPO_ROOT / "deploy" / "spark" / "systemd"
SUDOERS = REPO_ROOT / "deploy" / "spark" / "sudoers" / "forecast-runtime"
CLONE = "/home/trading/forecast-model"

RUNTIME_SERVICES = ["ibkr-recorder.service", "ibkr-health.service", "ibkr-paper-sync.service",
                    "climate-labels.service", "paper-book-backup.service"]
TIMERS = {"ibkr-health.timer": "*-*-* *:00:00 Europe/Istanbul",
          "ibkr-paper-sync.timer": "*-*-* 17:30:00 Europe/Istanbul",
          "climate-labels.timer": "*-*-* 16:30:00 Europe/Istanbul",
          "paper-book-backup.timer": "*-*-* 21:00:00 Europe/Istanbul"}
SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TWS_PASSWORD", "TWS_USERID",
               "VNC_SERVER_PASSWORD", "HEARTBEAT_URL_HEALTH", "HEARTBEAT_URL_LABELS",
               "HEARTBEAT_URL_BACKUP", "HEARTBEAT_URL_OFFBOX")


def parse_unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """A unit file as {section: [(key, value), ...]}; a key may repeat."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = []
            continue
        key, _, value = line.partition("=")
        sections[current].append((key.strip(), value.strip()))
    return sections


def values(unit: dict, section: str, key: str) -> list[str]:
    return [value for name, value in unit.get(section, []) if name == key]


def test_the_expected_units_exist_and_nothing_else():
    names = sorted(p.name for p in UNIT_DIR.iterdir())
    assert names == sorted([*RUNTIME_SERVICES, *TIMERS, "ibgw-owner-rule.service"])


@pytest.mark.parametrize("name", RUNTIME_SERVICES)
def test_a_runtime_service_runs_as_trading_with_the_agreed_protection(name):
    unit = parse_unit(UNIT_DIR / name)
    assert values(unit, "Service", "User") == ["trading"]
    assert values(unit, "Service", "Group") == ["trading"]
    assert values(unit, "Service", "UMask") == ["0027"]
    assert values(unit, "Service", "NoNewPrivileges") == ["yes"]
    assert values(unit, "Service", "OOMScoreAdjust") == ["-900"]
    assert values(unit, "Service", "WorkingDirectory") == [CLONE]


@pytest.mark.parametrize("name", RUNTIME_SERVICES)
def test_a_runtime_service_starts_the_clone_s_python_on_files_that_exist(name):
    (exec_start,) = values(parse_unit(UNIT_DIR / name), "Service", "ExecStart")
    assert exec_start.startswith(f"{CLONE}/.venv/bin/python -u ")
    scripts = re.findall(r"(?<![\w/])(scripts/[\w/]+\.py)", exec_start)
    modules = re.findall(r"-m ([\w.]+)", exec_start)
    assert scripts or modules, exec_start
    for script in scripts:
        assert (REPO_ROOT / script).is_file(), script
    for module in modules:
        assert (REPO_ROOT / (module.replace(".", "/") + ".py")).is_file(), module


@pytest.mark.parametrize(("name", "calendar"), sorted(TIMERS.items()))
def test_a_timer_names_its_zone_catches_up_and_has_its_service(name, calendar):
    unit = parse_unit(UNIT_DIR / name)
    assert values(unit, "Timer", "OnCalendar") == [calendar]
    assert values(unit, "Timer", "Persistent") == ["true"]
    (service,) = values(unit, "Timer", "Unit")
    assert (UNIT_DIR / service).is_file()
    assert values(unit, "Install", "WantedBy") == ["timers.target"]


def test_the_recorder_is_always_on_and_comes_back_by_itself():
    unit = parse_unit(UNIT_DIR / "ibkr-recorder.service")
    assert values(unit, "Service", "Restart") == ["always"]
    assert values(unit, "Service", "RestartSec") == ["30"]
    assert values(unit, "Install", "WantedBy") == ["multi-user.target"]
    (exec_start,) = values(unit, "Service", "ExecStart")
    assert exec_start.endswith("scripts/record_ibkr_ticks.py --interval-minutes 20")


def test_only_the_health_unit_loads_the_telegram_file():
    alerts = "/home/trading/.config/forecast-model/alerts.env"
    loaders = [name for name in RUNTIME_SERVICES
               if alerts in values(parse_unit(UNIT_DIR / name), "Service", "EnvironmentFile")]
    assert loaders == ["ibkr-health.service"]


def test_each_heartbeat_goes_through_the_wrapper_with_its_own_variable():
    expected = {"ibkr-health.service": ("HEARTBEAT_URL_HEALTH", "--ok-exit 0 1 --"),
                "climate-labels.service": ("HEARTBEAT_URL_LABELS", "--"),
                "paper-book-backup.service": ("HEARTBEAT_URL_BACKUP", "--")}
    for name, (variable, marker) in expected.items():
        unit = parse_unit(UNIT_DIR / name)
        (exec_start,) = values(unit, "Service", "ExecStart")
        assert f"scripts/spark/heartbeat_ping.py --url-env {variable} {marker}" in exec_start
        assert "/home/trading/.config/forecast-model/heartbeat.env" in values(
            unit, "Service", "EnvironmentFile")


def test_an_unhealthy_probe_is_not_a_failed_unit():
    unit = parse_unit(UNIT_DIR / "ibkr-health.service")
    assert values(unit, "Service", "SuccessExitStatus") == ["1"]


def test_the_owner_rule_admits_trading_on_the_api_and_the_operator_on_the_screen():
    unit = parse_unit(UNIT_DIR / "ibgw-owner-rule.service")
    starts = values(unit, "Service", "ExecStart")
    assert len(starts) == 2
    assert any("--dport 4002 -m owner ! --uid-owner trading -j REJECT" in s for s in starts)
    assert any("--dport 5900 -m owner ! --uid-owner scura -j REJECT" in s for s in starts)
    assert all("-o lo -p tcp" in s and "iptables -C" in s and "iptables -I" in s for s in starts)
    assert values(unit, "Service", "RemainAfterExit") == ["yes"]


def test_the_sudoers_file_names_only_these_units_and_only_three_verbs():
    text = SUDOERS.read_text(encoding="utf-8")
    assert "scura ALL=(root) NOPASSWD: FORECAST_UNITS" in text
    commands = re.findall(r"/usr/bin/systemctl (\S+) (\S+?)(?:,|\s|$)", text)
    assert commands, text
    units = {p.name for p in UNIT_DIR.iterdir()} - {"ibgw-owner-rule.service"}
    for verb, unit in commands:
        assert verb in {"start", "stop", "restart"}
        assert unit in units, unit
    assert {unit for _verb, unit in commands} == units
    assert "*" not in text and "ALL=(ALL)" not in text


def test_no_file_under_deploy_gives_a_secret_a_value():
    offenders = []
    for path in (REPO_ROOT / "deploy").rglob("*"):
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for key in SECRET_KEYS:
                match = re.match(rf"\s*{key}\s*[=:]\s*(.*)$", line)
                if match and match.group(1).strip() and not match.group(1).strip().startswith("${"):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert offenders == []


def test_files_under_deploy_are_lf_in_every_checkout():
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "deploy/** text eol=lf" in attributes
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_deploy/test_unit_files.py -q`
Expected: every test fails or errors with `FileNotFoundError` on `deploy/spark/systemd`, and the last one fails on the missing `.gitattributes` line.

- [ ] **Step 3: Write the units**

Append to `.gitattributes` (unless the line is already there):

```
# Unit files, the compose file and the sudoers drop-in are read by systemd, Docker and
# sudo on the Spark; they must be LF in every checkout.
deploy/** text eol=lf
```

`deploy/spark/systemd/ibkr-recorder.service`:

```ini
# ForecastEx quote recorder (clientId 2, read-only) -> /srv/quant_data/ibkr_ticks.
# Reads IBKR_PORT / IBKR_TRADING_MODE from the clone's .env. It retries the Gateway
# by itself (GatewaySession), so it needs no ordering against the Gateway container.
[Unit]
Description=ForecastEx quote recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trading
Group=trading
UMask=0027
NoNewPrivileges=yes
OOMScoreAdjust=-900
WorkingDirectory=/home/trading/forecast-model
ExecStart=/home/trading/forecast-model/.venv/bin/python -u scripts/record_ibkr_ticks.py --interval-minutes 20
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

`deploy/spark/systemd/ibkr-health.service`:

```ini
# Recorder and paper-sync health probe. Exit 1 means "found a problem and the alert
# path handled it", which is a successful run of this unit. The external heartbeat is
# pinged whenever the probe ran (exit 0 or 1), so a probe that breaks goes late there.
[Unit]
Description=IBKR recorder and paper-sync health probe

[Service]
Type=oneshot
User=trading
Group=trading
UMask=0027
NoNewPrivileges=yes
OOMScoreAdjust=-900
WorkingDirectory=/home/trading/forecast-model
EnvironmentFile=/home/trading/.config/forecast-model/alerts.env
EnvironmentFile=/home/trading/.config/forecast-model/heartbeat.env
ExecStart=/home/trading/forecast-model/.venv/bin/python -u scripts/spark/heartbeat_ping.py --url-env HEARTBEAT_URL_HEALTH --ok-exit 0 1 -- /home/trading/forecast-model/.venv/bin/python -u scripts/check_ibkr_recorder.py
SuccessExitStatus=1
TimeoutStartSec=300
```

`deploy/spark/systemd/ibkr-health.timer`:

```ini
[Unit]
Description=Hourly IBKR health probe

[Timer]
OnCalendar=*-*-* *:00:00 Europe/Istanbul
Persistent=true
Unit=ibkr-health.service

[Install]
WantedBy=timers.target
```

`deploy/spark/systemd/ibkr-paper-sync.service`:

```ini
# Daily reconciliation of resting paper orders (clientId 4, read-only). A failed run
# leaves this unit failed and shows up as SYNC_STALE in the health probe.
[Unit]
Description=IBKR paper book reconciliation

[Service]
Type=oneshot
User=trading
Group=trading
UMask=0027
NoNewPrivileges=yes
OOMScoreAdjust=-900
WorkingDirectory=/home/trading/forecast-model
ExecStart=/home/trading/forecast-model/.venv/bin/python -u scripts/sync_ibkr_paper.py
TimeoutStartSec=600
```

`deploy/spark/systemd/ibkr-paper-sync.timer`:

```ini
[Unit]
Description=Daily IBKR paper book reconciliation

[Timer]
OnCalendar=*-*-* 17:30:00 Europe/Istanbul
Persistent=true
Unit=ibkr-paper-sync.service

[Install]
WantedBy=timers.target
```

`deploy/spark/systemd/climate-labels.service`:

```ini
# NOAA first-print label ledger. The external heartbeat is pinged on success only.
[Unit]
Description=Climate label collector
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=trading
Group=trading
UMask=0027
NoNewPrivileges=yes
OOMScoreAdjust=-900
WorkingDirectory=/home/trading/forecast-model
EnvironmentFile=/home/trading/.config/forecast-model/heartbeat.env
ExecStart=/home/trading/forecast-model/.venv/bin/python -u scripts/spark/heartbeat_ping.py --url-env HEARTBEAT_URL_LABELS -- /home/trading/forecast-model/.venv/bin/python -u scripts/collect_climate_labels.py
TimeoutStartSec=900
```

`deploy/spark/systemd/climate-labels.timer`:

```ini
[Unit]
Description=Daily climate label collection

[Timer]
OnCalendar=*-*-* 16:30:00 Europe/Istanbul
Persistent=true
Unit=climate-labels.service

[Install]
WantedBy=timers.target
```

`deploy/spark/systemd/paper-book-backup.service`:

```ini
# Daily copy of the operational files with a manifest; 30 days kept. The external
# heartbeat is pinged on success only.
[Unit]
Description=Backup of the paper book, the ledgers and the operational model

[Service]
Type=oneshot
User=trading
Group=trading
UMask=0027
NoNewPrivileges=yes
OOMScoreAdjust=-900
WorkingDirectory=/home/trading/forecast-model
EnvironmentFile=/home/trading/.config/forecast-model/heartbeat.env
ExecStart=/home/trading/forecast-model/.venv/bin/python -u scripts/spark/heartbeat_ping.py --url-env HEARTBEAT_URL_BACKUP -- /home/trading/forecast-model/.venv/bin/python -u -m monitoring.backup_paper_book --root /srv/quant_data --dest /srv/quant_data_backups
TimeoutStartSec=600
```

`deploy/spark/systemd/paper-book-backup.timer`:

```ini
[Unit]
Description=Daily backup of the operational files

[Timer]
OnCalendar=*-*-* 21:00:00 Europe/Istanbul
Persistent=true
Unit=paper-book-backup.service

[Install]
WantedBy=timers.target
```

`deploy/spark/systemd/ibgw-owner-rule.service` (runs as root; it is the firewall, not the runtime):

```ini
# Loopback isolation for the IB Gateway container. The LLM stack's containers use the
# host network, so "bound to 127.0.0.1" does not keep them away from the Gateway.
# Port 4002 (API): only processes of the user trading. Port 5900 (remote screen): only
# processes of the operator, whose ssh tunnel is how the screen is reached.
# iptables -C makes each rule idempotent: it is inserted only when it is not there.
[Unit]
Description=Owner rule for the IB Gateway API and remote-screen ports
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c '/usr/sbin/iptables -C OUTPUT -o lo -p tcp --dport 4002 -m owner ! --uid-owner trading -j REJECT --reject-with tcp-reset 2>/dev/null || /usr/sbin/iptables -I OUTPUT -o lo -p tcp --dport 4002 -m owner ! --uid-owner trading -j REJECT --reject-with tcp-reset'
ExecStart=/bin/sh -c '/usr/sbin/iptables -C OUTPUT -o lo -p tcp --dport 5900 -m owner ! --uid-owner scura -j REJECT --reject-with tcp-reset 2>/dev/null || /usr/sbin/iptables -I OUTPUT -o lo -p tcp --dport 5900 -m owner ! --uid-owner scura -j REJECT --reject-with tcp-reset'
ExecStop=/bin/sh -c '/usr/sbin/iptables -D OUTPUT -o lo -p tcp --dport 4002 -m owner ! --uid-owner trading -j REJECT --reject-with tcp-reset; /usr/sbin/iptables -D OUTPUT -o lo -p tcp --dport 5900 -m owner ! --uid-owner scura -j REJECT --reject-with tcp-reset'

[Install]
WantedBy=multi-user.target
```

`deploy/spark/sudoers/forecast-runtime`:

```
# Install as /etc/sudoers.d/forecast-runtime, mode 0440, after `visudo -cf` accepts it.
# Lets the operator start and stop the runtime's units (the recorder, the four timers and
# the four services behind them) and restart the recorder, from a deploy or a rollback,
# without a password, and nothing else: no other unit, no other verb. Installing or
# changing a unit file stays a manual sudo step.
Cmnd_Alias FORECAST_UNITS = \
    /usr/bin/systemctl start ibkr-recorder.service, \
    /usr/bin/systemctl stop ibkr-recorder.service, \
    /usr/bin/systemctl restart ibkr-recorder.service, \
    /usr/bin/systemctl start ibkr-health.service, \
    /usr/bin/systemctl stop ibkr-health.service, \
    /usr/bin/systemctl start ibkr-health.timer, \
    /usr/bin/systemctl stop ibkr-health.timer, \
    /usr/bin/systemctl start ibkr-paper-sync.service, \
    /usr/bin/systemctl stop ibkr-paper-sync.service, \
    /usr/bin/systemctl start ibkr-paper-sync.timer, \
    /usr/bin/systemctl stop ibkr-paper-sync.timer, \
    /usr/bin/systemctl start climate-labels.service, \
    /usr/bin/systemctl stop climate-labels.service, \
    /usr/bin/systemctl start climate-labels.timer, \
    /usr/bin/systemctl stop climate-labels.timer, \
    /usr/bin/systemctl start paper-book-backup.service, \
    /usr/bin/systemctl stop paper-book-backup.service, \
    /usr/bin/systemctl start paper-book-backup.timer, \
    /usr/bin/systemctl stop paper-book-backup.timer
scura ALL=(root) NOPASSWD: FORECAST_UNITS
```

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/test_deploy/test_unit_files.py -q`
Expected: `23 passed`.

- [ ] **Step 5: Run everything, then commit**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: green, `All checks passed!`.

```bash
git add .gitattributes deploy/spark/systemd deploy/spark/sudoers tests/test_deploy/__init__.py tests/test_deploy/test_unit_files.py
git commit -m "Add the runtime's unit files, the owner rule and the sudoers drop-in"
```

---

### Task 7: The shell scripts: deploy, the two copy scripts, rollback, the research launcher

Five new scripts and one changed one. Two of them are also run for real in the tests, against stand-ins for ssh and git: what `rollback.sh` says when the Spark does not answer, and what `deploy.sh` does to a recorder that is not running, cannot be seen in a dry run. The copy scripts carry the marker rule from the other side: Task 1 made the live-writing entry points refuse a marked root; here the scripts that copy INTO a root refuse an unmarked one, and the old push refuses to push a marked one. Every script has a `--dry-run` that prints its commands and calls neither ssh nor rsync, which is what the tests run.

**Files:**
- Create: `scripts/spark/deploy.sh`, `scripts/spark/pull_snapshot.sh`, `scripts/spark/mirror_live.sh`, `scripts/spark/rollback.sh`, `scripts/spark/research_run.sh`
- Modify: `scripts/spark/push_snapshot.sh` (whole file replaced: refuses a marked source, `--cutover`, `--dry-run`, host and root of the verify step are variables)
- Test: `tests/test_deploy/test_shell_scripts.py`

**Interfaces:**
- Consumes: the marker file name `.mirror-of-spark` (Task 1); `scripts/spark/session_check.py` (Task 2), which `rollback.sh` pipes to the Spark's `python3`; the unit names and `deploy/spark/sudoers/forecast-runtime` (Task 6); the ssh hosts `spark` and `spark-rt` and the git remote `spark-rt` (Task 0).
- Produces, for Tasks 8, 10, 11, 12 and 13 (every script is started as `bash scripts/spark/<name>`; the checkout carries no executable bits):
  - `scripts/spark/deploy.sh [--no-restart] [--dry-run]` (Git Bash on the Zenbook); exit 0 done, 2 usage, 3 a unit file must be installed by hand first. It restarts the recorder only when the recorder is running; it never starts the runtime. Environment: `SSH`, `DEPLOY_GIT`, `DEPLOY_REMOTE`, `DEPLOY_OPERATOR`.
  - `scripts/spark/pull_snapshot.sh [--include-raw] [--dry-run]` (WSL on the Zenbook); exit 0 done, 2 usage, 3 the mirror carries no marker. Environment: `QUANT_DATA_MIRROR`, `QUANT_DATA_BACKUPS_MIRROR`, `SPARK_SRC`, `SPARK_BACKUPS_SRC`, `SPARK_SSH`.
  - `scripts/spark/mirror_live.sh [--include-raw] [--dry-run]` (on the Spark, as `scura`); same exit statuses. Environment: `LIVE_ROOT`, `MIRROR_ROOT`.
  - `scripts/spark/rollback.sh [--no-copy] [--dry-run]` (WSL on the Zenbook); exit 0 done, 2 usage, 3 destination refused, 4 the Spark may still hold the session (it could not be reached, a stop was refused, or the check found a Gateway: nobody logs in on the Zenbook), any other status a failed copy after the session was released. `--no-copy` ends after the session check. Its by-hand list names `scripts/scheduling/swap_live_root_for_mirror.ps1 -Undo` (Task 8). Environment: `ROLLBACK_DEST`, `SPARK_HOST`, `SPARK_SSH`.
  - `scripts/spark/research_run.sh [--memory 15G] [--dry-run] -- COMMAND [ARG ...]` (on the Spark, as `scura`).
  - `scripts/spark/push_snapshot.sh [--include-raw] [--cutover] [--dry-run]`; new exit 3 when the source carries the marker.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deploy/test_shell_scripts.py` (`tests/test_deploy/__init__.py` exists since Task 6):

```python
"""scripts/spark/*.sh: syntax, line endings, dry runs and refusals.

Every script is run by a real bash in its dry-run mode, which prints the
commands and calls neither ssh nor rsync. On Windows that bash is Git Bash;
System32\\bash.exe is the WSL launcher and is never used here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "spark"
UNIT_DIR = REPO_ROOT / "deploy" / "spark" / "systemd"
SUDOERS = REPO_ROOT / "deploy" / "spark" / "sudoers" / "forecast-runtime"
MARKER = ".mirror-of-spark"
ALL_SCRIPTS = ["deploy.sh", "env.sh", "mirror_live.sh", "pull_snapshot.sh", "push_snapshot.sh",
               "research_run.sh", "rollback.sh"]
WITH_OPTIONS = [name for name in ALL_SCRIPTS if name != "env.sh"]


def _bash() -> str | None:
    if sys.platform == "win32":
        git_bash = (Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                    / "Git" / "bin" / "bash.exe")
        return str(git_bash) if git_bash.is_file() else None
    return shutil.which("bash")


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash on this machine")


def run_script(name: str, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, (SCRIPTS / name).as_posix(), *args],
                          env={**os.environ, **env}, capture_output=True, text=True, timeout=120)


def sudo_allowed() -> set[tuple[str, str]]:
    """(verb, unit) pairs the sudoers drop-in lets the operator run without a password."""
    text = SUDOERS.read_text(encoding="utf-8")
    return set(re.findall(r"/usr/bin/systemctl (\S+) (\S+?)(?:,|\s|$)", text))


@pytest.fixture
def mirror(tmp_path) -> Path:
    """A root that carries the marker."""
    root = tmp_path / "mirror"
    root.mkdir()
    (root / MARKER).write_text("mirror of spark:/srv/quant_data\n", encoding="utf-8")
    return root


@pytest.fixture
def unmarked(tmp_path) -> Path:
    """A root that does not: it stands for a live root."""
    root = tmp_path / "live"
    root.mkdir()
    (root / "keep.txt").write_text("live state", encoding="utf-8")
    return root


# ----------------------------------------------------------------- every script


def test_the_directory_holds_exactly_these_shell_scripts():
    assert sorted(p.name for p in SCRIPTS.glob("*.sh")) == ALL_SCRIPTS


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_the_script_parses(name):
    done = subprocess.run([BASH, "-n", (SCRIPTS / name).as_posix()],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_the_script_is_lf(name):
    assert b"\r" not in (SCRIPTS / name).read_bytes()


@pytest.mark.parametrize("name", WITH_OPTIONS)
def test_help_prints_the_usage_and_exits_0(name):
    done = run_script(name, "--help")
    assert done.returncode == 0, done.stderr
    assert f"scripts/spark/{name}" in done.stdout


@pytest.mark.parametrize("name", WITH_OPTIONS)
def test_an_unknown_argument_is_a_usage_error(name):
    done = run_script(name, "--no-such-option")
    assert done.returncode == 2
    assert "unknown argument" in done.stderr


# ----------------------------------------------------------------- push_snapshot.sh


def test_push_refuses_a_source_that_is_a_mirror(mirror, tmp_path):
    snapshot = tmp_path / "snapshot"
    done = run_script("push_snapshot.sh", QUANT_DATA_SRC=mirror.as_posix(),
                      QUANT_DATA_SNAPSHOT=snapshot.as_posix())
    assert done.returncode == 3
    assert MARKER in done.stderr and "never pushed" in done.stderr
    assert not snapshot.exists()


def test_push_dry_run_feeds_the_research_mirror_by_default(unmarked, tmp_path):
    done = run_script("push_snapshot.sh", "--dry-run", QUANT_DATA_SRC=unmarked.as_posix(),
                      QUANT_DATA_SNAPSHOT=(tmp_path / "snapshot").as_posix())
    assert done.returncode == 0, done.stderr
    freeze, push, verify = [line for line in done.stdout.splitlines()
                            if line.startswith("DRY-RUN: ")]
    assert "--delete" in freeze and "--delete" not in push
    assert push.endswith(" spark:quant_data/")
    assert " spark cd ~/work/forecast-model " in verify
    assert verify.endswith("scripts/spark/verify_mirror.py --root ~/quant_data")
    assert "climate_data/raw/" in push
    assert not (tmp_path / "snapshot").exists()


def test_push_cutover_goes_into_the_live_root_as_trading(unmarked, tmp_path):
    done = run_script("push_snapshot.sh", "--cutover", "--include-raw", "--dry-run",
                      QUANT_DATA_SRC=unmarked.as_posix(),
                      QUANT_DATA_SNAPSHOT=(tmp_path / "snapshot").as_posix())
    assert done.returncode == 0, done.stderr
    _freeze, push, verify = [line for line in done.stdout.splitlines()
                             if line.startswith("DRY-RUN: ")]
    assert push.endswith(" spark-rt:/srv/quant_data/")
    assert " spark-rt cd /home/trading/forecast-model " in verify
    assert verify.endswith("scripts/spark/verify_mirror.py --root /srv/quant_data")
    assert "climate_data/raw/" not in push


# ----------------------------------------------------------------- pull_snapshot.sh


def test_pull_refuses_a_root_without_the_marker(unmarked):
    done = run_script("pull_snapshot.sh", "--dry-run", QUANT_DATA_MIRROR=unmarked.as_posix())
    assert done.returncode == 3
    assert "does not carry" in done.stderr and MARKER in done.stderr
    assert sorted(p.name for p in unmarked.iterdir()) == ["keep.txt"]


def test_pull_dry_run_copies_the_root_and_the_backups_and_never_deletes(mirror, tmp_path):
    backups = tmp_path / "backups"
    done = run_script("pull_snapshot.sh", "--dry-run", QUANT_DATA_MIRROR=mirror.as_posix(),
                      QUANT_DATA_BACKUPS_MIRROR=backups.as_posix())
    assert done.returncode == 0, done.stderr
    root_line, backups_line = [line for line in done.stdout.splitlines()
                               if line.startswith("DRY-RUN: rsync ")]
    assert f" spark:/srv/quant_data/ {mirror.as_posix()}/" in root_line
    assert f" spark:/srv/quant_data_backups/ {backups.as_posix()}/" in backups_line
    assert "--delete" not in done.stdout
    assert "BatchMode=yes" in root_line and f"/{MARKER}" in root_line
    assert "climate_data/raw/" in root_line
    assert not backups.exists()


def test_pull_include_raw_drops_the_raw_exclude(mirror):
    done = run_script("pull_snapshot.sh", "--dry-run", "--include-raw",
                      QUANT_DATA_MIRROR=mirror.as_posix())
    assert done.returncode == 0, done.stderr
    assert "climate_data/raw/" not in done.stdout


# ----------------------------------------------------------------- mirror_live.sh


def test_mirror_refuses_a_destination_without_the_marker(unmarked, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    done = run_script("mirror_live.sh", "--dry-run", LIVE_ROOT=source.as_posix(),
                      MIRROR_ROOT=unmarked.as_posix())
    assert done.returncode == 3
    assert "does not carry" in done.stderr


def test_mirror_refuses_a_source_that_is_a_mirror(mirror, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / MARKER).write_text("", encoding="utf-8")
    done = run_script("mirror_live.sh", "--dry-run", LIVE_ROOT=other.as_posix(),
                      MIRROR_ROOT=mirror.as_posix())
    assert done.returncode == 3
    assert "not the live root" in done.stderr


def test_mirror_dry_run_reads_the_live_root_and_never_deletes(mirror, unmarked):
    done = run_script("mirror_live.sh", "--dry-run", LIVE_ROOT=unmarked.as_posix(),
                      MIRROR_ROOT=mirror.as_posix())
    assert done.returncode == 0, done.stderr
    (line,) = [line for line in done.stdout.splitlines() if line.startswith("DRY-RUN: rsync ")]
    assert line.endswith(f" {unmarked.as_posix()}/ {mirror.as_posix()}/")
    assert "--delete" not in line and "ibkr_ticks/state/" in line


# ----------------------------------------------------------------- deploy.sh


def test_deploy_dry_run_pushes_then_compares_then_restarts():
    done = run_script("deploy.sh", "--dry-run")
    assert done.returncode == 0, done.stderr
    out = done.stdout
    push = out.index("DRY-RUN: git push spark-rt main")
    compare = out.index("cmp -s")
    wait = out.index("recorder_state.json")
    restart = out.index("sudo -n /usr/bin/systemctl restart ibkr-recorder.service")
    assert push < compare < wait < restart
    assert "/etc/systemd/system/" in out


def test_deploy_no_restart_leaves_the_recorder_alone():
    done = run_script("deploy.sh", "--dry-run", "--no-restart")
    assert done.returncode == 0, done.stderr
    assert "DRY-RUN: git push spark-rt main" in done.stdout
    assert "systemctl restart" not in done.stdout
    assert "--no-restart" in done.stdout


def test_the_restart_a_deploy_asks_for_is_one_sudo_allows():
    done = run_script("deploy.sh", "--dry-run")
    calls = set(re.findall(r"sudo -n /usr/bin/systemctl (\S+) (\S+)", done.stdout))
    assert calls == {("restart", "ibkr-recorder.service")}
    assert calls <= sudo_allowed()


# ----------------------------------------------------------------- rollback.sh


def test_rollback_refuses_a_missing_destination(tmp_path):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=(tmp_path / "absent").as_posix())
    assert done.returncode == 3
    assert "does not exist" in done.stderr


def test_rollback_refuses_a_destination_that_is_a_mirror(mirror):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=mirror.as_posix())
    assert done.returncode == 3
    assert "it is a mirror" in done.stderr


def test_rollback_dry_run_gives_the_session_up_before_it_copies(unmarked):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=unmarked.as_posix())
    assert done.returncode == 0, done.stderr
    out = done.stdout
    stop_units = out.index("/usr/bin/systemctl stop ")
    stop_gateway = out.index("docker compose stop")
    check = out.index("--port 4002 --container ibgw")
    copy = out.index("DRY-RUN: rsync ")
    by_hand = out.index("by hand")
    assert stop_units < stop_gateway < check < copy < by_hand
    copy_line = out[copy:].splitlines()[0]
    assert copy_line.endswith(f" spark:/srv/quant_data/ {unmarked.as_posix()}/")
    assert "--delete" not in copy_line
    assert "session_check.py" in out
    assert sorted(p.name for p in unmarked.iterdir()) == ["keep.txt"]


def test_rollback_stops_every_runtime_unit_with_a_command_sudo_allows(unmarked):
    done = run_script("rollback.sh", "--dry-run", ROLLBACK_DEST=unmarked.as_posix())
    calls = re.findall(r"sudo -n /usr/bin/systemctl (\S+) (\S+)", done.stdout)
    assert calls and {verb for verb, _unit in calls} == {"stop"}
    assert set(calls) <= sudo_allowed()
    runtime_units = {p.name for p in UNIT_DIR.iterdir()} - {"ibgw-owner-rule.service"}
    assert {unit for _verb, unit in calls} == runtime_units
    timers = [i for i, (_verb, unit) in enumerate(calls) if unit.endswith(".timer")]
    services = [i for i, (_verb, unit) in enumerate(calls) if unit.endswith(".service")]
    assert max(timers) < min(services)  # nothing may start a service we have just stopped


# ----------------------------------------------------------------- real runs against stand-ins
#
# A dry run shows the commands; these run the scripts for real, with a stand-in for ssh
# (and for git), to see what they do when the other machine answers badly.


def stand_in(tmp_path: Path, name: str, body: str) -> str:
    """An executable script bash can run in place of ssh or git; returns its POSIX path."""
    path = tmp_path / name
    path.write_bytes(("#!/usr/bin/env bash\n" + body).encode("utf-8"))
    path.chmod(0o755)
    return path.as_posix()


SSH_THAT_OBEYS = 'case "$*" in *"python3 -"*) cat >/dev/null ;; esac\nexit 0\n'


def test_rollback_says_not_to_log_in_when_the_spark_cannot_be_reached(unmarked, tmp_path):
    ssh = stand_in(tmp_path, "ssh_down", "exit 255\n")
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode == 4
    assert "Do NOT log in on the Zenbook" in done.stderr
    assert "rsync" not in done.stdout
    assert sorted(p.name for p in unmarked.iterdir()) == ["keep.txt"]


def test_rollback_says_not_to_log_in_when_the_gateway_cannot_be_stopped(unmarked, tmp_path):
    ssh = stand_in(tmp_path, "ssh_docker_refuses",
                   'case "$*" in *"docker compose stop"*) exit 1 ;; esac\nexit 0\n')
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode == 4
    assert "container could not be stopped" in done.stderr
    assert "Do NOT log in on the Zenbook" in done.stderr


def test_rollback_says_not_to_log_in_when_the_session_check_fails(unmarked, tmp_path):
    ssh = stand_in(tmp_path, "ssh_gateway_still_there",
                   'case "$*" in *"python3 -"*) cat >/dev/null; echo "a Gateway is here"; exit 1 ;; esac\n'
                   "exit 0\n")
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode == 4
    assert "session check on the Spark did not pass" in done.stderr
    assert "Do NOT log in on the Zenbook" in done.stderr


def test_rollback_no_copy_ends_after_the_session_check(tmp_path):
    ssh = stand_in(tmp_path, "ssh_ok", SSH_THAT_OBEYS)
    done = run_script("rollback.sh", "--no-copy", SPARK_SSH=ssh,
                      ROLLBACK_DEST=(tmp_path / "absent").as_posix())  # not needed without a copy
    assert done.returncode == 0, done.stderr
    assert "nothing was copied" in done.stdout
    assert "rsync" not in done.stdout and "by hand" not in done.stdout


def test_a_failed_copy_says_the_session_is_released_and_to_run_again(unmarked, tmp_path):
    # the stand-in obeys but speaks no rsync protocol (and Git Bash has no rsync at all)
    ssh = stand_in(tmp_path, "ssh_ok", SSH_THAT_OBEYS)
    done = run_script("rollback.sh", ROLLBACK_DEST=unmarked.as_posix(), SPARK_SSH=ssh)
    assert done.returncode not in (0, 4)
    assert "run this script again" in done.stderr
    assert "Do NOT log in" not in done.stderr
    assert "by hand" not in done.stdout


DEPLOY_SSH = """\
echo "$*" >> "$STAND_IN_LOG"
case "$*" in
  *"log --oneline"*) echo "abc1234 a commit" ;;
  *"cmp -s"*) [ -z "${STAND_IN_CHANGED:-}" ] || echo "$STAND_IN_CHANGED" ;;
  *"recorder_state.json"*) echo "heartbeat is 30s old" ;;
  *"systemctl restart"*) ;;
  *"is-active ibkr-recorder.service") echo "${STAND_IN_STATE:-active}"; [ "${STAND_IN_STATE:-active}" = active ] || exit 3 ;;
esac
exit 0
"""


def run_deploy(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    log = tmp_path / "calls.log"
    done = run_script("deploy.sh", SSH=stand_in(tmp_path, "ssh_for_deploy", DEPLOY_SSH),
                      DEPLOY_GIT=stand_in(tmp_path, "git_ok", 'echo "git $*"\nexit 0\n'),
                      STAND_IN_LOG=log.as_posix(), **env)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done, calls


def test_deploy_restarts_a_running_recorder_once_after_the_unit_files_agree(tmp_path):
    done, calls = run_deploy(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "git push spark-rt main" in done.stdout
    restarts = [i for i, call in enumerate(calls) if "systemctl restart ibkr-recorder.service" in call]
    compares = [i for i, call in enumerate(calls) if "cmp -s" in call]
    assert len(restarts) == 1 and compares and compares[0] < restarts[0]
    assert calls[restarts[0]].startswith("spark ")  # the operator's host, not the runtime user's


def test_deploy_does_not_start_a_recorder_that_is_not_running(tmp_path):
    done, calls = run_deploy(tmp_path, STAND_IN_STATE="inactive")
    assert done.returncode == 0, done.stderr
    assert "a deploy does not start it" in done.stdout
    assert not [call for call in calls if "systemctl restart" in call]


def test_deploy_stops_for_a_unit_file_that_must_be_installed_by_hand(tmp_path):
    done, calls = run_deploy(tmp_path, STAND_IN_CHANGED="ibkr-health.timer")
    assert done.returncode == 3
    assert ("sudo install -m 644 -o root -g root /home/trading/forecast-model/deploy/spark/systemd/"
            "ibkr-health.timer /etc/systemd/system/ibkr-health.timer") in done.stdout
    assert "sudo systemctl daemon-reload" in done.stdout
    assert not [call for call in calls if "systemctl restart" in call or "recorder_state.json" in call]


# ----------------------------------------------------------------- research_run.sh


def test_research_run_sets_the_ceiling_and_the_kill_priority():
    done = run_script("research_run.sh", "--dry-run", "--", "python", "train.py", "--epochs", "3")
    assert done.returncode == 0, done.stderr
    (line,) = done.stdout.splitlines()
    assert line.startswith("DRY-RUN: systemd-run --user --scope ")
    assert "-p MemoryMax=15G" in line and "-p MemorySwapMax=0" in line
    assert "echo 500 > /proc/self/oom_score_adj" in line
    assert line.endswith(" python train.py --epochs 3")


def test_research_run_takes_a_larger_ceiling_when_told():
    done = run_script("research_run.sh", "--memory", "60G", "--dry-run", "--", "true")
    assert done.returncode == 0, done.stderr
    assert "-p MemoryMax=60G" in done.stdout and "MemoryMax=15G" not in done.stdout


def test_research_run_refuses_a_missing_command_and_a_ceiling_without_a_unit():
    assert run_script("research_run.sh", "--dry-run").returncode == 2
    done = run_script("research_run.sh", "--memory", "15", "--dry-run", "--", "true")
    assert done.returncode == 2
    assert "15G" in done.stderr
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_deploy/test_shell_scripts.py -q`
Expected: the directory test fails (five scripts are missing), so do the tests of the missing scripts (`No such file or directory`, exit 127) and the three `push_snapshot.sh` tests (it does not know `--dry-run` and does not refuse a mirror); `bash -n` and the line-ending test pass for `env.sh` and `push_snapshot.sh` already.

- [ ] **Step 3: Replace `scripts/spark/push_snapshot.sh`**

The whole file becomes:

```bash
#!/usr/bin/env bash
# Push a frozen copy of the Zenbook's quant data to the Spark.
#
# Runs INSIDE WSL Ubuntu (rsync lives there; Git Bash has none). From the Zenbook:
#   PowerShell: wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/spark/push_snapshot.sh [--include-raw] [--cutover] [--dry-run]
#   Git Bash:   wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh" [...]
#               (REPO_WSL from scripts/spark/env.sh starts with //mnt on purpose: Git Bash
#               rewrites a bare /mnt/... argument into a Windows path before wsl.exe sees it)
#
# Until the cutover (docs/plans/2026-10-05-spark-runtime-move-design.md section 6) the
# Zenbook's ~/quant_data is the live root and this script feeds the Spark's research mirror.
# From the cutover on, the Zenbook's root is itself a mirror and carries the marker file
# .mirror-of-spark; this script then REFUSES to run (exit 3), because a mirror is never
# pushed over anything. Data flows from the Spark from then on (pull_snapshot.sh).
#
# Steps (design: docs/plans/2026-09-30-spark-research-split-design.md section 4):
#   1. freeze  ~/quant_data -> ~/quant_data_snapshot   (local, --delete, so the Zenbook-side
#      parity run reads exactly the bytes the Spark gets). parity/ and identity_spark.json
#      inside the snapshot are evidence and survive the --delete. The recorder's state/ dir,
#      *.tmp files and SQLite journals are skipped: they change under our feet, and a file
#      that vanishes mid-copy makes rsync exit 24, which is retried once.
#   2. push    ~/quant_data_snapshot -> spark:quant_data (no --delete: Spark-side
#      refits/experiments may coexist; canonical files are overwritten).
#   3. verify  on the Spark: scripts/spark/verify_mirror.py (exit 1 lists every unreadable
#      file; a push that lands inside the recorder's sub-second parquet write copies a
#      torn file -- re-run the push). The identity goes to a temp file and is moved into
#      place only on success, so a failed verify never leaves an empty identity behind.
#   4. print   the Spark's snapshot identity.
#
# --cutover is for the cutover evening and for seeding before it: the push goes into the
# Spark's live root (/srv/quant_data, as the trading user through the ssh host spark-rt)
# instead of the research mirror, and the verify step runs there from the runtime clone.
# --dry-run prints the three commands and runs nothing.
# climate_data/raw/ (7.6 GB of CDS downloads, re-downloadable) is excluded unless
# --include-raw. Avoid 17:25-17:40 local: the paper-sync task writes the SQLite DB.
set -euo pipefail

SRC="${QUANT_DATA_SRC:-/mnt/c/Users/asus/quant_data}"
SNAP="${QUANT_DATA_SNAPSHOT:-/mnt/c/Users/asus/quant_data_snapshot}"
SSH="${SPARK_SSH:-/mnt/c/Windows/System32/OpenSSH/ssh.exe}"
HOST="${SPARK_HOST:-spark}"
DEST="${SPARK_DEST:-$HOST:quant_data}"
REMOTE_ROOT="${SPARK_REMOTE_ROOT:-~/quant_data}"
REMOTE_REPO="${SPARK_REMOTE_REPO:-~/work/forecast-model}"
REMOTE_PY="${SPARK_REMOTE_PY:-.venv/bin/python}"
MARKER=".mirror-of-spark"

INCLUDE_RAW=0
CUTOVER=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --include-raw) INCLUDE_RAW=1 ;;
    --cutover) CUTOVER=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "push_snapshot: unknown argument: $arg" >&2; exit 2 ;;
  esac
done
if [ "$CUTOVER" -eq 1 ]; then
  HOST=spark-rt
  DEST=spark-rt:/srv/quant_data
  REMOTE_ROOT=/srv/quant_data
  REMOTE_REPO=/home/trading/forecast-model
fi

if [ -e "$SRC/$MARKER" ]; then
  echo "push_snapshot: $SRC carries $MARKER; it is a mirror of the Spark's live root and is never pushed" >&2
  exit 3
fi

EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm")
if [ "$INCLUDE_RAW" -eq 0 ]; then
  EXCLUDES+=(--exclude "climate_data/raw/")
fi
# Evidence that lives inside the snapshot: protected from the freeze's --delete
# (rsync never deletes excluded paths on the receiver) and never pushed.
KEEP=(--exclude "/parity/" --exclude "/identity_spark.json")

FREEZE=(rsync -rlt --delete "${EXCLUDES[@]}" "${KEEP[@]}" "$SRC/" "$SNAP/")
PUSH=(rsync -rlt --stats --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r "${EXCLUDES[@]}" "${KEEP[@]}"
      -e "$SSH" "$SNAP/" "$DEST/")
VERIFY="cd $REMOTE_REPO && $REMOTE_PY scripts/spark/verify_mirror.py --root $REMOTE_ROOT"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${FREEZE[*]}"
  echo "DRY-RUN: ${PUSH[*]}"
  echo "DRY-RUN: $SSH $HOST $VERIFY"
  exit 0
fi

echo "== 1/4 freeze: $SRC -> $SNAP"
mkdir -p "$SNAP"
set +e
"${FREEZE[@]}"
rc=$?
set -e
if [ "$rc" -eq 24 ]; then
  echo "   a source file vanished mid-freeze (rsync 24, a writer raced us); retrying once"
  "${FREEZE[@]}"
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

echo "== 2/4 push: $SNAP -> $DEST"
"${PUSH[@]}"

echo "== 3/4 verify on the Spark"
TMP_ID="$SNAP/identity_spark.json.partial"
if "$SSH" "$HOST" "$VERIFY" > "$TMP_ID"; then
  mv "$TMP_ID" "$SNAP/identity_spark.json"
else
  rc=$?
  rm -f "$TMP_ID"
  echo "push_snapshot: the verify step on the Spark failed (ssh exit $rc; see its output above); the previous identity is untouched" >&2
  exit "$rc"
fi

echo "== 4/4 identity (saved to $SNAP/identity_spark.json)"
cat "$SNAP/identity_spark.json"
```

What changed against the old file, and nothing else: the marker refusal; `--cutover`; `--dry-run`; `HOST` and `REMOTE_ROOT` instead of the literal `spark` and `~/quant_data` in the verify step; the freeze and push commands are built once as arrays so the dry run prints exactly what a real run executes; `--help` prints the whole header.

- [ ] **Step 4: Write `scripts/spark/pull_snapshot.sh` and `scripts/spark/mirror_live.sh`**

`scripts/spark/pull_snapshot.sh`:

```bash
#!/usr/bin/env bash
# Pull the Spark's live root into the Zenbook's mirror: the copy that lives off the Spark.
#
# Runs INSIDE WSL Ubuntu on the Zenbook (rsync lives there; Git Bash has none):
#   PowerShell: wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/spark/pull_snapshot.sh [--include-raw] [--dry-run]
#   Git Bash:   wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/pull_snapshot.sh" [...]
# The scheduled task \Spark\Spark Pull runs it at logon and every six hours
# (scripts/scheduling/run_spark_pull.ps1) and pings an external check when it succeeds.
#
# Two copies, both one way, Spark -> Zenbook (design:
# docs/plans/2026-10-05-spark-runtime-move-design.md section 6):
#   1. spark:/srv/quant_data          -> ~/quant_data          (the mirror)
#   2. spark:/srv/quant_data_backups  -> ~/quant_data_backups  (the daily copies with their
#      manifests; scripts/spark/restore_check.py verifies one of these)
# Rules:
#   * Refuses (exit 3) unless the mirror carries the marker file .mirror-of-spark, so it can
#     never write into a live root.
#   * No --delete, on purpose: this is a backup. If the Spark's root were ever emptied, a
#     mirroring delete would empty this copy too, at the one moment it is needed. The cost:
#     day directories the Spark has rotated away stay here until removed by hand.
#   * Skipped, as in push_snapshot.sh: ibkr_ticks/state/, *.tmp, SQLite journals, and
#     climate_data/raw/ (7.6 GB, re-downloadable) unless --include-raw.
#   * ssh never prompts (BatchMode) and gives up after 20 s: away from the tailnet the pull
#     fails fast, no ping is sent, and the next run tries again.
#   * The paper book arrives as a plain file copy. A pull that lands inside the daily sync
#     can copy it torn: the restore check says so and the next pull replaces it. The copies
#     that are consistent by construction are the day directories of copy 2.
# Exit: 0 done, 2 usage, 3 no marker; any other status is rsync's.
set -euo pipefail

SRC="${SPARK_SRC:-spark:/srv/quant_data}"
DEST="${QUANT_DATA_MIRROR:-/mnt/c/Users/asus/quant_data}"
BACKUPS_SRC="${SPARK_BACKUPS_SRC:-spark:/srv/quant_data_backups}"
BACKUPS_DEST="${QUANT_DATA_BACKUPS_MIRROR:-/mnt/c/Users/asus/quant_data_backups}"
SSH="${SPARK_SSH:-/mnt/c/Windows/System32/OpenSSH/ssh.exe}"
MARKER=".mirror-of-spark"

DRY_RUN=0
INCLUDE_RAW=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --include-raw) INCLUDE_RAW=1 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "pull_snapshot: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$DEST/$MARKER" ]; then
  echo "pull_snapshot: $DEST does not carry $MARKER; refusing to write into a root that is not marked as a mirror" >&2
  exit 3
fi

EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm" --exclude "/$MARKER")
if [ "$INCLUDE_RAW" -eq 0 ]; then
  EXCLUDES+=(--exclude "climate_data/raw/")
fi
RSH="$SSH -o BatchMode=yes -o ConnectTimeout=20"
PULL=(rsync -rlt --stats "${EXCLUDES[@]}" -e "$RSH" "$SRC/" "$DEST/")
# A day directory is built under a .partial name and renamed when complete; skip the former.
PULL_BACKUPS=(rsync -rlt --stats --exclude "*.partial/" -e "$RSH" "$BACKUPS_SRC/" "$BACKUPS_DEST/")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${PULL[*]}"
  echo "DRY-RUN: ${PULL_BACKUPS[*]}"
  exit 0
fi

echo "== 1/2 pull: $SRC -> $DEST"
set +e
"${PULL[@]}"
rc=$?
set -e
if [ "$rc" -eq 24 ]; then
  echo "   a source file vanished mid-pull (rsync 24, a writer raced us); retrying once"
  "${PULL[@]}"
elif [ "$rc" -ne 0 ]; then
  echo "pull_snapshot: the pull failed (rsync exit $rc)" >&2
  exit "$rc"
fi

echo "== 2/2 pull the daily backups: $BACKUPS_SRC -> $BACKUPS_DEST"
mkdir -p "$BACKUPS_DEST"
"${PULL_BACKUPS[@]}"
echo "pull_snapshot: done $(date -u +%FT%TZ)"
```

`scripts/spark/mirror_live.sh`:

```bash
#!/usr/bin/env bash
# Refresh the research mirror from the live root. Runs ON THE SPARK, as the operator.
#
#   bash scripts/spark/mirror_live.sh [--include-raw] [--dry-run]
#
# After the cutover /srv/quant_data is the one live root, written by the trading user only.
# The operator's ~/quant_data is a copy of it for research and carries the marker file
# .mirror-of-spark (design: docs/plans/2026-10-05-spark-runtime-move-design.md section 6).
#   * Refuses (exit 3) to write into a root WITHOUT the marker, so it can never be pointed
#     at a live root; and refuses a source WITH the marker (that is a mirror, not the live
#     root).
#   * No --delete: files that research wrote into the mirror stay; a file the live root
#     also holds is overwritten by the live one.
#   * Skipped, as in push_snapshot.sh: ibkr_ticks/state/, *.tmp, SQLite journals, and
#     climate_data/raw/ unless --include-raw.
# The live root is only read here, through the trading group. Avoid 17:25-17:40 local (the
# paper sync writes the SQLite book), and verify before scoring on the mirror:
#   .venv/bin/python scripts/spark/verify_mirror.py --root ~/quant_data
# Exit: 0 done, 2 usage, 3 refused; any other status is rsync's.
set -euo pipefail

SRC="${LIVE_ROOT:-/srv/quant_data}"
DEST="${MIRROR_ROOT:-$HOME/quant_data}"
MARKER=".mirror-of-spark"

DRY_RUN=0
INCLUDE_RAW=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --include-raw) INCLUDE_RAW=1 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "mirror_live: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$DEST/$MARKER" ]; then
  echo "mirror_live: $DEST does not carry $MARKER; refusing to write into a root that is not marked as a mirror" >&2
  exit 3
fi
if [ -e "$SRC/$MARKER" ]; then
  echo "mirror_live: the source $SRC carries $MARKER; it is a mirror, not the live root" >&2
  exit 3
fi

EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm" --exclude "/$MARKER")
if [ "$INCLUDE_RAW" -eq 0 ]; then
  EXCLUDES+=(--exclude "climate_data/raw/")
fi
COPY=(rsync -rlt --stats "${EXCLUDES[@]}" "$SRC/" "$DEST/")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${COPY[*]}"
  exit 0
fi

set +e
"${COPY[@]}"
rc=$?
set -e
if [ "$rc" -eq 24 ]; then
  echo "   a source file vanished mid-copy (rsync 24, a writer raced us); retrying once"
  "${COPY[@]}"
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
echo "mirror_live: $DEST refreshed from $SRC"
```

- [ ] **Step 5: Write `scripts/spark/deploy.sh`**

```bash
#!/usr/bin/env bash
# Deliver main to the Spark's runtime clone and restart the recorder between two sweeps.
#
# Runs in Git Bash on the Zenbook, from any checkout of this repo:
#   bash scripts/spark/deploy.sh [--no-restart] [--dry-run]
#
# Steps (design: docs/plans/2026-10-05-spark-runtime-move-design.md section 5):
#   1. git push spark-rt main. The runtime clone (/home/trading/forecast-model) has
#      receive.denyCurrentBranch=updateInstead, so the push updates its checked-out main in
#      place; it is refused while a tracked file there has uncommitted changes.
#   2. Compare every unit file of the pushed commit with the installed one under
#      /etc/systemd/system. If one differs or is missing, print the sudo commands that
#      install it and stop with exit 3: installing a unit is the account holder's step.
#      Run the deploy again afterwards.
#   3. Unless --no-restart, and only if the recorder is running: wait until it has just
#      finished a sweep (its heartbeat file is younger than 120 s; at most 22 minutes, one
#      sweep interval plus two), then restart ibkr-recorder.service, so a deploy does not
#      cost a sweep. A recorder that is not running is left alone: a deploy never starts
#      the runtime (before the cutover, and after a rollback, it is stopped on purpose).
#      The timers' services are one-shot and pick the new code up at their next run.
#   4. Print the deployed commit and the units' states.
# Not done here: Python dependencies. When pyproject.toml or the constraints file changed,
# rebuild the runtime clone's venv first (docs/spark_runbook.md section 7.4). The sudoers
# drop-in and the Gateway's compose file are not compared either: a change to one of them
# is installed by hand in the same sitting.
# Exit: 0 done, 2 usage, 3 a unit file must be installed by hand first; any other status is
# the failing step's own.
set -euo pipefail

# Git Bash rewrites arguments that look like POSIX paths before a Windows program (ssh.exe)
# sees them; the remote commands below are full of such paths.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

DRY_RUN=0
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-restart) RESTART=0 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "deploy: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ -z "${SSH:-}" ]; then
  if [ -x /c/Windows/System32/OpenSSH/ssh.exe ]; then
    SSH=/c/Windows/System32/OpenSSH/ssh.exe   # the client the repo's core.sshCommand names
  else
    SSH=ssh
  fi
fi
GIT="${DEPLOY_GIT:-git}"
REMOTE="${DEPLOY_REMOTE:-spark-rt}"       # the git remote and the ssh host of the trading user
OPERATOR="${DEPLOY_OPERATOR:-spark}"      # the ssh host of the operator, who may restart units
CLONE=/home/trading/forecast-model
HEARTBEAT=/srv/quant_data/ibkr_ticks/state/recorder_state.json
UNITS="ibkr-recorder.service ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer"

REPO_ROOT="$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd)"
cd "$REPO_ROOT"

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

# Each of these is one remote command line, run by the Spark's shell.
DIFF_CMD='cd '"$CLONE"' && for f in deploy/spark/systemd/*; do [ -e "$f" ] || continue; n=$(basename "$f"); cmp -s "$f" "/etc/systemd/system/$n" || echo "$n"; done'
STATE_CMD='systemctl is-active ibkr-recorder.service'
WAIT_CMD='f='"$HEARTBEAT"'; i=0; while [ "$i" -lt 132 ]; do age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0) )); if [ "$age" -lt 120 ]; then echo "heartbeat is ${age}s old"; exit 0; fi; i=$((i + 1)); if [ $((i % 6)) -eq 0 ]; then echo "waiting for the sweep to finish (heartbeat ${age}s old)"; fi; sleep 10; done; exit 1'
RESTART_CMD='sudo -n /usr/bin/systemctl restart ibkr-recorder.service'
STATUS_CMD='git -C '"$CLONE"' log --oneline -1; for u in '"$UNITS"'; do echo "$u: $(systemctl is-active "$u")"; done'

echo "== 1/4 push main to $REMOTE"
run "$GIT" push "$REMOTE" main

echo "== 2/4 unit files: the pushed commit against /etc/systemd/system"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: $SSH $REMOTE $DIFF_CMD"
  changed=""
else
  changed=$("$SSH" "$REMOTE" "$DIFF_CMD" | tr -d '\r')
fi
if [ -n "$changed" ]; then
  echo "deploy: these unit files differ from the installed ones, or are not installed:"
  for n in $changed; do echo "  $n"; done
  echo "The account holder installs them on the Spark, then this deploy is run again:"
  for n in $changed; do
    echo "  sudo install -m 644 -o root -g root $CLONE/deploy/spark/systemd/$n /etc/systemd/system/$n"
  done
  echo "  sudo systemctl daemon-reload"
  exit 3
fi

if [ "$RESTART" -eq 0 ]; then
  echo "== 3/4 skipped (--no-restart): the recorder keeps running the code it started with"
elif [ "$DRY_RUN" -eq 1 ]; then
  echo "== 3/4 restart the recorder right after a sweep, if it is running"
  echo "DRY-RUN: $SSH $REMOTE $STATE_CMD"
  echo "DRY-RUN: $SSH $REMOTE $WAIT_CMD"
  echo "DRY-RUN: $SSH $OPERATOR $RESTART_CMD"
else
  echo "== 3/4 restart the recorder right after a sweep, if it is running"
  state=$("$SSH" "$REMOTE" "$STATE_CMD" | tr -d '\r' || true)
  if [ "$state" != "active" ]; then
    echo "deploy: the recorder is not running on the Spark (state: ${state:-unknown}); a deploy does not start it"
  else
    set +e
    "$SSH" -o ServerAliveInterval=30 "$REMOTE" "$WAIT_CMD"
    rc=$?
    set -e
    case "$rc" in
      0) echo "deploy: a sweep has just finished" ;;
      1) echo "deploy: no sweep finished within 22 minutes; the recorder is not completing sweeps, so a restart costs none" ;;
      *) echo "deploy: could not watch the heartbeat (ssh exit $rc); the recorder was NOT restarted" >&2
         exit "$rc" ;;
    esac
    "$SSH" "$OPERATOR" "$RESTART_CMD"
  fi
fi

echo "== 4/4 deployed commit and unit states"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: $SSH $REMOTE $STATUS_CMD"
else
  "$SSH" "$REMOTE" "$STATUS_CMD" || echo "deploy: could not read the units' states" >&2
fi
```

- [ ] **Step 6: Write `scripts/spark/rollback.sh`**

```bash
#!/usr/bin/env bash
# Roll the runtime back from the Spark to the Zenbook: the mirror image of the cutover.
#
# Runs INSIDE WSL Ubuntu on the Zenbook (it needs rsync), like push_snapshot.sh:
#   Git Bash:   wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh" [--no-copy] [--dry-run]
#   PowerShell: wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/spark/rollback.sh [--no-copy] [--dry-run]
#
# The Gateway session is on one machine at a time, so the ORDER below is the point: the
# Spark gives the session up and is checked before anything is copied, and the Zenbook logs
# in only after this script has ended with exit 0 (design:
# docs/plans/2026-10-05-spark-runtime-move-design.md sections 4 and 6).
#   1. Stop the Spark's timers, then its services: one sudo-allowed systemctl call per unit.
#   2. Stop the Gateway container: cd ~/ibgw && docker compose stop.
#   3. Session check on the Spark: nothing listens on 4002 and the container does not run.
#      This machine's own copy of session_check.py is piped to the Spark's python3, so the
#      check does not depend on what any clone over there has checked out.
#   If step 1, 2 or 3 fails -- the Spark cannot be reached, sudo or docker refuse, the check
#   finds a Gateway -- the script stops with exit 4 and says so: nothing has been copied, and
#   nobody may log in on the Zenbook, because the Spark may still hold the session. A Spark
#   that cannot be reached cannot be proved to have let go from here: wait until it answers,
#   or stop the container at the machine itself.
#   4. Copy the Spark's live root into the Zenbook's kept root, ~/quant_data_zenbook_final.
#      No --delete: nothing the Zenbook holds is removed. This is the ONLY place where data
#      is copied from the Spark into a Zenbook live root; it refuses (exit 3) a destination
#      that is missing or that carries the mirror marker. If the copy fails, the session has
#      already been released: run the script again to finish the copy before anyone logs in.
#   5. Print what the account holder does next. None of it is done here.
# --no-copy ends after step 3: the Spark side only, for a rehearsal with everything stopped.
# Exit: 0 done, 2 usage, 3 destination refused, 4 the Spark may still hold the session; any
# other status is rsync's (the copy failed after the session was released).
set -euo pipefail

HOST="${SPARK_HOST:-spark}"
SRC="$HOST:/srv/quant_data"
DEST="${ROLLBACK_DEST:-/mnt/c/Users/asus/quant_data_zenbook_final}"
SSH="${SPARK_SSH:-/mnt/c/Windows/System32/OpenSSH/ssh.exe}"
MARKER=".mirror-of-spark"
TIMERS="ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer"
SERVICES="ibkr-health.service ibkr-paper-sync.service climate-labels.service paper-book-backup.service ibkr-recorder.service"
HERE="$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)"

DRY_RUN=0
COPY=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-copy) COPY=0 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "rollback: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ "$COPY" -eq 1 ]; then
  if [ ! -d "$DEST" ]; then
    echo "rollback: $DEST does not exist; the root kept at the cutover is the only destination" >&2
    exit 3
  fi
  if [ -e "$DEST/$MARKER" ]; then
    echo "rollback: $DEST carries $MARKER; it is a mirror, not the kept live root" >&2
    exit 3
  fi
fi

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

still_held() {
  echo "rollback: $1" >&2
  echo "rollback: the Spark may still hold the Gateway session and nothing was copied. Do NOT log in on the Zenbook." >&2
  exit 4
}

# Timers first, so nothing starts a service that has just been stopped.
STOP_CMD=""
for unit in $TIMERS $SERVICES; do
  STOP_CMD+="sudo -n /usr/bin/systemctl stop $unit && "
done
STOP_CMD+="echo 'all runtime units are stopped'"

echo "== 1/5 stop the Spark's timers and services"
run "$SSH" "$HOST" "$STOP_CMD" || still_held "the Spark's units could not be stopped (exit $?)"

echo "== 2/5 stop the Gateway container"
run "$SSH" "$HOST" "cd ~/ibgw && docker compose stop" \
  || still_held "the Gateway container could not be stopped (exit $?)"

echo "== 3/5 session check on the Spark"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: $SSH $HOST python3 - --port 4002 --container ibgw < $HERE/session_check.py"
elif ! "$SSH" "$HOST" "python3 - --port 4002 --container ibgw" < "$HERE/session_check.py"; then
  still_held "the session check on the Spark did not pass"
fi

if [ "$COPY" -eq 0 ]; then
  echo "== 4/5 and 5/5 skipped (--no-copy): the Spark has given the session up; nothing was copied"
  exit 0
fi

echo "== 4/5 copy $SRC -> $DEST (nothing is deleted there)"
EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm" --exclude "/$MARKER")
COPY_CMD=(rsync -rlt --stats "${EXCLUDES[@]}" -e "$SSH -o BatchMode=yes -o ConnectTimeout=20"
          "$SRC/" "$DEST/")
if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${COPY_CMD[*]}"
else
  set +e
  "${COPY_CMD[@]}"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "rollback: the copy failed (rsync exit $rc). The Spark has given the session up; run this script again to finish the copy before anyone logs in on the Zenbook." >&2
    exit "$rc"
  fi
fi

echo "== 5/5 by hand, in this order (the account holder)"
cat <<'EOF'
  On the Spark, so that a reboot does not start the runtime again:
    sudo systemctl disable ibkr-recorder.service ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer
  On the Zenbook, in an elevated PowerShell, from the repo root:
    Disable-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'
    .\scripts\scheduling\swap_live_root_for_mirror.ps1 -Undo
    Get-ScheduledTask -TaskPath '\IBKR\','\Climate\' | Where-Object TaskName -in 'IBKR Recorder','IBKR Recorder Health','IBKR Paper Sync','Climate Labels' | Enable-ScheduledTask
  Only after the swap has printed "live root:", open IB Gateway on the Zenbook and log in.
  The recorder task starts within five minutes.
EOF
```

`climate_data/raw/` is not excluded here: a forecast downloaded on the Spark during its week must come back, and files that have not changed cost rsync nothing.

- [ ] **Step 7: Write `scripts/spark/research_run.sh`**

The score is written by the job's own shell, not asked of systemd, and that is not a matter of taste. Tried on the Spark on 2026-10-06 (systemd 255): `systemd-run --user --scope -p OOMScoreAdjust=500 -- true` fails with `Unknown assignment: OOMScoreAdjust=500`, because a scope has no exec settings; the command line below, run over a plain `ssh spark`, printed `oom_score_adj=500`, `memory.max=16106127360` and `memory.swap.max=0`.

```bash
#!/usr/bin/env bash
# Run a research job on the Spark under a memory ceiling, as the first thing the kernel
# kills when memory runs out.
#
# Runs ON THE SPARK, as the operator (never as trading), from the research clone:
#   bash scripts/spark/research_run.sh -- .venv/bin/python scripts/fit_gtm_emos.py --trend --root ~/quant_data/climate_data
#   bash scripts/spark/research_run.sh --memory 60G -- .venv/bin/python train.py
#   bash scripts/spark/research_run.sh --dry-run -- COMMAND [ARG...]   # print the command, run nothing
#
# Kill order under memory pressure (design: docs/plans/2026-10-05-spark-runtime-move-design.md
# section 5): research jobs +500, the LLM (qwenflash) -500, the Gateway and the trading
# runtime -900. The kernel kills the highest score first.
#   * MemoryMax (default 15G, what the LLM stack leaves while vLLM is loaded) is a hard
#     ceiling for the job and everything it starts, and MemorySwapMax=0 keeps it from
#     spilling into swap instead. A job that hits the ceiling is killed inside its own scope.
#     Ask for more only after `cd ~/stack && docker compose stop qwenflash`.
#   * The score 500 is written by the job's own shell before it execs the command: a scope
#     unit has no OOMScoreAdjust= property, and raising one's own score needs no privilege.
#     Child processes inherit it.
# The job runs in the foreground of this shell; use tmux for anything long.
# Exit: 2 usage; otherwise the command's own status.
set -euo pipefail

MEMORY="15G"
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --memory)
      [ $# -ge 2 ] || { echo "research_run: --memory needs a value such as 15G" >&2; exit 2; }
      MEMORY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    --) shift; break ;;
    *) echo "research_run: unknown argument: $1 (the command goes after --)" >&2; exit 2 ;;
  esac
done
if [[ ! "$MEMORY" =~ ^[0-9]+[KMGT]$ ]]; then
  echo "research_run: --memory takes a number with a unit, such as 15G (got '$MEMORY')" >&2
  exit 2
fi
if [ $# -eq 0 ]; then
  echo "research_run: no command given; put it after --" >&2
  exit 2
fi

LAUNCH=(systemd-run --user --scope --quiet --collect
        -p "MemoryMax=$MEMORY" -p MemorySwapMax=0 --
        bash -c 'echo 500 > /proc/self/oom_score_adj && exec "$@"' research_run "$@")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${LAUNCH[*]}"
  exit 0
fi
exec "${LAUNCH[@]}"
```

- [ ] **Step 8: Run the tests and watch them pass**

Run: `python -m pytest tests/test_deploy/test_shell_scripts.py -q`
Expected: `54 passed`.

If a test of a script that writes nothing fails with a line-ending error, the file was saved with CRLF: `.gitattributes` already forces LF for `scripts/spark/*.sh` at checkout, so rewrite the file with LF endings and run again.

- [ ] **Step 9: Check the WSL-run scripts inside WSL too**

The tests ran the scripts under Git Bash. `pull_snapshot.sh`, `rollback.sh` and `push_snapshot.sh` run in WSL's bash in real use, so parse them there once (Git Bash, from the worktree, after `source scripts/spark/env.sh`):

```bash
for s in pull_snapshot.sh rollback.sh push_snapshot.sh; do wsl.exe -d Ubuntu -- bash -n "$REPO_WSL/scripts/spark/$s" && echo "$s parses in WSL"; done
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/pull_snapshot.sh" --dry-run; echo "exit=$?"
```

Expected: three `parses in WSL` lines; then, because the Zenbook's `~/quant_data` is still the live root and carries no marker, `pull_snapshot: /mnt/c/Users/asus/quant_data does not carry .mirror-of-spark; refusing ...` and `exit=3`. That refusal on the real live root is the point of the rule.

- [ ] **Step 10: Run everything, then commit**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: green, and `All checks passed!`.

```bash
git add scripts/spark/deploy.sh scripts/spark/pull_snapshot.sh scripts/spark/mirror_live.sh scripts/spark/rollback.sh scripts/spark/research_run.sh scripts/spark/push_snapshot.sh tests/test_deploy/test_shell_scripts.py
git commit -m "Add the deploy, pull, mirror, rollback and research-launcher scripts; the push refuses a mirror"
```

---

### Task 8: The Zenbook's side: the scheduled pull and the root swap

Two things happen on the Zenbook and nowhere else. One is the single most dangerous action of the cutover: renaming the live root away and putting an empty, marked mirror in its place. Typed as four lines it can go wrong in the worst way (a rename Windows refuses, followed by a marker created inside the live root), so it is a script that checks every step, with a test that holds a file open and watches it refuse. The other is the pull.

The copy off the Spark is only as good as its schedule. A Windows task pulls at logon and every six hours while the laptop is on, and the wrapper of Task 3 pings the external check `offbox-pull` after each pull that succeeded. The task's own log shows what happened; the check is what reports a laptop that has stopped pulling.

**Files:**
- Create: `scripts/scheduling/run_spark_pull.ps1`, `scripts/scheduling/register_spark_pull_task.ps1`, `scripts/scheduling/swap_live_root_for_mirror.ps1`
- Modify: `scripts/scheduling/unregister_ibkr_tasks.ps1` (it removes two of the three `\IBKR\` tasks; Task 13 needs all three gone)
- Test: `tests/test_deploy/test_powershell_scripts.py`

**Interfaces:**
- Consumes: `scripts/spark/heartbeat_ping.py --url-env HEARTBEAT_URL_OFFBOX -- COMMAND` (Task 3); `scripts/spark/pull_snapshot.sh` (Task 7).
- Produces: the task `\Spark\Spark Pull`; its log `logs\spark_pull_task.log`; the user-level environment variable name `HEARTBEAT_URL_OFFBOX`, which the account holder sets in Task 11. Tasks 11, 12 and 13 register, enable and read these. `scripts\scheduling\swap_live_root_for_mirror.ps1 [-Undo] [-UserHome DIR]`: exit 0 done, 1 refused (a line starting `swap refused:`, and nothing changed unless it says otherwise); Task 12 runs it forward, a rollback runs it with `-Undo`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deploy/test_powershell_scripts.py`:

```python
"""scripts/scheduling: the Zenbook's scheduled pull, the root swap, and the unregister script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULING = REPO_ROOT / "scripts" / "scheduling"
LAUNCHER = SCHEDULING / "run_spark_pull.ps1"
REGISTER = SCHEDULING / "register_spark_pull_task.ps1"
SWAP = SCHEDULING / "swap_live_root_for_mirror.ps1"
UNREGISTER_IBKR = SCHEDULING / "unregister_ibkr_tasks.ps1"
MARKER = ".mirror-of-spark"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell runs these")


@windows_only
@pytest.mark.parametrize("script", [LAUNCHER, REGISTER, SWAP, UNREGISTER_IBKR],
                         ids=lambda p: p.name)
def test_the_script_parses(script):
    command = ("$tokens = $null; $errors = $null; "
               "[void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{script}', [ref]$tokens, [ref]$errors); $errors.Count")
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=120)
    assert done.stdout.strip() == "0", done.stdout + done.stderr


def test_the_launcher_pulls_through_the_ping_wrapper_and_holds_no_url():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "scripts\\spark\\heartbeat_ping.py --url-env HEARTBEAT_URL_OFFBOX -- wsl.exe" in text
    assert "/scripts/spark/pull_snapshot.sh" in text
    assert "spark_pull_task.log" in text
    assert "exit $LASTEXITCODE" in text
    assert "http://" not in text and "https://" not in text


def test_the_task_runs_at_logon_and_every_six_hours_and_catches_up():
    text = REGISTER.read_text(encoding="utf-8")
    assert '"\\Spark\\"' in text and '"Spark Pull"' in text
    assert "-AtLogOn" in text
    assert "-RepetitionInterval (New-TimeSpan -Hours 6)" in text
    for setting in ("-StartWhenAvailable", "-RunOnlyIfNetworkAvailable", "-AllowStartIfOnBatteries",
                    "-MultipleInstances IgnoreNew", "-ExecutionTimeLimit (New-TimeSpan -Hours 1)"):
        assert setting in text, setting
    assert "run_spark_pull.ps1" in text
    assert "http://" not in text and "https://" not in text


def test_the_ibkr_unregister_script_removes_all_three_tasks():
    text = UNREGISTER_IBKR.read_text(encoding="utf-8")
    for name in ('"IBKR Recorder"', '"IBKR Recorder Health"', '"IBKR Paper Sync"'):
        assert name in text, name


# ----------------------------------------------------------------- the root swap, run for real


def swap(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                           "Bypass", "-File", str(SWAP), "-UserHome", str(home), *args],
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
def home(tmp_path) -> Path:
    """A home directory whose quant_data is a live root with one file of state."""
    lane = tmp_path / "home" / "quant_data" / "ibkr_ticks"
    lane.mkdir(parents=True)
    (lane / "live_state.txt").write_text("live", encoding="utf-8")
    return tmp_path / "home"


@windows_only
def test_the_swap_keeps_the_live_root_and_leaves_an_empty_marked_mirror(home):
    done = swap(home)
    assert done.returncode == 0, done.stdout + done.stderr
    kept = home / "quant_data_zenbook_final" / "ibkr_ticks" / "live_state.txt"
    assert kept.read_text(encoding="utf-8") == "live"
    assert sorted(p.name for p in (home / "quant_data").iterdir()) == [MARKER]


@windows_only
def test_a_rename_windows_refuses_never_puts_the_marker_into_the_live_root(home):
    held = home / "quant_data" / "ibkr_ticks" / "live_state.txt"
    with held.open("rb"):  # a file held open inside the root: the folder cannot be renamed
        done = swap(home)
    assert done.returncode == 1
    assert "swap refused" in done.stdout and "nothing was changed" in done.stdout
    assert not (home / "quant_data" / MARKER).exists()
    assert not (home / "quant_data_zenbook_final").exists()
    assert held.read_text(encoding="utf-8") == "live"


@windows_only
def test_a_second_swap_is_refused_and_changes_nothing(home):
    assert swap(home).returncode == 0
    done = swap(home)
    assert done.returncode == 1 and "swap refused" in done.stdout
    assert sorted(p.name for p in home.iterdir()) == ["quant_data", "quant_data_zenbook_final"]
    assert sorted(p.name for p in (home / "quant_data").iterdir()) == [MARKER]


@windows_only
def test_undo_puts_the_kept_root_back_and_retires_the_mirror(home):
    assert swap(home).returncode == 0
    (home / "quant_data" / "pulled.txt").write_text("a pulled copy", encoding="utf-8")

    done = swap(home, "-Undo")

    assert done.returncode == 0, done.stdout + done.stderr
    assert (home / "quant_data" / "ibkr_ticks" / "live_state.txt").read_text(encoding="utf-8") == "live"
    assert not (home / "quant_data" / MARKER).exists()
    assert (home / "quant_data_mirror_retired" / "pulled.txt").is_file()
    assert not (home / "quant_data_zenbook_final").exists()


@windows_only
def test_undo_refuses_to_move_a_root_that_is_not_the_mirror(home):
    (home / "quant_data_zenbook_final").mkdir()  # a kept root from somewhere; quant_data is still live

    done = swap(home, "-Undo")

    assert done.returncode == 1
    assert "is not the mirror" in done.stdout
    assert (home / "quant_data" / "ibkr_ticks" / "live_state.txt").is_file()
    assert (home / "quant_data_zenbook_final").is_dir()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_deploy/test_powershell_scripts.py -q`
Expected: the three new files are missing (`FileNotFoundError`, a parse-error count that is not `0`, and the five swap tests fail because PowerShell cannot find the script), and the unregister test fails on `"IBKR Paper Sync"`; the parse test of `unregister_ibkr_tasks.ps1` passes already.

- [ ] **Step 3: Write the launcher**

`scripts/scheduling/run_spark_pull.ps1`:

```powershell
# Launcher for the 'Spark Pull' scheduled task (\Spark\Spark Pull).
#
# Pulls the Spark's live root and its daily backups into this machine's mirror
# (%USERPROFILE%\quant_data, which carries the marker file .mirror-of-spark)
# and, when the pull succeeded, pings the external check 'offbox-pull'. A
# laptop that has not pulled for three days is reported by that check, not by
# this script (docs/plans/2026-10-05-spark-runtime-move-design.md sections 6
# and 8).
#
# The ping address is a capability. scripts\spark\heartbeat_ping.py reads it
# from the user-level environment variable HEARTBEAT_URL_OFFBOX and never
# prints it; it is not written in this file, in the task definition or in the
# log.
#
# stdout/stderr append to logs\spark_pull_task.log (rotated to .1 at 50 MB).
# The exit code is the pull's: 0 done, 3 the mirror carries no marker (the
# pull refused; nothing was written), anything else is rsync's or ssh's.

$ErrorActionPreference = 'Continue'

# Repo root is two levels above this script (scripts\scheduling\..\..).
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot

$logDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'spark_pull_task.log'

# Size guard: rotate to a single .1 generation once the log passes 50 MB.
if (Test-Path $logFile) {
    if ((Get-Item $logFile).Length -gt 50MB) {
        try { Move-Item -Force $logFile "$logFile.1" } catch { }
    }
}

Add-Content -Path $logFile -Value ("--- task launch {0:yyyy-MM-dd HH:mm:ss}Z ---" -f (Get-Date).ToUniversalTime())

# rsync lives in WSL, so the pull runs there. The repo as WSL sees it:
# C:\a\b -> /mnt/c/a/b. The path must not contain a space (it does not).
$repoWsl = '/mnt/' + $repoRoot.Substring(0, 1).ToLower() + ($repoRoot.Substring(2) -replace '\\', '/')

# cmd.exe does the redirection on purpose: Windows PowerShell 5.1 wraps a
# native command's redirected stderr lines in ErrorRecords, which garbles the
# log (see run_ibkr_sync.ps1). cmd appends both streams verbatim and passes
# the exit code through.
& cmd.exe /d /c "python -u scripts\spark\heartbeat_ping.py --url-env HEARTBEAT_URL_OFFBOX -- wsl.exe -d Ubuntu -- bash $repoWsl/scripts/spark/pull_snapshot.sh >> `"$logFile`" 2>&1"
exit $LASTEXITCODE
```

- [ ] **Step 4: Write the registration script and fix the unregister script**

`scripts/scheduling/register_spark_pull_task.ps1`:

```powershell
# Register the Zenbook's scheduled pull of the Spark's live root (\Spark\Spark Pull).
#
# Run from an ELEVATED PowerShell with the repo root as the current directory:
#
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\register_spark_pull_task.ps1
#
# Registration ONLY -- nothing is pulled here. The task runs at logon and every
# six hours while the laptop is on. A run missed while it slept starts when it
# wakes (StartWhenAvailable) and waits for a network
# (RunOnlyIfNetworkAvailable). A run that fails -- the tailnet is not up yet,
# the Spark is unreachable -- is retried three times, ten minutes apart, and
# after that simply waits for the next tick: the external check 'offbox-pull'
# is what reports a copy that is getting old.
#
# Before the first run:
#   * the account holder sets the ping address for this Windows user, typed at a
#     prompt so that it does not land in the shell's history file:
#       [Environment]::SetEnvironmentVariable('HEARTBEAT_URL_OFFBOX', (Read-Host 'ping address of offbox-pull'), 'User')
#   * the mirror carries its marker (the pull refuses otherwise): a cutover step.
#
# Remove:  Unregister-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull' -Confirm:$false

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$TaskPath = "\Spark\"
)

# Interactive, as for the IBKR tasks: S4U never executed an action on this
# machine (register_ibkr_tasks.ps1 records the finding), and the pull needs the
# user's ssh key and WSL, which live in the logged-on session.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Seed the repeating trigger at the next top of the hour.
$now = Get-Date
$seed = $now.AddMinutes(-$now.Minute).AddSeconds(-$now.Second).AddMilliseconds(-$now.Millisecond).AddHours(1)

$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn
    New-ScheduledTaskTrigger `
        -Once `
        -At $seed `
        -RepetitionInterval (New-TimeSpan -Hours 6) `
        -RepetitionDuration ([TimeSpan]::FromDays(3650))
)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$launcher = Join-Path $RepoRoot 'scripts\scheduling\run_spark_pull.ps1'
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`"" `
    -WorkingDirectory $RepoRoot

# Idempotent: replace any existing registration with the same name.
Unregister-ScheduledTask -TaskPath $TaskPath -TaskName "Spark Pull" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "Spark Pull" `
    -TaskPath $TaskPath `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Pulls the Spark's live quant-data root and its daily backups into this machine's mirror; pings the external check offbox-pull when the pull succeeded." | Out-Null

Write-Host "Registered: ${TaskPath}Spark Pull (at logon and every 6 hours; first tick at $seed)"
Write-Host "Inspect:  Get-ScheduledTask -TaskPath '\Spark\' | Get-ScheduledTaskInfo"
Write-Host "Tail log: Get-Content logs\spark_pull_task.log -Tail 20"
```

In `scripts/scheduling/unregister_ibkr_tasks.ps1` change two things: the first comment line becomes

```powershell
# Remove the IBKR recorder, health-check and paper-sync jobs from Windows Task Scheduler.
```

and the list becomes

```powershell
$taskNames = @("IBKR Recorder", "IBKR Recorder Health", "IBKR Paper Sync")
```

- [ ] **Step 5: Write the root swap**

`scripts/scheduling/swap_live_root_for_mirror.ps1`:

```powershell
# Turn this machine's live quant-data root into the kept root and put an empty,
# marked mirror in its place -- or, with -Undo, put the kept root back.
#
# Run in PowerShell from the repo root. Forward: at the cutover (plan Task 12),
# only after the Zenbook's Gateway is closed, its four scheduled tasks are
# disabled and the final push has been verified. -Undo: at a rollback, after
# scripts/spark/rollback.sh has ended with exit 0.
#
#   .\scripts\scheduling\swap_live_root_for_mirror.ps1          # quant_data -> quant_data_zenbook_final; a new quant_data with the marker
#   .\scripts\scheduling\swap_live_root_for_mirror.ps1 -Undo    # the mirror -> quant_data_mirror_retired; the kept root -> quant_data
#
# Every step is checked and the first failure ends the script with exit 1 and a
# line that starts "swap refused:". The reason this is a script and not four
# pasted lines: a rename that Windows refuses (some program holds a file open
# inside the root) is a non-terminating error, and the lines after it would
# then create the marker file inside the LIVE root. The pull would take that
# root for a mirror and write into it, and the rollback would have no kept
# root to go back to. Close anything that has a file open under the root first.
param(
    [switch]$Undo,
    [string]$UserHome = $env:USERPROFILE
)

$ErrorActionPreference = 'Stop'
$marker  = '.mirror-of-spark'
$root    = Join-Path $UserHome 'quant_data'
$kept    = Join-Path $UserHome 'quant_data_zenbook_final'
$retired = Join-Path $UserHome 'quant_data_mirror_retired'

function Stop-Swap([string]$message) {
    Write-Host "swap refused: $message"
    exit 1
}

if (-not $Undo) {
    if (-not (Test-Path $root -PathType Container)) { Stop-Swap "$root does not exist" }
    if (Test-Path (Join-Path $root $marker))        { Stop-Swap "$root already carries $marker, so it is a mirror already" }
    if (Test-Path $kept)                            { Stop-Swap "$kept already exists" }
    try {
        Rename-Item -Path $root -NewName 'quant_data_zenbook_final'
    } catch {
        Stop-Swap "Windows would not rename $root ($($_.Exception.Message)); nothing was changed"
    }
    if ((Test-Path $root) -or -not (Test-Path $kept -PathType Container)) {
        Stop-Swap "the rename did not take effect; look at $UserHome before doing anything else"
    }
    New-Item -ItemType Directory -Path $root | Out-Null
    Set-Content -Path (Join-Path $root $marker) -Value ("mirror of spark:/srv/quant_data, created {0:s}" -f (Get-Date))
    Write-Host "kept root:  $kept"
    Write-Host "new mirror: $root (empty, carries $marker)"
    exit 0
}

if (-not (Test-Path $kept -PathType Container)) { Stop-Swap "$kept does not exist, so there is no kept root to put back" }
if (Test-Path (Join-Path $kept $marker))         { Stop-Swap "$kept carries $marker, so it is not the kept live root" }
if (Test-Path $retired)                          { Stop-Swap "$retired already exists" }
if (Test-Path $root) {
    if (-not (Test-Path (Join-Path $root $marker))) {
        Stop-Swap "$root does not carry $marker, so it is not the mirror; nothing was changed"
    }
    try {
        Rename-Item -Path $root -NewName 'quant_data_mirror_retired'
    } catch {
        Stop-Swap "Windows would not rename the mirror ($($_.Exception.Message)); nothing was changed"
    }
    if (Test-Path $root) { Stop-Swap "the mirror is still at $root; nothing else was changed" }
}
try {
    Rename-Item -Path $kept -NewName 'quant_data'
} catch {
    Stop-Swap "Windows would not rename the kept root back ($($_.Exception.Message)); the mirror is now at $retired"
}
Write-Host "live root:      $root"
Write-Host "retired mirror: $retired"
exit 0
```

- [ ] **Step 6: Run the tests and watch them pass**

Run: `python -m pytest tests/test_deploy/test_powershell_scripts.py -q`
Expected on the Zenbook: `12 passed`. (On the Spark only the three tests that read text run: `3 passed, 9 skipped`.) The swap tests run the script for real, in a throwaway home directory; one of them holds a file open inside the root and expects the refusal.

- [ ] **Step 7: Run the launcher once by hand, before any marker exists**

From the worktree, in PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\scheduling\run_spark_pull.ps1; "exit=$LASTEXITCODE"
Get-Content logs\spark_pull_task.log -Tail 4
```

Expected: `exit=3`, and the log ends with a `--- task launch ... ---` line, the line `pull_snapshot: /mnt/c/Users/asus/quant_data does not carry .mirror-of-spark; refusing to write into a root that is not marked as a mirror` and `heartbeat: not sent (command exit 3)`. The Zenbook's root is still the live root, so the refusal is correct, nothing was written and no ping left the machine. This run proves the chain PowerShell, cmd, python, WSL, bash end to end. Nothing is registered in this task.

- [ ] **Step 8: Commit**

```bash
git add scripts/scheduling/run_spark_pull.ps1 scripts/scheduling/register_spark_pull_task.ps1 scripts/scheduling/swap_live_root_for_mirror.ps1 scripts/scheduling/unregister_ibkr_tasks.ps1 tests/test_deploy/test_powershell_scripts.py
git commit -m "Add the Zenbook's scheduled pull and the checked swap of its live root for a mirror"
```

---

### Task 9: The cutover check

The cutover gate (spec section 6, step 5) has four lines, and "every line must hold, otherwise roll back" is too important to read off a screen. This script evaluates them from what the runtime itself leaves behind and prints one verdict per line.

One trap decides its design: the health probe writes a line to `logs/ibkr_health.log` only for a result that is NOT healthy, and one per alert attempt; a healthy run leaves nothing there. So "the probe is healthy" cannot be read from that log. It is read from systemd: the health unit's last exit status and when it exited.

**Files:**
- Create: `scripts/spark/cutover_check.py`
- Test: `tests/test_spark/test_cutover_check.py`

**Interfaces:**
- Consumes: `scripts/spark/compare_json.py` (`main(argv) -> int`, exit 0 = parity) with the registered gate flags `--rtol 1e-8 --atol 1e-10 --min-numeric 1000 --ignore identity.machine --differ identity.machine`; the unit name `ibkr-health.service` (Task 6), asked through `systemctl show ibkr-health.service --timestamp=unix -p ExecMainStatus -p ExecMainExitTimestamp`; the probe's durable log `logs/ibkr_health.log` (`<ISO time> <STATUS> <text>` per non-OK result, `<ISO time> ALERT_SENT test - telegram` for a delivered test alert); the sync ledger `ibkr_ticks/paper/ibkr_sync_runs.jsonl` (fields `ts_utc`, `ok` and the six counts).
- Produces: CLI `python scripts/spark/cutover_check.py --root ROOT --repo REPO --since ISO_UTC --parity ZENBOOK.json SPARK.json`; exit `0` only when all four lines pass. Function `main(argv=None, run=subprocess.run) -> int`. Tasks 11 and 12 run the CLI on the Spark.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_spark/test_cutover_check.py`:

```python
"""scripts/spark/cutover_check.py: the four lines of the cutover gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import cutover_check  # noqa: E402

SINCE = "2026-10-20T16:00:00+00:00"
BEFORE = "2026-10-19T14:30:00+00:00"
AFTER = "2026-10-20T16:20:00+00:00"
COUNTS = {"examined": 3, "filled": 0, "partially_filled": 0, "cancelled": 0, "unchanged": 3,
          "unmatched_venue_orders": 0}


def _sync(ts: str, ok: bool = True, **changes) -> str:
    return json.dumps({"ts_utc": ts, "ok": ok, **COUNTS, **changes})


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).timestamp()


def _systemd(status: int, exited: str | None):
    """A stand-in for subprocess.run that answers `systemctl show ibkr-health.service ...`."""

    def run(cmd, **_kwargs):
        assert cmd[:3] == ["systemctl", "show", "ibkr-health.service"], cmd
        stamp = "" if exited is None else f"@{int(_epoch(exited))}"
        text = f"ExecMainExitTimestamp={stamp}\nExecMainStatus={status}\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

    return run


@pytest.fixture
def site(tmp_path):
    """A live root and a repo directory in the state a good cutover leaves them in."""
    root = tmp_path / "quant_data"
    sweep = root / "ibkr_ticks" / "quotes" / "date=2026-10-20" / "quotes-161500Z.parquet"
    sweep.parent.mkdir(parents=True)
    pd.DataFrame({"con_id": range(2400), "bid": [0.4] * 2400}).to_parquet(sweep, index=False)
    os.utime(sweep, (_epoch(AFTER), _epoch(AFTER)))
    paper = root / "ibkr_ticks" / "paper"
    paper.mkdir(parents=True)
    (paper / "ibkr_sync_runs.jsonl").write_text(
        _sync(BEFORE) + "\n" + _sync(AFTER) + "\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "logs").mkdir(parents=True)
    # A healthy run writes nothing here; the DOWN line is from before the cutover.
    (repo / "logs" / "ibkr_health.log").write_text(
        f"{BEFORE} DOWN ibkr recorder DOWN: unexpected disconnect\n"
        f"{AFTER} ALERT_SENT test - telegram\n", encoding="utf-8")
    same = {"x": [float(i) for i in range(1200)], "identity": {"machine": {"python": "3.14.2"}}}
    left = tmp_path / "zenbook.json"
    right = tmp_path / "spark.json"
    left.write_text(json.dumps(same), encoding="utf-8")
    other = dict(same, identity={"machine": {"python": "3.14.2", "machine": "aarch64"}})
    right.write_text(json.dumps(other), encoding="utf-8")
    return {"root": root, "repo": repo, "sweep": sweep, "paper": paper,
            "health": repo / "logs" / "ibkr_health.log", "left": left, "right": right}


def _run(site, capsys, systemd=None, parity: bool = True) -> tuple[int, str]:
    argv = ["--root", str(site["root"]), "--repo", str(site["repo"]), "--since", SINCE]
    if parity:
        argv += ["--parity", str(site["left"]), str(site["right"])]
    rc = cutover_check.main(argv, run=systemd or _systemd(0, AFTER))
    return rc, capsys.readouterr().out


def test_a_good_cutover_passes_every_line(site, capsys):
    rc, out = _run(site, capsys)
    assert rc == 0, out
    lines = out.splitlines()
    assert [line.split()[0] for line in lines[:4]] == ["PASS"] * 4
    assert [line.split()[1] for line in lines[:4]] == ["sweep:", "health:", "sync:", "parity:"]
    assert lines[4] == "CUTOVER GATE: PASS"


def test_a_sweep_from_before_the_cutover_does_not_count(site, capsys):
    os.utime(site["sweep"], (_epoch(BEFORE), _epoch(BEFORE)))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep" in out and "CUTOVER GATE: FAIL" in out


def test_a_thin_sweep_does_not_count(site, capsys):
    pd.DataFrame({"con_id": range(1999)}).to_parquet(site["sweep"], index=False)
    os.utime(site["sweep"], (_epoch(AFTER), _epoch(AFTER)))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep" in out and "1999" in out


def test_a_probe_that_found_a_problem_fails_the_gate_and_says_which(site, capsys):
    with site["health"].open("a", encoding="utf-8") as log:
        log.write(f"{AFTER} STALE ibkr recorder STALE: heartbeat is 70 min old\n")
    rc, out = _run(site, capsys, systemd=_systemd(1, AFTER))
    assert rc == 1
    assert "FAIL health" in out and "exit status is 1" in out and "STALE" in out


def test_a_probe_that_has_not_run_since_the_cutover_does_not_count(site, capsys):
    rc, out = _run(site, capsys, systemd=_systemd(0, BEFORE))
    assert rc == 1
    assert "FAIL health" in out and "has not finished a run since the cutover" in out

    rc, out = _run(site, capsys, systemd=_systemd(0, None))  # the unit never ran at all
    assert rc == 1
    assert "has not finished a run since the cutover" in out


def test_systemd_that_cannot_be_asked_is_not_read_as_healthy(site, capsys):
    def no_systemctl(cmd, **_kwargs):
        raise FileNotFoundError("systemctl")

    rc, out = _run(site, capsys, systemd=no_systemctl)
    assert rc == 1
    assert "FAIL health" in out and "could not ask systemd" in out


def test_a_test_alert_that_only_reached_the_log_does_not_count(site, capsys):
    site["health"].write_text(f"{AFTER} ALERT_SENT test - log_only\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL health" in out and "no delivered test alert" in out


def test_the_sync_must_have_run_since_and_report_the_same_book(site, capsys):
    ledger = site["paper"] / "ibkr_sync_runs.jsonl"
    ledger.write_text(_sync(BEFORE) + "\n" + _sync(AFTER, unchanged=2, cancelled=1) + "\n",
                      encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and "cancelled" in out

    ledger.write_text(_sync(BEFORE) + "\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and "no sync run since" in out

    ledger.write_text(_sync(BEFORE) + "\n" + _sync(AFTER, ok=False) + "\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and "ok is false" in out


def test_a_parity_mismatch_fails_the_gate(site, capsys):
    payload = json.loads(site["right"].read_text(encoding="utf-8"))
    payload["x"][0] += 1.0
    site["right"].write_text(json.dumps(payload), encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL parity" in out


def test_without_the_parity_payloads_the_gate_cannot_pass(site, capsys):
    rc, out = _run(site, capsys, parity=False)
    assert rc == 1
    assert "FAIL parity" in out and "not checked" in out
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_spark/test_cutover_check.py -q`
Expected: `ModuleNotFoundError: No module named 'cutover_check'`.

- [ ] **Step 3: Write `scripts/spark/cutover_check.py`**

```python
"""Evaluate the cutover gate from what the runtime leaves behind.

    python scripts/spark/cutover_check.py --root /srv/quant_data --repo /home/trading/forecast-model \\
        --since 2026-10-20T16:00:00+00:00 --parity zenbook.json spark.json

Four lines, every one of which must hold (spec section 6, step 5), all judged
on what happened at or after --since, the moment the Spark's units started:

  sweep   a sweep file with at least MIN_SWEEP_ROWS rows was written;
  health  the health unit finished a run with exit status 0, and a test alert
          was delivered by Telegram;
  sync    the paper sync ran, is ok, and reports the same book as the last ok
          run before --since (the Zenbook's);
  parity  the two score payloads pass the registered parity gate.

The probe writes to logs/ibkr_health.log only when it finds a problem, so a
healthy run is read from systemd (the unit's last exit status and exit time),
not from that log. Run it on the Spark. It reads; it runs nothing against the
venue and sends nothing. Exit 0 when all four pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import compare_json  # noqa: E402

MIN_SWEEP_ROWS = 2000
HEALTH_UNIT = "ibkr-health.service"
PARITY_FLAGS = ["--rtol", "1e-8", "--atol", "1e-10", "--min-numeric", "1000",
                "--ignore", "identity.machine", "--differ", "identity.machine"]
SYNC_COUNTS = ("examined", "filled", "partially_filled", "cancelled", "unchanged",
               "unmatched_venue_orders")


def _when(text: str) -> datetime:
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def check_sweep(root: Path, since: datetime) -> tuple[bool, str]:
    files = sorted((root / "ibkr_ticks" / "quotes").glob("date=*/*.parquet"),
                   key=lambda p: p.stat().st_mtime)
    fresh = [p for p in files
             if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= since]
    if not fresh:
        return False, "no sweep file written since the cutover"
    rows = pq.read_metadata(fresh[-1]).num_rows
    if rows < MIN_SWEEP_ROWS:
        return False, f"{fresh[-1].name} has {rows} rows, under {MIN_SWEEP_ROWS}"
    return True, f"{fresh[-1].name}, {rows} rows"


def _health_unit_last_run(run) -> tuple[int | None, datetime | None]:
    """(exit status, exit time) of the health unit's last run; the time is None if it never ran."""
    done = run(["systemctl", "show", HEALTH_UNIT, "--timestamp=unix",
                "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp"],
               capture_output=True, text=True, timeout=20)
    if done.returncode != 0:
        raise OSError(f"systemctl exit {done.returncode}")
    fields = dict(line.split("=", 1) for line in (done.stdout or "").splitlines() if "=" in line)
    raw = fields.get("ExecMainExitTimestamp", "").strip()
    if raw in ("", "n/a"):
        exited = None
    else:
        stamp = re.fullmatch(r"@(\d+)", raw)
        if not stamp:
            raise ValueError(f"unreadable exit time {raw!r}")
        exited = datetime.fromtimestamp(int(stamp.group(1)), tz=timezone.utc)
    status = fields.get("ExecMainStatus", "").strip()
    return (int(status) if status.lstrip("-").isdigit() else None), exited


def _health_log_since(repo: Path, since: datetime) -> tuple[str | None, bool]:
    """(last non-OK status line, whether a test alert was delivered by Telegram), since."""
    log = repo / "logs" / "ibkr_health.log"
    last_problem, test_alert = None, False
    if not log.is_file():
        return last_problem, test_alert
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            if _when(parts[0]) < since:
                continue
        except ValueError:
            continue
        if parts[1] == "ALERT_SENT":
            if parts[2:5] == ["test", "-", "telegram"]:
                test_alert = True
        elif not parts[1].startswith("ALERT_"):
            last_problem = " ".join(parts[1:])
    return last_problem, test_alert


def check_health(repo: Path, since: datetime, run=subprocess.run) -> tuple[bool, str]:
    last_problem, test_alert = _health_log_since(repo, since)
    problems = []
    exited = None
    try:
        status, exited = _health_unit_last_run(run)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        problems.append(f"could not ask systemd about {HEALTH_UNIT} ({type(exc).__name__}: {exc})")
    else:
        if exited is None or exited < since:
            problems.append(f"{HEALTH_UNIT} has not finished a run since the cutover")
        elif status != 0:
            detail = f": {last_problem}" if last_problem else ""
            problems.append(f"the probe's last exit status is {status}{detail}")
    if not test_alert:
        problems.append("no delivered test alert since the cutover")
    if problems:
        return False, "; ".join(problems)
    return True, f"probe exit 0 at {exited:%H:%M:%S}Z, test alert delivered by telegram"


def check_sync(root: Path, since: datetime) -> tuple[bool, str]:
    ledger = root / "ibkr_ticks" / "paper" / "ibkr_sync_runs.jsonl"
    if not ledger.is_file():
        return False, f"{ledger} is missing"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    after = [r for r in records if _when(r["ts_utc"]) >= since]
    before_ok = [r for r in records if _when(r["ts_utc"]) < since and r.get("ok")]
    if not after:
        return False, "no sync run since the cutover"
    last = after[-1]
    if not last.get("ok"):
        return False, "the last sync run's ok is false"
    if not before_ok:
        return False, "no earlier ok run to compare the book with"
    reference = before_ok[-1]
    differing = [name for name in SYNC_COUNTS if last.get(name) != reference.get(name)]
    if differing:
        detail = ", ".join(f"{name} {reference.get(name)} -> {last.get(name)}"
                           for name in differing)
        return False, f"the book differs from the last run before the cutover: {detail}"
    return True, "ok, same counts as the last run before the cutover"


def check_parity(payloads: list[str] | None) -> tuple[bool, str]:
    if not payloads:
        return False, "not checked: pass --parity ZENBOOK.json SPARK.json"
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = compare_json.main([*payloads, *PARITY_FLAGS])
    lines = captured.getvalue().strip().splitlines()
    return code == 0, lines[-1] if lines else f"compare_json exit {code}"


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="the live quant-data root")
    parser.add_argument("--repo", required=True, help="the runtime clone (its logs/ is read)")
    parser.add_argument("--since", required=True,
                        help="ISO time the Spark's units were started, with its UTC offset")
    parser.add_argument("--parity", nargs=2, metavar=("ZENBOOK_JSON", "SPARK_JSON"), default=None)
    args = parser.parse_args(argv)
    since = _when(args.since)
    root, repo = Path(args.root).expanduser(), Path(args.repo).expanduser()

    results = [("sweep", *check_sweep(root, since)), ("health", *check_health(repo, since, run)),
               ("sync", *check_sync(root, since)), ("parity", *check_parity(args.parity))]
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
    verdict = all(passed for _name, passed, _detail in results)
    print("CUTOVER GATE: " + ("PASS" if verdict else "FAIL -- roll back"))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
```

The sync ledger's lines also carry the account id; this script reads the counts and never prints a line of it.

- [ ] **Step 4: Run them and watch them pass**

Run: `python -m pytest tests/test_spark/test_cutover_check.py -q`
Expected: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/spark/cutover_check.py tests/test_spark/test_cutover_check.py
git commit -m "Add the cutover check that evaluates the four lines of the gate"
```

---

### Task 10: The documents

The operator's view of the runtime on the Spark is written now, while the Zenbook is still the runtime. So this task only ADDS: a new section 7 to the Spark runbook that says of itself that it is not yet in force, and short notices elsewhere. The text that describes the Zenbook stays untouched until the cutover has passed its gate; Task 12 flips it then, and Task 13 retires it.

**Files:**
- Modify: `docs/spark_runbook.md` (a notice under the title; new section 7)
- Modify: `docs/ibkr_runbook.md` (a notice above "Quick facts")
- Modify: `scripts/scheduling/README.md` (the pull task)
- Modify: `docs/MIGRATION_CHECKPOINT.md` (new section 11)
- Modify: `docs/plans/2026-09-30-spark-research-split-design.md` (an amendment note under its header)
- Test: `tests/test_architecture/test_runbook_names.py`

**Interfaces:**
- Consumes: every script, unit and path the earlier tasks produced; the text names them, and the test fails on a name that does not exist.
- Produces: `docs/spark_runbook.md` section 7 with subsections 7.1 to 7.10; Tasks 12 and 13 edit its status line and the notices.

- [ ] **Step 1: Write the failing test**

Create `tests/test_architecture/test_runbook_names.py`:

```python
"""docs/spark_runbook.md section 7 names files and units; each of them must exist."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs" / "spark_runbook.md"
HEADING = "## 7. The trading runtime on the Spark"
TOP_LEVEL = ("scripts/", "deploy/", "monitoring/", "docs/", "tests/", "src/", "analysis/", "data/")


def section_7() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert HEADING in text, "section 7 is missing"
    return text.split(HEADING, 1)[1]


def test_every_repo_path_section_7_names_exists():
    tokens = set(re.findall(r"`([^`\s]+)`", section_7()))
    paths = {t for t in tokens if t.startswith(TOP_LEVEL) and "<" not in t and "*" not in t}
    assert len(paths) >= 15, sorted(paths)  # the section really is about these files
    if not any((REPO_ROOT / "docs").glob("spark_runtime_spike_*.md")):
        # the Gateway project is committed together with the spike report (Task 0)
        paths = {p for p in paths if not p.startswith("deploy/spark/ibgw/")}
    missing = sorted(p for p in paths if not (REPO_ROOT / p).exists())
    assert missing == []


def test_every_unit_section_7_names_is_in_the_repo():
    units = set(re.findall(r"`([a-z-]+\.(?:service|timer))`", section_7()))
    known = {p.name for p in (REPO_ROOT / "deploy" / "spark" / "systemd").iterdir()}
    assert {"ibkr-recorder.service", "ibkr-health.timer", "ibgw-owner-rule.service"} <= units
    assert units <= known, sorted(units - known)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_architecture/test_runbook_names.py -q`
Expected: two failures, `section 7 is missing`.

- [ ] **Step 3: Add the notice and section 7 to `docs/spark_runbook.md`**

Directly under the title line `# DGX Spark runbook -- research mirror` insert:

```markdown

> **Runtime move (2026-10): built, not yet in force.** Sections 1 to 6 describe
> the system as it runs: the Zenbook is the runtime, the Spark a research
> mirror. Section 7 describes the Spark as the runtime and takes over at the
> cutover (`docs/spark_runtime_cutover_<date>.md`, when it exists).
```

At the end of the file append:

```markdown

## 7. The trading runtime on the Spark

**Status: built, not yet in force.** Until a cutover report
(`docs/spark_runtime_cutover_<date>.md`) exists, the Zenbook runs the runtime,
`docs/ibkr_runbook.md` describes it, and sections 1 to 3 above are the rules
in force. From the cutover on, this section replaces them wherever they
disagree. Design: `docs/plans/2026-10-05-spark-runtime-move-design.md`.

Git Bash commands assume `source scripts/spark/env.sh`, as everywhere on this
page. A shell script is always started through `bash` (the checkout carries no
executable bits).

### 7.1 Who runs what

| User | Runs | Reaches the Gateway API |
| --- | --- | --- |
| `trading` | the recorder, the health probe, the paper sync, the label collector, the backup, and every hand-run script that talks to the venue or writes live state | yes, and nobody else |
| `scura` | the research clone, training and simulation, the LLM stack, the Gateway's compose project, the screen tunnel | no |

- Runtime clone: `/home/trading/forecast-model`, `main` only, delivered by
  `scripts/spark/deploy.sh`. Its tracked files are never edited in place: a
  changed tracked file makes the next push be refused.
- Live root: `/srv/quant_data` (`/home/trading/quant_data` is a symlink to
  it). Daily backups: `/srv/quant_data_backups`.
- Mirrors, each carrying the file `.mirror-of-spark`: `~/quant_data` of
  `scura` on the Spark, and `~/quant_data` on the Zenbook. A script that
  writes live state refuses a marked root (exit 2); a script that copies into
  a root refuses an unmarked one (exit 3).
- ssh shortcuts on the Zenbook: `spark` (the operator) and `spark-rt`
  (`trading`), both at the Spark's Tailscale name.
- The client ids are unchanged (`src/venues/ibkr/clients.py`); all four roles
  connect from the Spark, as `trading`.

### 7.2 The Gateway: container, login, kill switch

- The Gateway is the compose project `~/ibgw/` of `scura`, apart from
  `~/stack`. Its source is `deploy/spark/ibgw/docker-compose.yml`, with the
  image pinned by digest: never `docker compose pull`. It publishes the API on
  `127.0.0.1:4002` and the remote screen on `127.0.0.1:5900`, nothing else.
  `ibgw-owner-rule.service` rejects every connection to 4002 that does not
  come from a process of `trading`, and every connection to 5900 that does
  not come from `scura`.
- No IBKR password is stored on the Spark. The login is typed:
  1. On the Zenbook, with Tailscale up: `ssh -L 5900:127.0.0.1:5900 spark`,
     and leave it open.
  2. A VNC client on `localhost:5900`, with the screen password (not an IBKR
     credential; it lives in `~/ibgw/.env`).
  3. In the Gateway's dialog: the live user name with the Paper Trading
     toggle, the password, the phone prompt if one comes, then the
     paper-trading disclaimer before anything else.
  4. Close the VNC client, then the tunnel.

  The recorder reconnects by itself; the hourly probe sends the recovery
  message.
- When: every Sunday after 01:00 US Eastern (08:00 Istanbul time, 09:00 in
  the US winter), and whenever a DOWN message says the Gateway cannot be
  reached. The daily restart at 23:45 Europe/Istanbul needs no login.
- One session, one machine. Before the paper user logs in anywhere else, the
  Spark's container is stopped and `scripts/spark/session_check.py` exits 0
  there, as `scura`:

      python3 scripts/spark/session_check.py --port 4002 --container ibgw

- Kill switch: `"$SSH" spark 'cd ~/ibgw && docker compose stop'` closes the
  Gateway and with it every connection to the venue. `docker compose start`
  brings the login dialog back; nothing trades until someone logs in. The
  container's restart policy is `unless-stopped`: a crash or a reboot brings
  it back, at the login dialog, while a deliberate stop stays in force
  across a reboot.
- Do not open IB Gateway on the Zenbook any more: a login there takes the
  session away from the Spark.
- A new Gateway version is a new digest in
  `deploy/spark/ibgw/docker-compose.yml`, tried first the way the spike tried
  this one, then copied to `~/ibgw/` and started by the account holder.

### 7.3 Units, schedules, logs

| Unit | Runs | When (Europe/Istanbul) |
| --- | --- | --- |
| `ibkr-recorder.service` | `scripts/record_ibkr_ticks.py`, a sweep every 20 minutes | always; restarts itself after 30 s |
| `ibkr-health.timer` | `scripts/check_ibkr_recorder.py`, then a ping | every hour, on the hour |
| `ibkr-paper-sync.timer` | `scripts/sync_ibkr_paper.py` | 17:30 |
| `climate-labels.timer` | `scripts/collect_climate_labels.py`, then a ping | 16:30 |
| `paper-book-backup.timer` | `monitoring/backup_paper_book.py`, then a ping | 21:00 |

The unit files live in `deploy/spark/systemd/` and are installed under
`/etc/systemd/system` by the account holder. `scura` may, without a password,
start and stop the units above and the services behind the timers, and restart
`ibkr-recorder.service`; nothing else, and no other verb
(`deploy/spark/sudoers/forecast-runtime`). Enabling, disabling and installing
need `sudo` with a password. Each timer names its zone, so changing the
Spark's clock zone moves no job by itself, and a run missed while the Spark
was off fires at the next boot.

    "$SSH" spark 'systemctl list-timers --no-pager "ibkr-*" "climate-*" "paper-*"'
    "$SSH" spark 'systemctl is-active ibkr-recorder.service'
    "$SSH" spark-rt 'cat /srv/quant_data/ibkr_ticks/state/recorder_state.json'
    "$SSH" spark-rt 'grep -v "account=" forecast-model/logs/ibkr_recorder.log | tail -n 20'
    "$SSH" spark-rt 'tail -n 5 forecast-model/logs/ibkr_health.log'

The probe writes to `logs/ibkr_health.log` only when it finds a problem or
sends a message; a healthy hour leaves no line. Whether the last run was
healthy is the unit's exit status:

    "$SSH" spark 'systemctl show ibkr-health.service -p ExecMainStatus -p ExecMainExitTimestamp'

To run the probe now, exactly as the timer does:
`"$SSH" spark 'sudo -n /usr/bin/systemctl start ibkr-health.service'`. It is a
real run of the alert state machine. The alert-channel test of
`docs/ibkr_runbook.md` section 10.3 becomes:

    "$SSH" spark-rt 'cd forecast-model && set -a && . ~/.config/forecast-model/alerts.env && set +a && .venv/bin/python scripts/check_ibkr_recorder.py --test-alert'

The journal of the recorder and of the paper sync carries their session
line, which names the account. Read it filtered, and do not paste the
`systemctl status` output of these two units anywhere:

    "$SSH" spark 'journalctl -u ibkr-recorder.service -n 50 --no-pager | grep -v "account="'

The triage order of `docs/ibkr_runbook.md` section 7 holds with these
substitutions: "Gateway process up" is `docker ps --filter name=ibgw`;
"logged in" is the remote screen of 7.2; "port listening" is
`ss -ltn | grep 4002` on the Spark; "recorder task running" is
`systemctl is-active ibkr-recorder.service`.

### 7.4 Deploy

    bash scripts/spark/deploy.sh             # Git Bash on the Zenbook
    bash scripts/spark/deploy.sh --dry-run   # print the steps, run nothing

It pushes `main` to the runtime clone, compares the unit files with the
installed ones, waits for the recorder to finish a sweep and restarts it, so
a deploy does not cost a sweep. `--no-restart` delivers the code and leaves
the recorder on the code it started with; the timers' jobs pick new code up
at their next run either way.

- Exit 3 means a unit file changed: the script prints the `sudo install`
  lines; the account holder runs them and `sudo systemctl daemon-reload` on
  the Spark, then the deploy is run again. A new timer also needs
  `sudo systemctl enable --now <its name>`.
- The research clone is a separate delivery: `git push spark main`
  (section 2).
- When `pyproject.toml` or the constraints file changed, deliver the code
  without a restart first, then install as `trading` from the runtime clone,
  which now holds the new files, then deploy again, so that the recorder is
  restarted after a fresh sweep on the new code with the new packages (before
  the first push the clone still holds the old files, and an install made then
  would put the old set back):

      bash scripts/spark/deploy.sh --no-restart
      "$SSH" spark-rt 'cd forecast-model && uv pip install --python .venv/bin/python -e ".[dev,ibkr,climate,mcp,web,trading]" -c "$(ls constraints/zenbook-*.txt | tail -n 1)"'
      bash scripts/spark/deploy.sh

### 7.5 Scripts run by hand

As `trading`, in the runtime clone: `ssh spark-rt`, `cd forecast-model`, then
for example `.venv/bin/python scripts/run_gtm_paper.py`,
`.venv/bin/python scripts/run_ibkr_paper.py --mode approve`,
`.venv/bin/python scripts/fit_gtm_emos.py --trend --live`,
`.venv/bin/python scripts/backfill_ibkr_hourly.py --dry-run`,
`.venv/bin/python scripts/fetch_climate_forecasts.py`. The Gateway settings
come from the clone's `.env`. These scripts refuse a root that carries the
mirror marker, and from the Zenbook or as `scura` they cannot reach the
Gateway at all.

Two monthly rituals of `docs/ibkr_runbook.md` section 10 change place:

- The fee date (`IBKR_FEES_VERIFIED_ON`) that counts is the one in the
  runtime clone's `.env` on the Spark.
- The universe refresh needs the Gateway, so it runs on the Spark, but the
  snapshot is a tracked file and commits are made on the Zenbook. Write it
  into the home directory, not into the clone, and bring it over:

      "$SSH" spark-rt 'cd forecast-model && .venv/bin/python scripts/verify_ibkr_entitlements.py --port 4002 --snapshot ~/ibkr_universe_live.json'
      "$SCP" spark-rt:ibkr_universe_live.json data/ibkr_universe_live.json
      git diff --stat data/ibkr_universe_live.json

  Review, commit, then `bash scripts/spark/deploy.sh`.

### 7.6 Mirrors

- The research mirror is refreshed by `scripts/spark/mirror_live.sh`, run on
  the Spark as `scura` from `~/work/forecast-model`
  (`bash scripts/spark/mirror_live.sh`); then `scripts/spark/verify_mirror.py`
  with `--root ~/quant_data` before scoring on it.
- The Zenbook's mirror is filled by `scripts/spark/pull_snapshot.sh`, which
  the scheduled task `\Spark\Spark Pull` runs at logon and every six hours
  (log `logs\spark_pull_task.log`). By hand:
  `wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/pull_snapshot.sh"`,
  with `--include-raw` once if the 7.6 GB of CDS downloads should have a
  second copy too.
- `scripts/spark/push_snapshot.sh` refuses a marked source, so nothing is
  pushed from the Zenbook any more. Neither copy script deletes anything.
- The parity check of section 4 compares two mirrors now. Both must hold the
  same bytes: refresh both right after a sweep has finished (the heartbeat of
  7.3 shows when), then score.

### 7.7 Backups and the restore check

- Every day at 21:00 the operational files are copied to
  `/srv/quant_data_backups/YYYYMMDD/` with a `manifest.json`; the paper book
  goes through SQLite's backup API; thirty days are kept
  (`monitoring/backup_paper_book.py`).
- The pull brings those day directories to the Zenbook's
  `~/quant_data_backups`, where nothing rotates them: delete old ones by hand
  when wanted. These are the copies that count: the mirror's own paper book
  is a plain file copy, and a pull that lands inside the 17:30 sync can copy
  it torn until the next pull replaces it.
- Once a month, on the Zenbook, `scripts/spark/restore_check.py` restores one
  into a fresh scratch directory; delete the scratch afterwards:

      python scripts/spark/restore_check.py --from ~/quant_data_backups/<newest day> --scratch ~/restore_scratch

  `RESTORE OK: N files restored and verified` is the pass; a `FAIL` line names
  the file. `--from ~/quant_data` checks the mirror itself the same way.
- If the Spark's disk is lost: rebuild the machine's side (plan Task 0 steps 1
  to 4 and Task 11), keep every Gateway logged out and the units stopped, run
  the restore check on the Zenbook's mirror, and copy the mirror back from
  WSL. This is the one time data goes from a mirror into the live root, by
  hand and on purpose:

      rsync -rlt --exclude "/.mirror-of-spark" --exclude "ibkr_ticks/state/" -e /mnt/c/Windows/System32/OpenSSH/ssh.exe /mnt/c/Users/asus/quant_data/ spark-rt:/srv/quant_data/

  If the mirror's paper book failed the check, take the operational files
  from the newest day directory that passes instead (the same command with
  that directory as the source and `--exclude manifest.json`). What is lost
  is the sync record since the last pull; the orders are at the venue and the
  next sync finds them.

### 7.8 The external checks

| Check | Pinged by | Expected |
| --- | --- | --- |
| `spark-health` | `ibkr-health.service`, whenever the probe ran (exit 0 or 1) | every hour |
| `climate-labels` | `climate-labels.service`, on success | every day |
| `paper-backup` | `paper-book-backup.service`, on success | every day |
| `offbox-pull` | the Zenbook's pull task, on success | within three days |

A late check means the thing that pings it is not running: for
`spark-health`, the Spark itself is off or offline. The ping addresses are
capabilities. On the Spark they live in
`/home/trading/.config/forecast-model/heartbeat.env` (mode 600), on the
Zenbook in the user-level variable `HEARTBEAT_URL_OFFBOX`.
`scripts/spark/heartbeat_ping.py` never prints them; never paste one into a
log, a document or a chat. A unit reads its file at each start, so a replaced
address needs no restart.

### 7.9 Rollback (the first week after the cutover)

    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh" --dry-run
    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh"

`scripts/spark/rollback.sh` stops the Spark's units and the Gateway
container, checks that the Spark no longer holds a session, and only then
copies the Spark's live root into the Zenbook's kept root
`~/quant_data_zenbook_final` (nothing is deleted there). It ends by printing
what the account holder does, in this order: disable the units on the Spark,
disable the pull task `\Spark\Spark Pull`, check the copied book with
`scripts/spark/restore_check.py --from ~/quant_data_zenbook_final --scratch
<a fresh directory>` (it must print `RESTORE OK`), put the kept root back with
`scripts/scheduling/swap_live_root_for_mirror.ps1` and its `-Undo` switch,
re-enable the four scheduled tasks, log in on the Zenbook. Only exit 0 allows
that login. Exit 4 means the Spark may still hold the session (it could not be
reached, a stop was refused, or the check found a Gateway): do not log in on
the Zenbook. Exit 5 is a copy that failed after the session was released: run
the script again. `--no-copy` runs the Spark side only, for a rehearsal.

### 7.10 Research jobs

Training and simulation start through the launcher
`scripts/spark/research_run.sh`, as `scura`, from the research clone:

    bash scripts/spark/research_run.sh -- .venv/bin/python scripts/fit_gtm_emos.py --trend --root ~/quant_data/climate_data
    bash scripts/spark/research_run.sh --memory 60G -- <command>     # only after: cd ~/stack && docker compose stop qwenflash

It gives the job a memory ceiling (15 GiB unless told otherwise) and makes it
the first thing the kernel kills when memory runs out: research jobs score
+500, the LLM -500, the Gateway and the runtime -900.
`bash scripts/spark/research_run.sh -- cat /proc/self/oom_score_adj` prints
`500`. Use tmux for a long job: it runs in the foreground of its shell.
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `python -m pytest tests/test_architecture/test_runbook_names.py -q`
Expected: `2 passed`. A failure names the path or unit the text got wrong: fix the text, not the test.

- [ ] **Step 5: Add the notices to the other documents**

`docs/ibkr_runbook.md` -- insert above the line `Quick facts:`:

```markdown
> **Runtime move (2026-10): built, not yet in force.** The runtime is moving
> to the DGX Spark (`docs/spark_runbook.md` section 7). Until a cutover report
> `docs/spark_runtime_cutover_<date>.md` exists, this page describes the
> system as it runs, on the Zenbook.

```

`scripts/scheduling/README.md` -- insert above the heading `## Leftover Polymarket Task Scheduler jobs`:

````markdown
## Spark pull (the copy of the live root that lives off the Spark)

From the cutover of the runtime to the Spark on, this machine's `~/quant_data`
is a mirror of the Spark's live root (`docs/spark_runbook.md` section 7.6).

| Job | Command | Cadence |
|---|---|---|
| Pull the live root and the daily backups | `run_spark_pull.ps1` (WSL, `scripts/spark/pull_snapshot.sh`) | Task Scheduler `\Spark\Spark Pull`, at logon and every six hours |

Register it from an elevated PowerShell, after setting the user-level
variable `HEARTBEAT_URL_OFFBOX` (the script's header says how):

```powershell
.\scripts\scheduling\register_spark_pull_task.ps1
```

Log: `logs\spark_pull_task.log`. The pull refuses a `~/quant_data` that does
not carry the file `.mirror-of-spark`. Remove the task with
`Unregister-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull' -Confirm:$false`.

````

`docs/MIGRATION_CHECKPOINT.md` -- append at the end of the file:

```markdown

## 11. Runtime move to the Spark (2026-10)

**Status: built, not cut over.** The Zenbook is still the runtime; everything
in sections 0 to 8 stands. What exists: the design
(`docs/plans/2026-10-05-spark-runtime-move-design.md`), the plan
(`docs/plans/2026-10-05-spark-runtime-move.md`), the spike report
(`docs/spark_runtime_spike_<date>.md`), and the build: a Linux user `trading`
on the Spark with a runtime clone, systemd units under `deploy/spark/`, an
owner rule that admits only `trading` to the Gateway port, a marker file
that tells a mirror from the live root, a daily backup with a restore check,
external heartbeat checks, and the deploy, pull, mirror and rollback scripts
under `scripts/spark/`. The operator's view is `docs/spark_runbook.md`
section 7. This section is rewritten at the cutover and again at acceptance.
```

`docs/plans/2026-09-30-spark-research-split-design.md` -- insert after the line that begins `` `docs/spark_parity_<date>.md`) take the date `` (the end of the header block, before `## 0. One-paragraph goal`):

```markdown

> **Amended 2026-10-05.** `docs/plans/2026-10-05-spark-runtime-move-design.md`
> moves the Gateway and the runtime to the Spark. From its cutover on,
> invariants 1, 2, 4 and 5 of section 2 below are replaced as section 12 of
> that document says; invariant 3, the parity tooling, the LLM client and the
> LLM boundary stand. Until the cutover this document is in force unchanged.
```

- [ ] **Step 6: Run everything, then commit**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: green, `All checks passed!`.

```bash
git add docs/spark_runbook.md docs/ibkr_runbook.md scripts/scheduling/README.md docs/MIGRATION_CHECKPOINT.md docs/plans/2026-09-30-spark-research-split-design.md tests/test_architecture/test_runbook_names.py
git commit -m "Document the runtime on the Spark: runbook section 7 and the notices that point at it"
```

---

### Task 11: Installation on the Spark, the heartbeat checks, the rehearsals

Everything is put in place and tried while the Zenbook is still the runtime. At the end of this task the Spark holds an installed, stopped runtime and a seeded live root; nothing there has talked to the venue since the spike.

Needs: Task 0 with the verdict GO, and Tasks 1 to 10 complete on the branch. The hourly backfill and the universe refresh that spec section 11 puts before this move are finished: no backfill or discovery process is running on the Zenbook.

**Files:**
- Create on the Spark, outside git: the ten unit files under `/etc/systemd/system/`, `/etc/sudoers.d/forecast-runtime`, the credential files of the Global Constraints, the pinned `/home/scura/ibgw/docker-compose.yml`
- Create on the Zenbook, outside git: the scheduled task `\Spark\Spark Pull` (left disabled), the user-level variable `HEARTBEAT_URL_OFFBOX`
- No file of the repo changes in this task.

**Interfaces:**
- Consumes: everything Tasks 0 to 10 produced.
- Produces: the installed state Task 12 starts from; the confirmation that `systemctl show --timestamp=unix` prints `@<seconds>` on the Spark's systemd, which `cutover_check.py` relies on.

- [ ] **Step 1: Land the build on `main` and deliver it to both clones**

Outside 17:25-17:40 local and not within five minutes of the top of the hour: the main checkout is still the live runtime and its scheduled jobs start from these files. The merge is a fast-forward. **(with the account holder's go-ahead)** If `main` has moved since the branch was cut (`git merge-base --is-ancestor main spark-runtime-move; echo $?` prints `1`), first bring the branch up to date in the worktree with `git merge main` and run the suite again.

```bash
python -m pytest -q 2>&1 | tail -n 1
python -m ruff check . | tail -n 1
git status --short | wc -l
git -C /c/Users/asus/OneDrive/Desktop/new_work merge --ff-only spark-runtime-move
source scripts/spark/env.sh
git push spark main
git push spark-rt main
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python -m pytest -q 2>&1 | tail -n 1 && .venv/bin/python -m ruff check . | tail -n 1'
"$SSH" spark 'cd ~/work/forecast-model && .venv/bin/python -m pytest -q 2>&1 | tail -n 1'
```

Expected: no failure and `All checks passed!` on the Zenbook; `0` uncommitted files; the fast-forward; no failure and `All checks passed!` from both clones on the Spark. The counts are not fixed numbers, because more work may have landed: the Spark's passed count is the Zenbook's, minus the tests that need Windows PowerShell (its Windows-only tests, in `tests/test_deploy/test_powershell_scripts.py`, skipped there; the three text tests of that file run on the Spark too), plus the one POSIX-only test (the umask test of `tests/test_monitoring/test_backup_paper_book.py`, skipped on the Zenbook). Whether `main` also goes to GitHub (`git push origin main`) is the account holder's call: ask, do not assume.

- [ ] **Step 2 (account holder): install the units, the sudoers drop-in and the owner rule**

On the Spark, as `scura`:

```bash
sudo sh -c 'install -m 644 -o root -g root /home/trading/forecast-model/deploy/spark/systemd/* /etc/systemd/system/'
sudo visudo -cf /home/trading/forecast-model/deploy/spark/sudoers/forecast-runtime && sudo install -m 440 -o root -g root /home/trading/forecast-model/deploy/spark/sudoers/forecast-runtime /etc/sudoers.d/forecast-runtime
sudo -l | grep -c "systemctl restart ibkr-recorder.service"
sudo systemctl daemon-reload
sudo systemctl enable --now ibgw-owner-rule.service
sudo iptables -S OUTPUT | grep -E "dport (4002|5900)"
```

Before the second line, open a second terminal on the Spark with a root shell (`sudo -s`) and keep it until the `sudo -l` line has answered: a broken file under `/etc/sudoers.d` stops `sudo` for everybody, and this machine is reached over ssh only. The drop-in is installed only if `visudo` accepts it (`parsed OK`); the check and the install are one line on purpose. Expected: `1` from the `sudo -l` line, and at the end exactly two rules, one per port. Nothing else is enabled or started here: the runtime's units stay off until the cutover.

- [ ] **Step 3: Verify the installation**

```bash
"$SSH" spark 'for u in ibkr-recorder.service ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer; do echo "$u $(systemctl is-enabled "$u" 2>&1) $(systemctl is-active "$u")"; done; systemctl is-active ibgw-owner-rule.service'
"$SSH" spark 'sudo -n /usr/bin/systemctl stop ibkr-health.timer; echo "allowed: rc=$?"; sudo -n /usr/bin/true; echo "everything else: rc=$?"'
"$SSH" spark 'systemd-analyze calendar "*-*-* 17:30:00 Europe/Istanbul" | grep -E "Next elapse|From now"'
"$SSH" spark 'systemctl show ibgw-owner-rule.service --timestamp=unix -p ExecMainStatus -p ExecMainExitTimestamp'
bash scripts/spark/deploy.sh --no-restart
```

Expected, in order: five lines `<unit> disabled inactive`, then `active`. `allowed: rc=0`, then sudo's `a password is required` and `everything else: rc=1`. A `Next elapse:` line at `17:30:00 +03`. `ExecMainStatus=0` and `ExecMainExitTimestamp=@` followed by digits (this systemd, 255, printed `@1791167830` for another one-shot unit on 2026-10-06): if a date is printed instead of `@<seconds>`, stop here and change `_health_unit_last_run` in `scripts/spark/cutover_check.py` (with its test) to read what this systemd prints, before the cutover evening. The deploy prints `Everything up-to-date`, no unit that differs, `== 3/4 skipped`, the commit and five `...: inactive` lines.

- [ ] **Step 4: The credential files of `trading`**

The runtime clone's `.env` holds settings, not secrets; it is written from the Zenbook's `.env` without showing a value (Git Bash, main checkout):

```bash
{ grep -E '^IBKR_(COMMISSION_PER_CONTRACT|FEES_VERIFIED_ON|RESTART_WINDOW_LOCAL)=' /c/Users/asus/OneDrive/Desktop/new_work/.env | tr -d '\r'; printf 'IBKR_PORT=4002\nIBKR_TRADING_MODE=paper\n'; } | "$SSH" spark-rt 'umask 077 && cat > forecast-model/.env && cut -d= -f1 forecast-model/.env | sort | paste -sd" "'
```

Expected: `IBKR_COMMISSION_PER_CONTRACT IBKR_FEES_VERIFIED_ON IBKR_PORT IBKR_RESTART_WINDOW_LOCAL IBKR_TRADING_MODE`. Nothing else from the Zenbook's `.env` goes to the Spark.

**(account holder)** The three files that hold secrets. In Git Bash on the Zenbook, after `source scripts/spark/env.sh`; each command prints key names only:

```bash
printf 'ALERT_METHOD=%s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$ALERT_METHOD" "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" | "$SSH" spark-rt 'umask 077 && mkdir -p ~/.config/forecast-model && cat > ~/.config/forecast-model/alerts.env && cut -d= -f1 ~/.config/forecast-model/alerts.env | paste -sd" "'
"$SCP" ~/.cdsapirc spark-rt:.cdsapirc && "$SSH" spark-rt 'chmod 600 .cdsapirc'
```

and, after step 5 has produced the addresses, on the Spark as `trading` (`ssh spark-rt`):

```bash
umask 077 && nano ~/.config/forecast-model/heartbeat.env
```

with three lines, `HEARTBEAT_URL_HEALTH=`, `HEARTBEAT_URL_LABELS=` and `HEARTBEAT_URL_BACKUP=`, each followed by its check's ping address.

Verify, without a value on the screen:

```bash
"$SSH" spark-rt 'stat -c "%U %a %n" forecast-model/.env .config/forecast-model/alerts.env .config/forecast-model/heartbeat.env .cdsapirc; cut -d= -f1 .config/forecast-model/heartbeat.env | sort | paste -sd" "; grep -c "^HEARTBEAT_URL_[A-Z]*=https://" .config/forecast-model/heartbeat.env; grep -c "=." .config/forecast-model/alerts.env; grep -c "^ALERT_METHOD=telegram$" .config/forecast-model/alerts.env'
"$SSH" spark 'stat -c "%U %a %n" ~/ibgw/.env; grep -c "^AUTO_RESTART_TIME=11:45 PM$" ~/ibgw/.env; grep -c "^TWS_PASSWORD=$" ~/ibgw/.env'
```

Expected: four lines `trading 600 <file>`; `HEARTBEAT_URL_BACKUP HEARTBEAT_URL_HEALTH HEARTBEAT_URL_LABELS`; `3`; then `3` and `1` (all three alert settings have a value, and the method is telegram: a variable that was not set in the shell that wrote the file would have left an empty value). Then `scura 600 /home/scura/ibgw/.env`, `1`, `1` (the restart time is back at 23:45 and the IBKR password field is empty).

The alert path and the three ping addresses are tried now, not on the cutover evening, where a failure would mean a rollback. **(with the account holder's go-ahead, who says whether the message arrived on the phone)**

```bash
"$SSH" spark-rt 'cd forecast-model && set -a && . ~/.config/forecast-model/alerts.env && set +a && .venv/bin/python scripts/check_ibkr_recorder.py --test-alert'
"$SSH" spark-rt 'cd forecast-model && set -a && . ~/.config/forecast-model/heartbeat.env && set +a && for v in HEARTBEAT_URL_HEALTH HEARTBEAT_URL_LABELS HEARTBEAT_URL_BACKUP; do .venv/bin/python scripts/spark/heartbeat_ping.py --url-env $v -- true; done'
```

Expected: `test alert delivered via telegram` and the message on the phone; `heartbeat: sent` three times, and the three checks green in the heartbeat service. **(account holder)** then pauses the three checks there: nothing pings them until the cutover, and a paused check starts again by itself at its next ping.

- [ ] **Step 5 (account holder): the heartbeat account and its four checks**

At healthchecks.io or an equivalent, create four checks and a notification channel (e-mail or Telegram):

| Check | Period | Grace |
| --- | --- | --- |
| `spark-health` | 1 hour | 1 hour |
| `climate-labels` | 1 day | 3 hours |
| `paper-backup` | 1 day | 3 hours |
| `offbox-pull` | 1 day | 2 days |

So `offbox-pull` reports after three days without a pull. A check that has never been pinged does not alert; each starts to count at its first ping. The three Spark addresses go into `heartbeat.env` (step 4); the fourth is set on the Zenbook in step 8.

- [ ] **Step 6: The pinned Gateway project onto the Spark**

```bash
"$SSH" spark 'diff ~/ibgw/docker-compose.yml ~/work/forecast-model/deploy/spark/ibgw/docker-compose.yml; cp ~/work/forecast-model/deploy/spark/ibgw/docker-compose.yml ~/ibgw/docker-compose.yml && cd ~/ibgw && docker compose config --quiet && echo "compose file is valid" && grep -c "@sha256:" docker-compose.yml && docker ps --filter name=ibgw --format "{{.Status}}" | wc -l'
```

Expected: the diff shows the image line (a tag before, the digest now) and comment lines, nothing else; `compose file is valid`; `1`; `0` (the container is not running, and this step does not start it).

- [ ] **Step 7: Seed the live root**

The cutover evening must not wait for 7.6 GB. The same push that will run then runs once now, into the Spark's live root, which nothing reads yet. It is bracketed by the manifest, as every push from the live root is. Outside 17:25-17:40. **(with the account holder's go-ahead)**

```bash
mkdir -p ~/quant_data_snapshot/parity/runtime_rehearsal_$RUN_DATE
python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/runtime_rehearsal_$RUN_DATE/manifest_before_seed.json
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh" --cutover --include-raw 2>&1 | tail -n 12
python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/runtime_rehearsal_$RUN_DATE/manifest_after_seed.json
diff ~/quant_data_snapshot/parity/runtime_rehearsal_$RUN_DATE/manifest_before_seed.json ~/quant_data_snapshot/parity/runtime_rehearsal_$RUN_DATE/manifest_after_seed.json && echo "the live root was not touched"
"$SSH" spark-rt 'stat -c "%U:%G %a %n" /srv/quant_data /srv/quant_data/ibkr_ticks; du -sh /srv/quant_data | cut -f1'
"$SSH" spark 'ls /srv/quant_data | head -n 3; test ! -e /srv/quant_data/.mirror-of-spark && echo "no marker in the live root"'
```

Expected: the push ends with the identity block of `/srv/quant_data`; `the live root was not touched`; `trading:trading 2750 /srv/quant_data` and `trading:trading 2755 /srv/quant_data/ibkr_ticks`; about `7.6G`; `scura` lists the root; `no marker in the live root`.

- [ ] **Step 8 (account holder): the Zenbook's pull task, registered and left disabled**

In an elevated PowerShell:

```powershell
[Environment]::SetEnvironmentVariable('HEARTBEAT_URL_OFFBOX', (Read-Host 'ping address of offbox-pull'), 'User')
cd C:\Users\asus\OneDrive\Desktop\new_work
.\scripts\scheduling\register_spark_pull_task.ps1
Start-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'
Start-Sleep 30; Get-Content logs\spark_pull_task.log -Tail 3
Disable-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'
```

The address is pasted at the prompt of the first line, so it is in no history file. Expected in the log: the line `pull_snapshot: /mnt/c/Users/asus/quant_data does not carry .mirror-of-spark; refusing ...` and `heartbeat: not sent (command exit 3)`. The task works and cannot touch the live root; it stays disabled until the cutover creates the mirror.

The address itself is tried in a NEW PowerShell window (a variable set for the user is seen only by processes started afterwards), from the main checkout:

```powershell
python scripts\spark\heartbeat_ping.py --url-env HEARTBEAT_URL_OFFBOX -- python -c "pass"
```

Expected: `heartbeat: sent`. **(account holder)** pauses the check `offbox-pull` until the cutover.

- [ ] **Step 9: The rehearsals**

Each line has an expected result; one that differs is fixed before the cutover is scheduled. Run them from the main checkout, after step 1 has landed the build there: `REPO_WSL` points at it, and its `.py` files are CRLF (the checkout converts them), which is the form of `session_check.py` that `rollback.sh` pipes to the Spark's python, so the rehearsal also shows that the Spark's python runs that copy. Three of them are real runs, not dry runs: the Spark side of the rollback (everything there is stopped, so stopping it again changes nothing, but sudo, docker and the piped check are really used), a pull of one small directory into a scratch mirror (ssh without a prompt from WSL, the read through the group, rsync at both ends), and a research job left running when its login ends (the last lines of the block).

```bash
source scripts/spark/env.sh
bash scripts/spark/deploy.sh --dry-run | grep -c "DRY-RUN"
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh" --no-copy; echo "rollback rehearsal: exit=$?"
MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- env ROLLBACK_DEST=/tmp bash "$REPO_WSL/scripts/spark/rollback.sh" --dry-run | grep -E "^== |DRY-RUN: rsync" | cut -c1-90
MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- mkdir -p /tmp/mirror_probe
MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- touch /tmp/mirror_probe/.mirror-of-spark
MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- env QUANT_DATA_MIRROR=/tmp/mirror_probe QUANT_DATA_BACKUPS_MIRROR=/tmp/backups_probe SPARK_SRC=spark:/srv/quant_data/ibkr_ticks/paper bash "$REPO_WSL/scripts/spark/pull_snapshot.sh" | tail -n 2
MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- ls /tmp/mirror_probe
MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- rm -rf /tmp/mirror_probe /tmp/backups_probe
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh" --cutover --include-raw --dry-run | cut -c1-60
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python scripts/spark/cutover_check.py --root /srv/quant_data --repo /home/trading/forecast-model --since "$(date -u +%FT%T+00:00)"; echo "exit=$?"'
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python -m monitoring.backup_paper_book --root /srv/quant_data --dest ~/backup_rehearsal && .venv/bin/python scripts/spark/restore_check.py --from ~/backup_rehearsal/$(ls ~/backup_rehearsal | tail -n 1) --scratch ~/restore_rehearsal; rm -rf ~/backup_rehearsal ~/restore_rehearsal'
"$SSH" spark 'cd ~/work/forecast-model && bash scripts/spark/research_run.sh -- cat /proc/self/oom_score_adj'
"$SSH" spark "cd ~/work/forecast-model && bash scripts/spark/research_run.sh -- sh -c 'cat /sys/fs/cgroup\$(cut -d: -f3 /proc/self/cgroup)/memory.max'"
python scripts/spark/session_check.py --port 4002 --process ibgateway; echo "exit=$?"
"$SSH" spark 'loginctl show-user scura -p Linger'
"$SSH" spark 'cd ~/work/forecast-model && (nohup bash scripts/spark/research_run.sh -- sleep 300 < /dev/null > /dev/null 2>&1 &)'
sleep 40; "$SSH" spark-rt 'pgrep -af "[s]leep 300" || echo "the job is gone"'
"$SSH" spark 'pkill -f "[s]leep 300"; true'
```

Expected, in order:
1. `7` (the push, the compare, the recorder's state, the wait, the recorder's state again, the restart, the status).
2. `all runtime units are stopped`, the session check's `no Gateway here` line, the `skipped (--no-copy)` line and `rollback rehearsal: exit=0`. An exit 4 here is a finding: sudo, docker or the check does not work the way a real rollback needs.
3. The five `== n/5` headings in order with the rsync line under the fourth. (`ROLLBACK_DEST=/tmp` because the kept root does not exist before the cutover; a dry run copies nothing.)
4. `pull_snapshot: done ...`, then the names of the paper directory's files (`ibkr_paper.db`, `ibkr_sync_runs.jsonl` and the others; names only). The scratch directories are removed by the next line.
5. Three lines starting `DRY-RUN:`.
6. Four `FAIL` lines whose reasons are the honest ones for a runtime that has not started -- `no sweep file written since the cutover`; `ibkr-health.service has not finished a run since the cutover; no delivered test alert since the cutover`; `no sync run since the cutover`; `not checked: pass --parity ...` -- then `CUTOVER GATE: FAIL -- roll back` and `exit=1`. A `could not ask systemd` in the health line is a defect to fix now.
7. `backup written: ... (N/9 files present)` then `RESTORE OK: N files restored and verified`, with the same N.
8. `500`.
9. `16106127360` (15 GiB).
10. `session check: a Gateway is here: ...` and `exit=1` while the Zenbook's Gateway is open.
11. `Linger=yes`.
12. A line `<pid> sleep 300`, not `the job is gone`.

11 and 12 are one finding. A job started by `research_run.sh` lives in the user manager of `scura` (`systemd-run --user --scope`), and that manager stops with the last login of `scura` unless linger is on, taking the job with it. Close every other login of `scura` first (the VNC tunnel, a tmux session): any of them keeps the manager alive and hides the problem. `Linger=no`, or `the job is gone` 40 s after the starting login ended: **(account holder)** decides on `sudo loginctl enable-linger scura`, and lines 11 and 12 are run again. (The brackets in `[s]leep` keep `pgrep` and `pkill` from matching their own command line.)

Nothing is committed in this task. Record the results in the ledger.

---

### Task 12: The cutover

One weekday evening, started between 18:00 and 21:30 Europe/Istanbul: after the Zenbook's 17:30 sync has run, well before the 23:45 restart. Recording stops for about 30 to 45 minutes; the hourly backfill of Task 13 fills that. The Gateway session moves exactly once, and it is never on both machines: the Spark's container is not even started until a check has shown that the Zenbook has let go, and the check is repeated just before the login on the Spark.

**Files:**
- Create: `docs/spark_runtime_cutover_<date>.md`
- Modify: `docs/spark_runbook.md`, `docs/ibkr_runbook.md`, `docs/MIGRATION_CHECKPOINT.md` (they are made to say what is now true)
- Create outside git: the evidence directory `~/quant_data_snapshot/parity/cutover_<date>/` and the pointer `~/quant_data_snapshot/parity/cutover_current.txt`; the marker files of the two mirrors

**Interfaces:**
- Consumes: the installed state of Task 11; `push_snapshot.sh --cutover` and `rollback.sh` (Task 7); `swap_live_root_for_mirror.ps1` (Task 8); `session_check.py` (Task 2); `heartbeat_ping.py` (Task 3); `cutover_check.py` (Task 9); `live_manifest.py`, `parity_score.py`, `compare_json.py` (Option B).
- Produces: the Spark as the runtime; `SINCE`, the moment its recorder started, kept in `<evidence directory>/since.txt`.

**Where the evening is, and the way out.** This block only reads. Run it whenever the state is in doubt, for instance after a session that died:

```bash
source scripts/spark/env.sh
python scripts/spark/session_check.py --port 4002 --process ibgateway; echo "zenbook session: exit=$?"
ls -d ~/quant_data*
"$SSH" spark 'docker ps --filter name=ibgw --format "ibgw: {{.Status}}"; for u in ibkr-recorder.service ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer; do echo "$u $(systemctl is-enabled "$u" 2>&1) $(systemctl is-active "$u")"; done'
```

```powershell
Get-ScheduledTask -TaskPath '\IBKR\','\Climate\','\Spark\' | Format-Table TaskName, State
```

| What it shows | Where the evening is | The way out |
| --- | --- | --- |
| the four tasks `Ready` or `Running`, `zenbook session: exit=1` | before step 3 | nothing to undo |
| the four tasks `Disabled`, no `quant_data_zenbook_final` | steps 3 to 6 | call it off: in one Git Bash call run `source scripts/spark/env.sh` and then `wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh" --no-copy` (it stops the Spark's container if it is up, runs the piped session check and requires its exit 0), and only after exit 0 re-enable the four tasks and log in on the Zenbook |
| `quant_data_zenbook_final` exists, the pull task `Disabled` | steps 7 to 10 | step 11, the rollback, for ANY failure: a login that does not work, a unit that does not start, a gate line that fails |
| the units `enabled active`, the pull task `Ready` | step 12 or later | the cutover stands; from here a rollback is runbook section 7.9 |

- [ ] **Step 1: Preconditions (nothing changes yet)**

**(account holder)** The laptop is on mains power with its lid open until the last step; a lid that closes on battery suspends everything half-way. The hourly backfill and the universe refresh of spec section 11 are finished.

```bash
source scripts/spark/env.sh
EV=~/quant_data_snapshot/parity/cutover_$RUN_DATE && mkdir -p "$EV" && echo "$EV" > ~/quant_data_snapshot/parity/cutover_current.txt
git -C /c/Users/asus/OneDrive/Desktop/new_work rev-parse main
"$SSH" spark-rt 'git -C forecast-model rev-parse HEAD; git -C forecast-model status --short --untracked-files=no | wc -l'
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python -m pytest -q 2>&1 | tail -n 1'
"$SSH" spark 'for u in ibkr-recorder.service ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer; do echo "$u $(systemctl is-active "$u")"; done; systemctl is-active ibgw-owner-rule.service; docker ps --filter name=ibgw --format "{{.Status}}" | wc -l'
"$SSH" spark-rt 'stat -c "%U %a %n" forecast-model/.env .config/forecast-model/alerts.env .config/forecast-model/heartbeat.env'
python scripts/spark/session_check.py --port 4002 --process ibgateway; echo "zenbook: exit=$?"
```

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object { if ($_.CommandLine -match 'scripts[\\/]\S+\.py') { $Matches[0] } }
```

Expected: the same commit twice and `0` changed files; the suite line; five `inactive` lines, `active`, and `0` running containers; three `trading 600` lines; `zenbook: exit=1` (the Zenbook holds the session, as it should now). The PowerShell line names every script a Python process on the Zenbook is running: it must print `scripts\record_ibkr_ticks.py` and nothing else. A backfill, a discovery run or a script somebody started by hand would go on writing into the root after the freeze, and what it wrote would never reach the Spark: let it finish first.

Nothing on the Zenbook may start IB Gateway by itself at logon: the first logon after the cutover, or a reboot during the rollback week, would take the session back from the Spark without a word. In PowerShell:

```powershell
Get-ScheduledTask | Where-Object { ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -match 'gateway|ibc' } | Format-Table TaskPath, TaskName, State
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' | Format-List | Out-String -Stream | Select-String -Pattern 'gateway|ibc'
Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup", "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp" | Where-Object Name -match 'gateway|ibc'
```

Expected: the three lines print nothing: no scheduled task, `Run` key or Startup item mentions the Gateway or IBC. Whatever prints is disabled by the **(account holder)** before step 3.

The evidence directory's path is now in `cutover_current.txt`; every later block reads it from there, because nothing else survives from one shell to the next.

- [ ] **Step 2: Arm the outside alarm, and make the Zenbook's book fresh**

From step 3 until the Spark's timers run, neither machine's own alerts work. One ping now starts the external check's clock: if the Spark's probe is not running two hours from here, the heartbeat service says so.

```bash
source scripts/spark/env.sh
"$SSH" spark-rt 'cd forecast-model && set -a && . ~/.config/forecast-model/heartbeat.env && set +a && .venv/bin/python scripts/spark/heartbeat_ping.py --url-env HEARTBEAT_URL_HEALTH -- true'
```

Expected: `heartbeat: sent` (a paused check starts counting again with this ping).

**(account holder)** `Start-ScheduledTask -TaskPath '\IBKR\' -TaskName 'IBKR Paper Sync'`, so that the book the Spark's first sync is compared with is minutes old, not hours. Then:

```bash
python - <<'EOF'
import json
from datetime import datetime, timezone
from pathlib import Path
lines = (Path.home() / "quant_data/ibkr_ticks/paper/ibkr_sync_runs.jsonl").read_text(encoding="utf-8").splitlines()
last_ok = [json.loads(line) for line in lines if line.strip() and json.loads(line).get("ok")][-1]
age = datetime.now(timezone.utc) - datetime.fromisoformat(last_ok["ts_utc"])
print("last ok sync:", last_ok["ts_utc"], f"({age.total_seconds() / 60:.0f} min ago)")
EOF
```

Expected: a last ok sync a few minutes old. (The ledger's lines carry the account id; this prints a time and nothing else.)

- [ ] **Step 3: Freeze the Zenbook, and prove it has given the session up**

Watch for the end of a sweep, so the stop does not cut one:

```bash
python -c "import pathlib, time; p = pathlib.Path.home() / 'quant_data/ibkr_ticks/state/recorder_state.json'; print(f'heartbeat is {time.time() - p.stat().st_mtime:.0f} s old')"
```

When it prints less than about 120 s, **(account holder)** in an elevated PowerShell:

```powershell
Get-ScheduledTask -TaskPath '\IBKR\','\Climate\' | Where-Object TaskName -in 'IBKR Recorder','IBKR Recorder Health','IBKR Paper Sync','Climate Labels' | Disable-ScheduledTask
Stop-ScheduledTask -TaskPath '\IBKR\' -TaskName 'IBKR Recorder'
```

then log out of the Zenbook's Gateway and close it. Now the check (Git Bash, then PowerShell):

```bash
python scripts/spark/session_check.py --port 4002 --process ibgateway; echo "zenbook: exit=$?"
```

```powershell
@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -match 'scripts[\\/]\S+\.py').Count
Get-ScheduledTask -TaskPath '\IBKR\','\Climate\' | Format-Table TaskName, State
```

Expected: `session check: no Gateway here (port 4002 closed, no ibgateway process)` and `zenbook: exit=0`; `0` Python processes running a script; the four tasks `Disabled`. **Nothing below starts until all three hold.** From here until step 8 the session is on neither machine.

- [ ] **Step 4 (account holder): the Gateway container up on the Spark, logged out**

Only now, with the Zenbook checked: on the Spark, `cd ~/ibgw && docker compose up -d && docker compose ps`. On the Zenbook, the tunnel (`ssh -L 5900:127.0.0.1:5900 spark`) and the VNC viewer. The container needs about three minutes; step 5 runs meanwhile. When the screen is up it must show the LOGIN DIALOG, as the spike saw after a stop (Task 0 step 15). Do not log in yet. If it shows the Gateway's main window instead, it has taken a session by itself: `docker compose stop` at once, and call the evening off as the table says for steps 3 to 6: `rollback.sh --no-copy`, and only its exit 0 allows re-enabling the four tasks and logging in on the Zenbook.

```bash
source scripts/spark/env.sh
"$SSH" spark 'ss -ltnH | grep -E ":(4002|5900) "'
```

Expected: two listening sockets, both on `127.0.0.1`.

- [ ] **Step 5: The final push, and the proof that both roots hold the same operational files**

```bash
source scripts/spark/env.sh
EV=$(cat ~/quant_data_snapshot/parity/cutover_current.txt)
python scripts/spark/live_manifest.py --root ~/quant_data --out "$EV/manifest_zenbook.json"
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh" --cutover --include-raw 2>&1 | tail -n 12
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python scripts/spark/live_manifest.py --root /srv/quant_data --out ~/manifest_spark.json'
"$SCP" spark-rt:manifest_spark.json "$EV/manifest_spark.json"
python - "$EV/manifest_zenbook.json" "$EV/manifest_spark.json" <<'EOF'
import json
import sys

zenbook, spark = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:3])


def core(record):
    return None if record is None else (record["sha256"], record["size"])


differing = sorted(k for k in set(zenbook) | set(spark) if core(zenbook.get(k)) != core(spark.get(k)))
print(f"{len(zenbook)} operational files; differing: {differing}")
sys.exit(1 if differing else 0)
EOF
```

Expected: the push ends with the identity of `/srv/quant_data`; then `9 operational files; differing: []`. A file that differs stops the cutover: push again, compare again. If it still differs, call the evening off as the table above says for steps 3 to 6.

- [ ] **Step 6: Parity, while both roots are still frozen**

Nothing runs on either machine, so the snapshot the push froze and the Spark's live root hold the same bytes:

```bash
source scripts/spark/env.sh
EV=$(cat ~/quant_data_snapshot/parity/cutover_current.txt)
python scripts/spark/parity_score.py --root ~/quant_data_snapshot --out "$EV/parity_zenbook.json"
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python scripts/spark/parity_score.py --root /srv/quant_data --out ~/parity_spark.json'
"$SCP" spark-rt:parity_spark.json "$EV/parity_spark.json"
"$SCP" "$EV/parity_zenbook.json" spark-rt:parity_zenbook.json
python scripts/spark/compare_json.py "$EV/parity_zenbook.json" "$EV/parity_spark.json" --ignore identity.machine --differ identity.machine --rtol 1e-8 --atol 1e-10 --min-numeric 1000 | tail -n 3
```

Expected: `PARITY OK`, with a `machines:` line naming an x86 Windows side and an aarch64 Linux side. The same two payloads are judged again by the gate in step 10. If this is not `PARITY OK`, call the evening off as for step 5.

- [ ] **Step 7 (account holder): the Zenbook's root becomes the kept root, and an empty mirror takes its name**

Nothing runs on the Zenbook and the push is verified. The swap is done before the Spark starts, so that from this moment no script on the Zenbook can write live state by mistake, and so that a failed gate can be undone by the real `rollback.sh`. Close anything that has a file open under `~/quant_data` (an Explorer window, an editor, a Python prompt). In PowerShell, from the main checkout:

```powershell
.\scripts\scheduling\swap_live_root_for_mirror.ps1
Get-ChildItem $env:USERPROFILE -Directory -Filter 'quant_data*' | Format-Table Name
```

Expected: `kept root:  C:\Users\asus\quant_data_zenbook_final` and `new mirror: C:\Users\asus\quant_data (empty, carries .mirror-of-spark)`, then the three names `quant_data`, `quant_data_snapshot`, `quant_data_zenbook_final`. A line that starts `swap refused:` means nothing was changed: remove what it names and run it again. **Do not create the marker by hand.** The task `\Spark\Spark Pull` stays disabled until the gate has passed.

- [ ] **Step 8: Log in on the Spark, start the recorder, then the timers**

First the Zenbook once more: half an hour has passed since step 3.

```bash
python scripts/spark/session_check.py --port 4002 --process ibgateway; echo "zenbook: exit=$?"
```

Expected: `zenbook: exit=0`. Anything else: nobody logs in on the Spark until it is.

**(account holder)** types the login into the dialog on the remote screen (runbook section 7.2). When the Gateway's main window is up, the executor confirms that the API answers `trading`, and fixes `SINCE` by the Spark's clock, the clock every later timestamp comes from:

```bash
source scripts/spark/env.sh
EV=$(cat ~/quant_data_snapshot/parity/cutover_current.txt)
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python -c "
from src.venues.ibkr.clients import connect_ib
from src.venues.ibkr.settings import IBKRSettings
ib = connect_ib(\"research\", IBKRSettings())
print(\"server time:\", ib.reqCurrentTime())
ib.disconnect()
"'
"$SSH" spark-rt 'date -u +%FT%T+00:00' | tr -d '\r' | tee "$EV/since.txt"
```

Expected: a `server time:` line; the second command prints and stores `SINCE`. If the login does not work (the dialog rejects it, the phone prompt never comes, the screen is lost), the way out is step 11. Then **(account holder)**, on the Spark, the recorder alone:

```bash
sudo systemctl enable --now ibkr-recorder.service
```

The timers wait for the first sweep: the heartbeat file is not copied by the push, so a probe that ran before the first sweep had finished would find no heartbeat and send a DOWN message for nothing. Watch for the sweep (about twelve minutes):

```bash
source scripts/spark/env.sh
"$SSH" spark-rt 'ls /srv/quant_data/ibkr_ticks/quotes/date=$(date -u +%F)/ | tail -n 2; cat /srv/quant_data/ibkr_ticks/state/recorder_state.json; echo'
```

Expected, once it has finished: a sweep file whose time is after `SINCE`, and a heartbeat with `rows` above 2000. Then **(account holder)**:

```bash
sudo systemctl enable --now ibkr-health.timer ibkr-paper-sync.timer climate-labels.timer paper-book-backup.timer
```

- [ ] **Step 9: Exercise what the gate looks at**

```bash
source scripts/spark/env.sh
"$SSH" spark 'sudo -n /usr/bin/systemctl start ibkr-paper-sync.service; echo "sync: rc=$?"'
```

**(with the account holder's go-ahead, who then says whether the message arrived on the phone)** the test alert:

```bash
source scripts/spark/env.sh
"$SSH" spark-rt 'cd forecast-model && set -a && . ~/.config/forecast-model/alerts.env && set +a && .venv/bin/python scripts/check_ibkr_recorder.py --test-alert'
```

Then the probe, run as the timer runs it:

```bash
source scripts/spark/env.sh
"$SSH" spark 'sudo -n /usr/bin/systemctl start ibkr-health.service; echo "probe unit: rc=$?"; systemctl show ibkr-health.service --timestamp=unix -p ExecMainStatus -p ExecMainExitTimestamp'
```

Expected: `sync: rc=0`; `test alert delivered via telegram` and the message on the phone; `probe unit: rc=0`, `ExecMainStatus=0` and an exit time after `SINCE`. The external check `spark-health` stays green with this run. `ExecMainStatus=1` means the probe found a problem; `"$SSH" spark-rt 'tail -n 3 forecast-model/logs/ibkr_health.log'` says which. `probe unit: rc=1` means the unit itself failed, which is not the same thing: `ExecMainStatus=3` is a probe that crashed (an uncaught exception or a broken import; nothing was pinged, and the traceback is in `"$SSH" spark 'journalctl -u ibkr-health.service -n 30 --no-pager'`), and a run that stopped at the owner-rule pre-check never started the probe, so systemd resets `ExecMainStatus` to 0 and leaves `ExecMainExitTimestamp` empty (the unit is `failed`; a 0 with no exit time is not a healthy probe), and `"$SSH" spark 'systemctl status ibkr-health.service --no-pager'` is what shows it (look at `ibgw-owner-rule.service` first).

- [ ] **Step 10: The gate**

```bash
source scripts/spark/env.sh
EV=$(cat ~/quant_data_snapshot/parity/cutover_current.txt)
[ -s "$EV/since.txt" ] && "$SSH" spark-rt "cd forecast-model && .venv/bin/python scripts/spark/cutover_check.py --root /srv/quant_data --repo /home/trading/forecast-model --since $(cat "$EV/since.txt") --parity ~/parity_zenbook.json ~/parity_spark.json" | tee "$EV/gate.txt"
```

Expected: four `PASS` lines (the `PASS parity` line is followed by compare_json's own output lines, indented) and `CUTOVER GATE: PASS`. (No output at all means `since.txt` is missing or empty: go back to step 8's second block; do not stamp a new time now, or everything that happened before it stops counting.) **Every line must hold.** A line that fails for a reason that can be cured on the spot (the probe ran before the sweep had finished) is cured and the gate is run again, within the hour.

The sync line passes when the book carried over, not when the counts are equal: with "before" the last ok sync before `SINCE` and "after" the first ok sync after it (the newest sync must also be ok), `examined` after equals `examined` before minus `filled` before minus `cancelled` before, and `unmatched_venue_orders` is unchanged; `filled`, `partially_filled`, `cancelled` and `unchanged` may differ, because the venue may have filled or cancelled an order between the two syncs, and the line prints each one that did with both values, which the account holder looks up in the Client Portal and records in the report; any other difference is a `FAIL sync` that prints the identity. The sync line's detail ends with `(before <time>Z, after <time>Z, N not-ok run(s) between)`, which names the two syncs it compared (the "before" and the "after" above) by their UTC times; a non-zero N means a failed sync ran between them, and a failed sync that concluded rows breaks the identity without the book being broken, so look at the sync ledger (its lines carry the account id: read only `ts_utc` and `ok`) before deciding on step 11. Any line that still fails means step 11.

- [ ] **Step 11: Only if something from step 7 on failed -- roll back, the same evening**

```bash
source scripts/spark/env.sh
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh" --dry-run | grep -E "^== |DRY-RUN: rsync" | cut -c1-110
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh"; echo "rollback: exit=$?"
```

Expected: the dry run lists its five steps; the real run ends with the by-hand list and `rollback: exit=0`. **Only exit 0 allows a login on the Zenbook.** Exit 4 means the Spark may still hold the session (it could not be reached, a stop was refused, or the check found a Gateway): the script says which, and nobody logs in on the Zenbook until a later run ends with 0, or the container has been stopped at the Spark itself. Exit 5 is a failed copy after the session was released (rsync's own status is in the message): run it again. Then **(account holder)** does what the script printed, in its order: disable the units on the Spark; on the Zenbook disable the pull task (`Disable-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'`), check the copied book with `python scripts/spark/restore_check.py --from ~/quant_data_zenbook_final --scratch <a fresh directory>` (it must print `RESTORE OK`), then `.\scripts\scheduling\swap_live_root_for_mirror.ps1 -Undo` and re-enable the four tasks; open the Gateway and log in. The report of step 13 is written with the verdict `ROLLED BACK` and the reason, and the plan stops until the cause is understood.

- [ ] **Step 12: After a passing gate -- start the pull, mark the research mirror, see the marker work**

**(account holder)**, in an elevated PowerShell on the Zenbook:

```powershell
Enable-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'
Start-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'
```

Wait about two minutes for the first pull, then the executor, from the main checkout (the task logs there):

```bash
source scripts/spark/env.sh
"$SSH" spark 'printf "mirror of /srv/quant_data, created %s\n" "$(date -u +%FT%TZ)" > ~/quant_data/.mirror-of-spark && cd ~/work/forecast-model && bash scripts/spark/mirror_live.sh | tail -n 2'
tail -n 4 logs/spark_pull_task.log
ls ~/quant_data/ibkr_ticks/quotes | tail -n 2
python scripts/run_gtm_paper.py; echo "a live-writing script on the mirror: exit=$?"
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh" --dry-run; echo "a push from the mirror: exit=$?"
```

Expected: `mirror_live: /home/scura/quant_data refreshed from /srv/quant_data`; the pull log ends with `pull_snapshot: done ...` and `heartbeat: sent`, and the check `offbox-pull` turns green; today's date directory is in the Zenbook's mirror; `refusing to write GTM paper signals: ... is a mirror of the live root` and `exit=2`; `push_snapshot: ... carries .mirror-of-spark; ... never pushed` and `exit=3`. The marker now guards both mirrors from both sides. (`~/quant_data_backups` fills after the first 21:00 backup.)

- [ ] **Step 13: Write the cutover report and make the documents say what is true**

Create `docs/spark_runtime_cutover_<date>.md` with: the date, the start and end times and how long recording was interrupted (the Zenbook's last sweep, the Spark's first); the commit both machines ran; the session check's lines from steps 3 and 8; the manifest line of step 5 and where the two manifests are; the parity line of step 6 (copy `parity_zenbook.json` and `parity_spark.json` next to the report as `docs/spark_runtime_cutover_<date>_zenbook.json` and `_spark.json`, as the parity report does); `SINCE`; the lines of `gate.txt` verbatim and the verdict; what was done by hand; the hourly backfill owed for the gap (Task 13 step 2). The account is called "the paper account": no id, no address, no token.

In `docs/spark_runbook.md`, replace the notice under the title with

```markdown

> **Since <date> the Spark is the trading runtime** (cutover report:
> `docs/spark_runtime_cutover_<date>.md`). Section 7 is the operator's page.
> Sections 1 and 3 describe the arrangement before that date and are kept
> for the rollback week only; where they disagree with section 7, section 7
> holds. Sections 2, 4, 5 and 6 stand.
```

and the status paragraph at the top of section 7 (from `**Status: built, not yet in force.**` to the sentence that names the design) with

```markdown
**Status: in force since <date>** (cutover report:
`docs/spark_runtime_cutover_<date>.md`). Design:
`docs/plans/2026-10-05-spark-runtime-move-design.md`.
```

In `docs/ibkr_runbook.md`, replace the notice above `Quick facts:` with

```markdown
> **Since <date> the runtime runs on the DGX Spark.** The operator's page is
> `docs/spark_runbook.md` section 7: login 7.2, units and logs 7.3, deploy
> 7.4. On this page, sections 2, 5, 7 and 10 still describe how the recorder,
> the restart window, the alerts and the monthly rituals behave; sections 1,
> 3, 4, 6, 8 and 9 describe the Zenbook's Task Scheduler setup, which is
> disabled and kept for one rollback week. **Do not open IB Gateway on the
> Zenbook: a login there takes the session away from the Spark.**
```

In `docs/MIGRATION_CHECKPOINT.md` section 11, replace its first two sentences, the ones that open with `**Status: built, not cut over.**` (find them by that opening: the words after `still the runtime` have been reworded since this plan was written), with

```markdown
**Status: cut over on <date>; acceptance pending.** The Spark is the runtime
(cutover report `docs/spark_runtime_cutover_<date>.md`); the Zenbook's four
scheduled tasks are disabled and its old live root is kept as
`~/quant_data_zenbook_final` for one rollback week. Wherever sections 0 to 8
say "Task Scheduler" or `~/quant_data` on the Zenbook, read "systemd on the
Spark" and `/srv/quant_data`.
```

In all four, `<date>` is the cutover's date. Then, on `spark-runtime-move` in the worktree (the branch has been at `main` since Task 11):

```bash
python -m pytest tests/test_architecture/test_runbook_names.py -q
git add docs/spark_runtime_cutover_* docs/spark_runbook.md docs/ibkr_runbook.md docs/MIGRATION_CHECKPOINT.md
git commit -m "Record the cutover of the runtime to the Spark"
```

Expected: `4 passed`. Land it with `git -C /c/Users/asus/OneDrive/Desktop/new_work merge --ff-only spark-runtime-move` and deliver it with `bash scripts/spark/deploy.sh --no-restart` (the main checkout is no longer the runtime, so the hour no longer matters).

---

### Task 13: The first week, acceptance, and retiring the Zenbook's tasks

The cutover is accepted when a week of evidence says so, not when the gate passed. Until then the Zenbook's four tasks stay registered and disabled, and its old root stays in place: that is the rollback path.

**Files:**
- Modify: `docs/spark_runtime_cutover_<date>.md` (an acceptance section), `docs/spark_runbook.md`, `docs/ibkr_runbook.md`, `docs/MIGRATION_CHECKPOINT.md`

**Interfaces:**
- Consumes: the running system; `restore_check.py` (Task 5), `rollback.sh` and `research_run.sh` (Task 7), `unregister_ibkr_tasks.ps1` (Task 8).
- Produces: the accepted state, written into the report.

- [ ] **Step 1: Every day of the week -- the sweep count and the checks**

```bash
source scripts/spark/env.sh
"$SSH" spark-rt 'cd /srv/quant_data/ibkr_ticks/quotes && for d in $(ls -d date=* | tail -n 8); do echo "$d $(ls "$d" | wc -l)"; done'
"$SSH" spark 'systemctl list-timers --no-pager "ibkr-*" "climate-*" "paper-*" | cut -c1-110; ls /srv/quant_data_backups | tail -n 3'
tail -n 2 /c/Users/asus/OneDrive/Desktop/new_work/logs/spark_pull_task.log
```

A full UTC day holds 72 sweeps. The criterion is seven consecutive days with at least 70, where a day that contains the weekly login (Sunday), the planned reboot of step 6 or another planned restart is judged without that window: note the window's length in the report. All four external checks stay green. Record each day's count.

- [ ] **Step 2: The day after -- fill the cutover's gap with hourly bars**

The backfill refuses a window that reaches today, so this runs on the first day after the cutover, as `trading`, away from 16:30, 17:30 and 23:45 (it takes about two hours; start it inside `tmux` on the Spark). `<date>` is the cutover's date; the day holds sweeps on both sides of the gap, so it is named with `--refetch`:

```bash
source scripts/spark/env.sh
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python scripts/backfill_ibkr_hourly.py --start <date> --end <date> --refetch <date> --dry-run 2>&1 | grep -v "account=" | tail -n 8'
```

Expected: a plan for one date and about 340 requests. Then **(account holder, or the executor at their word)** the same command without `--dry-run` and without the `tail`, inside `tmux`; a clean run ends with `Failed 0` and exit 0 (exit 7: run it again, stored bars are never written twice). Record the run id in the report.

- [ ] **Step 3 (account holder): the weekly login from outside the home network**

On the first Sunday, do the login of runbook section 7.2 with the laptop on a phone hotspot. Record the time the DOWN message arrived, the time of the login and the time of the recovery message.

- [ ] **Step 4: A research job beside the runtime, with the kill order read back**

```bash
source scripts/spark/env.sh
"$SSH" spark 'cd ~/work/forecast-model && bash scripts/spark/research_run.sh -- .venv/bin/python scripts/fit_gtm_emos.py --trend --root ~/quant_data/climate_data | tail -n 3'
"$SSH" spark 'echo "recorder $(cat /proc/$(systemctl show -p MainPID --value ibkr-recorder.service)/oom_score_adj)"; for c in ibgw qwenflash; do echo "$c $(cat /proc/$(docker inspect -f "{{.State.Pid}}" "$c")/oom_score_adj)"; done; bash ~/work/forecast-model/scripts/spark/research_run.sh -- sh -c "echo research \$(cat /proc/self/oom_score_adj)"'
```

Expected: the fit finishes; `recorder -900`, `ibgw -900`, `qwenflash -500`, `research 500`. The day's sweep count (step 1) shows the recorder kept its cadence while the job ran. Refresh the research mirror afterwards (`bash scripts/spark/mirror_live.sh`): the refit overwrote the mirror's model file, as a refit on a mirror always has.

- [ ] **Step 5: The hand-run path, as `trading`**

Orders are only ever placed by scripts a person starts. That path has not run on the Spark yet; run it once before the Zenbook's copy of it is retired. **(with the account holder's go-ahead)** Neither command places an order: the first writes the paper signals, as it does every time it is run; the second is the dress rehearsal.

```bash
source scripts/spark/env.sh
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python scripts/run_gtm_paper.py 2>&1 | grep -v "account=" | tail -n 5; echo "signals: rc=${PIPESTATUS[0]}"'
"$SSH" spark-rt 'cd forecast-model && .venv/bin/python scripts/run_ibkr_paper.py --simulate 2>&1 | grep -v "account=" | tail -n 8; echo "simulate: rc=${PIPESTATUS[0]}"'
```

Expected: `signals: rc=0` and a signals file newer than a minute under `/srv/quant_data/climate_data/paper/`; `simulate: rc=0` with the intents listed and nothing sent. A permission, an import or a settings error here is fixed now, not on the day an order matters.

- [ ] **Step 6 (account holder): one planned reboot**

The first power cut should not be the first test. On a weekday, away from the scheduled jobs: `sudo reboot` on the Spark. Then, as the machine comes back:

```bash
source scripts/spark/env.sh
"$SSH" spark 'systemctl is-active ibgw-owner-rule.service docker.service; docker ps --filter name=ibgw --format "ibgw: {{.Status}}"; ss -ltnH | grep -E ":(4002|5900) "; systemctl is-active ibkr-recorder.service; systemctl list-timers --no-pager "ibkr-*" "climate-*" "paper-*" | tail -n 2'
"$SSH" spark-rt 'timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/4002" && echo "trading: connected"'
"$SSH" spark 'timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/4002"; echo "scura: rc=$?"'
```

Expected: `active` twice; the container `Up` again by itself (it was running, and `unless-stopped` brings it back); both ports on `127.0.0.1` only; the recorder `active` and retrying; four timers listed; `trading: connected` and `scura: rc=1` (the owner rule came back before Docker did). The Gateway shows the login dialog: a reboot is a cold start. The DOWN message arrives within the hour; **(account holder)** logs in (runbook 7.2); the recovery message follows. Record the three times, and how long recording was interrupted.

- [ ] **Step 7: The restore check against the copy off the machine**

After at least one 21:00 backup has been pulled (Git Bash, main checkout):

```bash
NEWEST=$(ls ~/quant_data_backups | grep -E '^[0-9]{8}$' | tail -n 1)
python scripts/spark/restore_check.py --from ~/quant_data_backups/$NEWEST --scratch "$TEMP/restore_check_$NEWEST"
python scripts/spark/restore_check.py --from ~/quant_data --scratch "$TEMP/restore_check_mirror"
rm -rf "$TEMP/restore_check_$NEWEST" "$TEMP/restore_check_mirror"
```

Expected: `RESTORE OK: N files restored and verified` twice. This is the restore that has been tried, from the copy that would survive the Spark. The day directory is the one that counts: its paper book was written through SQLite's backup API. The mirror's paper book is a plain file copy, and a pull that landed inside the 17:30 sync can have copied it torn; if only the mirror's check fails, and only on the paper book, pull once more and check again before calling it a failure.

- [ ] **Step 8: The rollback's dry run, now against the real kept root**

```bash
source scripts/spark/env.sh
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/rollback.sh" --dry-run | grep -E "^== |DRY-RUN: rsync|quant_data_zenbook_final" | cut -c1-110
```

Expected: the five headings in order, and the rsync line ending in `/mnt/c/Users/asus/quant_data_zenbook_final/`. Nothing was stopped and nothing was copied.

- [ ] **Step 9: Acceptance**

All of these hold, each with its evidence written into an `## Acceptance` section of the cutover report:

- seven consecutive days with at least 70 sweeps (step 1);
- the cutover's gap filled with hourly bars (step 2);
- the weekly login done from outside the home network (step 3);
- a research job ran while the recorder kept its cadence, and the kill order read -900, -900, -500, +500 (step 4);
- the hand-run path ran as `trading` (step 5);
- the Spark came back from a reboot the way step 6 describes;
- the restore check passed against the Zenbook's copy (step 7);
- the rollback's dry run verified (step 8);
- the four external checks green for the week.

If one does not hold, the week is extended or the rollback of runbook section 7.9 is run; the Zenbook's tasks are not unregistered. When all hold, step 10 replaces the old invariants in the documents and step 11 retires the Zenbook's tasks.

- [ ] **Step 10: The documents, for the last time -- the old invariants are replaced**

The criteria of step 9 hold, so the pages stop carrying the old arrangement beside the new one. This is the last criterion ("runbooks updated and the old invariants replaced"); the Zenbook's tasks are unregistered after it, in step 11.

`docs/spark_runbook.md`:

1. The title becomes `# DGX Spark runbook -- trading runtime and research`, and the notice under it is deleted.
2. Section 1, from its heading to the line before `## 2. Layout on the Spark`, becomes:

```markdown
## 1. What the Spark is

The Spark (`spark-8f47`, aarch64, 128 GB unified memory) is three things since
the cutover of <date>: the trading runtime (section 7), the research machine,
and the host of the local vLLM model. They are kept apart by Linux users: the
runtime runs as `trading`; research and the LLM stack run as `scura`; only
processes of `trading` can reach the Gateway (section 7.2).

| Role | clientId | Where |
| --- | --- | --- |
| `live_trading` | 1 | Spark, `trading`, by hand (`scripts/run_ibkr_paper.py`) |
| `data_collector` | 2 | Spark, `ibkr-recorder.service` |
| `research` | 3 | Spark, `trading`, by hand |
| `paper_sync` | 4 | Spark, `ibkr-paper-sync.timer` |

Never run as `scura`, from the research clone or on the Zenbook: the scripts
that talk to the venue or write live state (section 7.5). Two things stop
them there: they cannot reach the Gateway (no Gateway settings, and the owner
rule), and they refuse a data root that carries the mirror marker.

```

3. In section 2, the bullet that begins ``- Data: `~/quant_data` -- a MIRROR pushed from the Zenbook`` becomes:

```markdown
- Data: `~/quant_data` -- a MIRROR of the live root `/srv/quant_data`,
  refreshed by `scripts/spark/mirror_live.sh` (section 7.6) and marked by the
  file `.mirror-of-spark`. Same default root as everywhere on purpose; no
  `IBKR_DATA_ROOT` / `CLIMATE_DATA_ROOT` overrides anywhere.
```

4. Section 3, from its heading to the line before `## 4. Parity check`, becomes:

```markdown
## 3. Where data is written, and where it is copied

Live state is written in one place: `/srv/quant_data` on the Spark, by
`trading`. From there it is copied, one way, to the two mirrors (section 7.6)
and to the daily backups (section 7.7). Nothing is copied from a mirror into
the live root, with two exceptions that are done by hand and on purpose: the
rollback of section 7.9 and the restore after a lost disk of section 7.7.
`scripts/spark/push_snapshot.sh`, which fed the Spark from the Zenbook until
the cutover, refuses to run on a mirror.

To prove that something left the live root alone, bracket it with the
manifest, as `trading`, and `diff` the two files:

    "$SSH" spark-rt 'cd forecast-model && .venv/bin/python scripts/spark/live_manifest.py --root /srv/quant_data --out ~/manifest_before.json'

The operational `models/gtm_emos.json` and `paper/gtm_signals.json` are
produced on the Spark only, by `trading` (`fit_gtm_emos.py --live` and
`run_gtm_paper.py` there). Outputs written into a mirror by research
(refits, experiments) are ephemeral: the next refresh overwrites any file the
live root also holds.

```

5. In section 4, the code block that begins `# Zenbook, Git Bash, from the repo root, with the work committed.` becomes:

```markdown
    # Both sides score their own mirror. Refresh both right after the same
    # sweep (section 7.3 shows the heartbeat), so that they hold the same bytes.
    source scripts/spark/env.sh
    "$SSH" spark 'cd ~/work/forecast-model && bash scripts/spark/mirror_live.sh'
    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/pull_snapshot.sh"
    python scripts/spark/parity_score.py --root ~/quant_data --out ~/quant_data_snapshot/parity/zenbook.json
    "$SSH" spark 'cd ~/work/forecast-model && .venv/bin/python scripts/spark/parity_score.py --root ~/quant_data --out ~/parity_spark.json'
    "$SCP" spark:parity_spark.json ~/quant_data_snapshot/parity/spark.json
    python scripts/spark/compare_json.py ~/quant_data_snapshot/parity/zenbook.json ~/quant_data_snapshot/parity/spark.json --ignore identity.machine --differ identity.machine --rtol 1e-8 --atol 1e-10 --min-numeric 1000
```

   and in the same section every later `~/quant_data_snapshot` that names a root to score or to fit on (not the `parity/` evidence directory) becomes `~/quant_data`, the Zenbook's mirror.
6. In section 6, the last sentence (`The Gateway's API port is never opened to the LAN: ...`) becomes: `The Gateway's API port is published on the Spark's loopback only and admits the user trading only (section 7.2); it is never opened to the LAN or to the tailnet.`
7. In the status paragraph of section 7, nothing changes.

`docs/ibkr_runbook.md`:

1. The notice above `Quick facts:` becomes

```markdown
> **Since <date> the runtime runs on the DGX Spark, and the Zenbook's
> scheduled tasks are unregistered.** The operator's page is
> `docs/spark_runbook.md` section 7. Sections 2, 5, 7 and 10 of this page
> still describe how the recorder, the restart window, the alerts and the
> monthly rituals behave. Sections 1, 3, 4, 6, 8 and 9 are history: they
> describe the Task Scheduler setup that ran until the cutover.
```

2. In the Quick facts table the rows `Gateway`, `Data root`, `Heartbeat`, `Recorder log` and `Scheduled tasks` become:

```markdown
| Gateway | IB Gateway in the container `ibgw` on the Spark; API on the Spark's `127.0.0.1:4002`, reachable by the Linux user `trading` only (`docs/spark_runbook.md` section 7.2) |
| Data root | `/srv/quant_data/ibkr_ticks` on the Spark (the Zenbook's `~/quant_data` is a mirror) |
| Heartbeat | `/srv/quant_data/ibkr_ticks/state/recorder_state.json` |
| Recorder log | `/home/trading/forecast-model/logs/ibkr_recorder.log` on the Spark |
| Scheduling | systemd on the Spark: `ibkr-recorder.service`, `ibkr-health.timer`, `ibkr-paper-sync.timer`, `climate-labels.timer`, `paper-book-backup.timer` (`docs/spark_runbook.md` section 7.3) |
```

3. In the table of section 2, the Machine column of all four rows becomes `Spark (as trading)`, and the sentence `The DGX Spark holds no client id (docs/spark_runbook.md section 1).` becomes `All four connect from the Spark; the Zenbook holds no client id any more.`

`docs/MIGRATION_CHECKPOINT.md`: in section 11, `**Status: cut over on <date>; acceptance pending.**` becomes `**Status: cut over on <date>, accepted on <date>.**`, and the sentence about the disabled tasks and the kept root becomes `The Zenbook's four scheduled tasks are unregistered; it is the development machine and holds a mirror of the live root.` In section 7 ("Open user actions"), item 1 becomes `**Log into the Gateway on the Spark every Sunday** (docs/spark_runbook.md section 7.2); the DOWN message says when it is due.`

Then, on `spark-runtime-move` in the worktree:

```bash
python -m pytest tests/test_architecture/test_runbook_names.py -q
python -m pytest -q 2>&1 | tail -n 1
git add docs/spark_runtime_cutover_* docs/spark_runbook.md docs/ibkr_runbook.md docs/MIGRATION_CHECKPOINT.md
git commit -m "Accept the runtime on the Spark and retire the Zenbook's scheduled tasks"
```

Expected: `4 passed`, the suite green. Land it as in Task 12 step 13.

- [ ] **Step 11 (account holder): retire the Zenbook's runtime**

In an elevated PowerShell, from the main checkout:

```powershell
.\scripts\scheduling\unregister_ibkr_tasks.ps1
.\scripts\scheduling\unregister_climate_tasks.ps1
Get-ScheduledTask -TaskPath '\IBKR\','\Climate\' -ErrorAction SilentlyContinue | Format-Table TaskName, State
```

Expected: `Removed:` for each of the three `\IBKR\` tasks, `Removed \Climate\Climate Labels (if it existed).`, and nothing listed by the last command. `\Polymarket\backup_db` is not touched. Remove the IB Gateway shortcut from the Zenbook's Start menu and desktop too (or uninstall it): the weekly login is done on the Spark's remote screen now, and a login on the Zenbook out of habit would take the session away from the Spark. The kept root `~/quant_data_zenbook_final` is deleted by the account holder, by hand, one week after acceptance and not before `python scripts/spark/restore_check.py --from ~/quant_data --scratch <a fresh directory>` has passed once more; its 7.6 GB of raw CDS downloads then exist on the Spark only, so pull once with `--include-raw` first if a second copy is wanted.

What comes next is the security layer in front of the Gateway (spec D4, D5): its own design, before any real money.
