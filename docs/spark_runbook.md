# DGX Spark runbook -- research mirror

> **Runtime move (2026-10): built, not yet in force.** Sections 1 to 6 describe
> the system as it runs: the Zenbook is the runtime, the Spark a research
> mirror. Section 7 describes the Spark as the runtime and takes over at the
> cutover (`docs/spark_runtime_cutover_<date>.md`, when it exists).

Parity evidence:
the newest `docs/spark_parity_*.md` with its `_zenbook.json` / `_spark.json`
payloads. This page is the operator's view.

Git Bash commands on this page assume `source scripts/spark/env.sh` from the
repo root. It defines `SSH` and `SCP` (the Windows OpenSSH `ssh.exe` and
`scp.exe`, which read the `Host spark` entry of `~/.ssh/config`) and
`REPO_WSL` (this checkout's path as WSL sees it; section 3).

## 1. What the Spark is, and is not

The Spark (aarch64, 128 GB unified memory) runs the research
half of this repo and the local vLLM model. It is NOT a trading machine:

| Role | clientId | Machine |
| --- | --- | --- |
| `live_trading` | 1 | Zenbook (`scripts/run_ibkr_paper.py`, operator-run) |
| `data_collector` | 2 | Zenbook, Task Scheduler `\IBKR\IBKR Recorder` |
| `research` | 3 | Zenbook, ad hoc (not the Spark in this phase) |
| `paper_sync` | 4 | Zenbook, Task Scheduler `\IBKR\IBKR Paper Sync` |

The Spark holds no client id, and two independent guards keep it from
trading even by accident:

- The Gateway's API cannot be reached from the LAN. The guard is NOT the bind
  address: the Gateway listens on every interface (`[::]:4002`, checked
  2026-10-03 with `Get-NetTCPConnection -State Listen -LocalPort 4002`). What
  closes it is Windows Firewall (two inbound Block rules named `IB Gateway` on
  the Public profile, which the Wi-Fi is in, and no Allow rule) plus the
  Gateway's own trusted list (`TrustedIPs=127.0.0.1` in its `jts.ini`).
  Verified from the Spark the same day: a TCP connect to the Zenbook's 4002
  gets no answer. Re-check after a Gateway reinstall, a Windows "allow access"
  prompt, or a change of network profile (Public to Private); from Git Bash on
  the Zenbook, `rc=124` (timeout) is the passing result:

      "$SSH" spark 'timeout 6 bash -c "exec 3<>/dev/tcp/${SSH_CLIENT%% *}/4002"; echo "rc=$?"'
- The Spark's `.env` and shell carry no `IBKR_PORT` / `IBKR_TRADING_MODE`, so
  the entrypoints that build `IBKRSettings` (`record_ibkr_ticks.py`,
  `run_ibkr_paper.py`, `sync_ibkr_paper.py`, and `backfill_ibkr_hourly.py`
  unless it is given both `--port` and `--mode`) stop at startup with
  pydantic's `ValidationError` (proof: section 2 of the 2026-10-03 parity
  report). The two standalone diagnostics, `verify_ibkr_entitlements.py` and
  `probe_ibkr_history.py`, build no settings and dial `127.0.0.1`, which on
  the Spark is the Spark itself, with no Gateway behind it.

Never run on the Spark (all under `scripts/`):

- The six Gateway clients: `record_ibkr_ticks.py`, `sync_ibkr_paper.py`,
  `run_ibkr_paper.py`, `backfill_ibkr_hourly.py`,
  `verify_ibkr_entitlements.py`, `probe_ibkr_history.py`.
- `collect_climate_labels.py`: the NOAA ledger is canonical on the Zenbook
  and a Spark run would fork it. Nothing technical stops it (it needs no
  credential and no Gateway), so the rule is the only guard. A forked ledger
  is overwritten by the next push, or fails the parity identity on
  `labels_sha256` if you score first.
- `fetch_climate_forecasts.py`: there is no `~/.cdsapirc` on the Spark.

## 2. Layout on the Spark

- Repo: `~/work/forecast-model`, a non-bare checkout with
  `receive.denyCurrentBranch=updateInstead`. The Spark never pulls from
  GitHub: its GitHub key only works in the operator's interactive session
  and there is no credential helper, so no GitHub credential is used there.
  Code arrives by `git push spark main` from the Zenbook, which updates the
  checked-out branch in place. The push is refused while any tracked file on
  the Spark has uncommitted changes (untracked files such as `.env` and
  `.venv` do not matter); this prints nothing when the tree is clean:

      "$SSH" spark 'cd ~/work/forecast-model && git status --short --untracked-files=no'

  On the Zenbook the remote `spark` is `spark:work/forecast-model` and the
  repo-local `core.sshCommand` is `C:/Windows/System32/OpenSSH/ssh.exe`, so
  the push uses the same ssh client, key and `Host spark` entry as every
  other command here and works from Git Bash. Both settings live only in the
  Zenbook's `.git/config`, so a fresh clone needs them again:

      git remote add spark spark:work/forecast-model
      git config core.sshCommand C:/Windows/System32/OpenSSH/ssh.exe

  `git push` to GitHub remains the separate, normal step. While the work was
  in flight the Spark's checkout followed its branch
  (`git push spark spark-research-split`). After the merge, switch it once.
  First bring the Zenbook's local `main` up to date (in the main checkout),
  because that is the `main` the push delivers: with the pre-merge one, the
  `git checkout main` on the Spark would take away `scripts/spark/` and
  `src/llm/`.

      git checkout main && git pull
      git push spark main
      "$SSH" spark 'cd ~/work/forecast-model && git checkout main'

  From then on every `git push spark main` updates `main` in place.
- venv: `.venv` (uv-managed Python 3.14.2, pinned by the newest
  `constraints/zenbook-*.txt`). To rebuild it, in a shell on the Spark
  (`ssh spark`; uv lives in `~/.local/bin`, which a non-interactive ssh
  command does not have on its PATH, hence the full path):

      cd ~/work/forecast-model && rm -rf .venv && ~/.local/bin/uv venv .venv --python 3.14.2
      ~/.local/bin/uv pip install -e ".[dev,ibkr,climate,mcp,web,trading]" -c constraints/zenbook-2026-10-03.txt

  uv 0.12.22 has no `--clear` for an existing `.venv` (only
  `--allow-existing`), hence the `rm -rf`; swap in a newer constraints file
  when one exists.
- Data: `~/quant_data` -- a MIRROR pushed from the Zenbook (section 3). Same
  default root as the Zenbook on purpose; no `IBKR_DATA_ROOT` /
  `CLIMATE_DATA_ROOT` overrides anywhere.
- `.env`: exactly one line, `LLM_MODEL=Qwen3.8-Flash-Next-NVFP4` (mode 600).
  No API keys.
- LLM stack: `~/stack/docker-compose.yml` (repo `curasinan/spark-llm-stack`;
  services `qwenflash` (vLLM), `openwebui`, `portainer`, `phoenix`); vLLM on
  `127.0.0.1:8000`, Open WebUI 8080, Portainer 9000, Phoenix 6006/4317, all
  bound to loopback (checked 2026-10-03: the only socket listening on all
  interfaces is 22/tcp) and behind ufw (22/tcp is the only open port). Images
  are pinned by digest: never `docker compose pull`, never edit the compose
  file without the operator. NVIDIA driver packages are on `apt-mark hold`:
  no `apt upgrade`.
- Memory, measured 2026-10-11 with Flash-Next loaded: weights 74.3 GiB and KV
  cache 10 GiB, about 87 GiB for vLLM in all; about 34 GiB of page cache holds
  the PLE sidecar, which vLLM reads through mmap; truly free is under 1 GiB.
  `free -h` counts that page cache as available (the 22-24 GiB this line used
  to quote), but a job that takes it evicts the sidecar and slows the LLM. So
  with Flash-Next loaded a host-memory job takes its memory from that cache,
  and there is no room for a GPU job at all. `docker stats` does not show
  vLLM's CUDA memory because the cgroup is not charged for it (section 7.10).
  For anything larger, finish the LLM batch first, then stop the vLLM service
  on the Spark: `cd ~/stack && docker compose stop qwenflash`.
- Tests, from the Zenbook:
  `"$SSH" spark 'cd ~/work/forecast-model && .venv/bin/python -m pytest -q'`.
  The suite isolates the home itself: `tests/conftest.py` points the home at
  a throwaway directory before anything is collected, and
  `tests/test_architecture/test_no_home_writes.py` fails the run if anything
  appears under that directory's `quant_data`. Before 2026-10-03 parts of the
  suite wrote under `~/quant_data/polymarket_weather` of whatever home they
  saw, which on the Spark is the mirror root, so every run needed an outer
  `HOME=$(mktemp -d)`; the parity report's runs were made that way (its
  section 12 records the change).

## 3. Pushing data (Zenbook -> Spark, never back)

On the Zenbook, outside 17:25-17:40 local (the paper-sync task writes the
SQLite DB then). PowerShell:

    wsl -d Ubuntu -- bash /mnt/c/Users/asus/OneDrive/Desktop/new_work/scripts/spark/push_snapshot.sh

Git Bash (MSYS would rewrite a bare `/mnt/...` argument into a Windows path;
`env.sh` defines `REPO_WSL` with the `//mnt` prefix that survives):

    source scripts/spark/env.sh
    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh"

`env.sh` derives `REPO_WSL` from the checkout it is sourced from, so these
two lines work from the main checkout and from any worktree. Both forms take
`--include-raw`, and `--help` prints the script's usage header. The scripts
under `scripts/spark/*.sh` must stay LF (`.gitattributes` enforces it on
checkout): an editor that saves CRLF breaks them under WSL's bash with
`$'\r': command not found`.

The script, in four steps:

1. Freeze `~/quant_data` into `~/quant_data_snapshot` (rsync with `--delete`,
   so the Zenbook-side parity run reads exactly the bytes the Spark gets).
   Skipped: `climate_data/raw/` (7.6 GB of re-downloadable CDS files; add
   `--include-raw` to ship it once, and only while no CDS fetch is running,
   because `*.part` partial downloads are not skipped), the recorder's
   `ibkr_ticks/state/`, `*.tmp`, `*-journal`, `*.db-wal` and `*.db-shm`.
   `parity/` and `identity_spark.json` inside the snapshot are evidence and
   survive the `--delete`. A source file that vanishes mid-copy (rsync exit
   24, a writer raced us) is retried once.
2. Push the snapshot to `spark:quant_data` (no `--delete`).
3. Verify on the Spark: `scripts/spark/verify_mirror.py` runs from the
   Spark's checkout, so deliver the code first (section 2). It exits 1
   naming every unreadable file. A push that lands inside the recorder's
   sub-second parquet write copies a torn sweep: just run it again. On
   success the identity it prints is saved as
   `~/quant_data_snapshot/identity_spark.json`, through a temp file moved
   into place only then, so a failed verify never leaves an empty identity
   and the previous one stays.
4. Print that saved identity.

To prove a push left the live root alone, bracket it with
`live_manifest.py` (read-only; it is the one tool here meant for the live
root) and `diff` the two files (parity report, section 3):

    # Git Bash, from the repo root (in PowerShell, diff is Compare-Object)
    python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/manifest_before_push.json
    # ... the push ...
    python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/manifest_after_push.json
    diff ~/quant_data_snapshot/parity/manifest_before_push.json ~/quant_data_snapshot/parity/manifest_after_push.json

Rules: no data is ever copied from the Spark into `~/quant_data` on the
Zenbook. The one thing that comes back is evidence: the Spark's parity
payload is copied into `~/quant_data_snapshot/parity/` (section 4) and from
there into git next to the report. Spark-side outputs (refits, parity
payloads, experiments) are ephemeral; the next push overwrites any canonical
file they touched. The operational `models/gtm_emos.json` and
`paper/gtm_signals.json` are produced on the Zenbook only
(`fit_gtm_emos.py --live` and `run_gtm_paper.py` there); on the Spark,
scoring is `parity_score.py`, which writes nothing under `paper/`.

## 4. Parity check (repeat after any numerical-stack change on either side)

    # Zenbook, Git Bash, from the repo root, with the work committed.
    # Both machines must run the same commit: main, or the branch both are on.
    source scripts/spark/env.sh
    git push spark main
    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh"
    python scripts/spark/parity_score.py --root ~/quant_data_snapshot --out ~/quant_data_snapshot/parity/zenbook.json
    "$SSH" spark 'cd ~/work/forecast-model && .venv/bin/python scripts/spark/parity_score.py --root ~/quant_data --out ~/parity_spark.json'
    "$SCP" spark:parity_spark.json ~/quant_data_snapshot/parity/spark.json
    python scripts/spark/compare_json.py ~/quant_data_snapshot/parity/zenbook.json ~/quant_data_snapshot/parity/spark.json --ignore identity.machine --differ identity.machine --rtol 1e-8 --atol 1e-10 --min-numeric 1000

`PARITY OK` at the registered tolerance is the gate (2026-10-03: 1536
numeric leaves, max relative difference 3.8e-10); the `identity.snapshot`
block, which is not ignored, proves both machines scored the same bytes at
the same commit. `--ignore identity.machine` keeps the machine blocks out of
the numbers (they differ by design) and `--differ identity.machine` closes
the gap that leaves: it fails unless both payloads have the block and the two
blocks are not identical, so two copies of one machine's payload (the wrong
file in the `spark.json` slot) cannot pass as cross-machine parity. The
`machines:` line names both sides (2026-10-03: `AMD64/Windows/py3.14.2`
against `aarch64/Linux/py3.14.2`). `--min-numeric 1000` is a floor on the
numeric leaves compared (the real payload has about 1500): an accidentally
empty signals list must not pass as parity, and a legitimately smaller
ladder fails it loudly, which is the cue to lower the floor with a note
(ruling R-L). The compare ignores `identity.machine`, `git_dirty` included,
so the CLI prints `WARNING: <side> payload has git_dirty=true` for any side
that says so (a warning, not a failure; no warning means nothing to grep
for). Treat it as a stop: a dirty tracked tree means the code that scored is
not the commit the identity names, so commit or discard the change and score
again. The tolerance is not 1e-15 because the
frame's ERA5->NOAA bridge is refit with LAPACK on each machine and reduction
order differs by CPU kernel (measured x86 kernel-to-kernel noise: 3e-10
relative); 1e-8 is still six orders below the smallest price step that
matters.

`verify_mirror.py` and `parity_score.py` are for mirrors and frozen roots.
On the Zenbook score `~/quant_data_snapshot`, which the push script has just
frozen, never the live `~/quant_data`: the scorer reads and hashes its inputs
at different moments, so on a root that moves (the live one, whenever the
recorder writes) the identity can describe bytes it did not score. It raises
if a newer sweep lands between its hashing and its scoring, but that is the
one movement it can see. It also refuses an identity whose code SHA is
unknown, which is what a checkout without git gives, so score from a git
checkout on both machines.

A refit comparison is report-only. Fit on both sides, the Zenbook on the
snapshot root. The fit runs only on a root it is given: with no `--root`,
or an empty one, it prints the files it would have overwritten and exits 2.
`--live` is the operational refit: it rewrites the live root's
`models/gtm_emos.json`, so it has no place in a comparison. Then fetch the
Spark's two artifacts into the snapshot's evidence directory, and compare
each pair files-first (`--ignore` takes a list, so it goes last; the
diagnostics also need `fitted_at_utc` and `artifact_paths` ignored):

    # Zenbook, Git Bash, after the scoring parity run
    python scripts/fit_gtm_emos.py --trend --root ~/quant_data_snapshot/climate_data
    "$SSH" spark 'cd ~/work/forecast-model && .venv/bin/python scripts/fit_gtm_emos.py --trend --root ~/quant_data/climate_data'
    "$SCP" spark:quant_data/climate_data/models/gtm_emos.json ~/quant_data_snapshot/parity/gtm_emos_spark.json
    "$SCP" spark:quant_data/climate_data/reports/gtm_emos_diagnostics.json ~/quant_data_snapshot/parity/diagnostics_spark.json
    python scripts/spark/compare_json.py ~/quant_data_snapshot/climate_data/models/gtm_emos.json ~/quant_data_snapshot/parity/gtm_emos_spark.json --rtol 1e-6 --atol 1e-9 --ignore fingerprint
    python scripts/spark/compare_json.py ~/quant_data_snapshot/climate_data/reports/gtm_emos_diagnostics.json ~/quant_data_snapshot/parity/diagnostics_spark.json --rtol 1e-6 --atol 1e-9 --ignore fitted_at_utc artifact_paths fingerprint

The optimiser amplifies the same kernel differences, and fresh artifacts
carry a `fingerprint` string that changes with any last bit: on 2026-10-03
one of the model's 50 coefficients (lead 2, `d`) differed by 1.2e-6
relative, and both compares printed `PARITY FAILED` (exit 1, expected
here); the design asks for an investigation only above 1e-4. A refit
overwrites the model and diagnostics under the root it ran on, so push again
before the next scoring parity run.

## 5. Using the local model

- From the Zenbook: open the tunnel with `ssh -L 8000:127.0.0.1:8000 spark`
  (Git Bash: `"$SSH" -L 8000:127.0.0.1:8000 spark`), then the default
  `LLM_BASE_URL=http://127.0.0.1:8000/v1` works unchanged. `LLM_MODEL` is
  required wherever the client runs (environment variable or that checkout's
  `.env`). The Spark's `.env` holds only that one line; `.env.example`
  documents every `LLM_` setting, `LLM_CONNECT_TIMEOUT_S` and
  `LLM_READ_TIMEOUT_S` included.
- The client talks only to `LLM_BASE_URL` (the loopback tunnel by default).
  It ignores proxy environment variables (`trust_env=False`) and therefore
  also `SSL_CERT_FILE` / `SSL_CERT_DIR`, so an `https://` base URL with a
  private CA is unsupported by design. The test that pins this
  (`test_client_without_a_transport_ignores_the_proxy_environment` in
  `tests/test_llm/test_client_request.py`) reads httpx internals, so an
  httpx upgrade past the 0.28.1 pinned in `constraints/` may fail it loudly:
  fix the test, do not delete it.
- Request rules (measured on the Spark): `max_tokens >= 8192` for real work
  (the settings default; the gated live test asks for a yes/no schema answer
  and uses 2048), `reasoning_effort=low` (default effort burns ~1.6k
  reasoning tokens), MTP off. Measured 2026-10-03: a schema-constrained
  extraction is about 100 completion tokens at `reasoning_effort=low`,
  11.7-16.4 tok/s single-stream, about 7 s end to end.
- After a reboot the stack comes up by itself in about 10 minutes
  (`ple-warm.service` warms the sidecar before docker; every service has
  `restart: always`). It is ready when, on the Spark,
  `docker inspect -f '{{.State.Health.Status}}' qwenflash` prints `healthy`.
  The served model name is `Qwen3.8-Flash-Next-NVFP4`.
- The server returns the reasoning text under `message.reasoning`, not
  `reasoning_content`; the client accepts both (`reasoning_content` when it
  is not null, else `reasoning`). Two replies captured from the Spark on
  2026-10-03 live under `tests/test_llm/fixtures/` as byte copies and are
  driven through `tests/test_llm/test_real_shapes.py`: replace them only with
  new captures, never edit them by hand (a hand-drawn fixture once hid
  exactly this bug).
- Checks, run from the repo root on the Spark (the settings read `.env`
  relative to the working directory). `scripts/llm_smoke.py` checks that
  `LLM_MODEL` is what the server serves and that a schema-constrained
  extraction round-trips; it exits 0 only then, and prints latency, tokens/s
  and the reasoning-token count. The live test is skipped unless
  `LLM_LIVE=1`. The test conftest clears every `LLM_*` variable except
  `LLM_LIVE` from its environment (`LLM_MODEL` included), so the live test
  reads `LLM_MODEL` only from `.env`:

      "$SSH" spark 'cd ~/work/forecast-model && .venv/bin/python scripts/llm_smoke.py'
      "$SSH" spark 'cd ~/work/forecast-model && LLM_LIVE=1 .venv/bin/python -m pytest tests/test_llm/test_live.py -q'

- Boundary: `src.llm` is never imported under `climate/`, `src/trading/`,
  `src/venues/`, `src/edge/`, `src/domain/`, `src/math/`, `src/models/`,
  `monitoring/`, nor in `scripts/run_ibkr_paper.py`,
  `scripts/sync_ibkr_paper.py`, `scripts/record_ibkr_ticks.py`,
  `scripts/run_gtm_paper.py`, `scripts/fit_gtm_emos.py` or
  `scripts/collect_climate_labels.py` (`tests/test_architecture/test_llm_boundary.py`;
  a literal `REQUIRED_SCOPE` list in that file pins the set, so narrowing it
  takes an edit to the list). The last two are guarded because they produce
  the operational model and its training labels. `src/collectors/` is
  deliberately outside: it is where text-to-data processing may live. The
  scan reads imports only: it cannot see transitive imports, `importlib` /
  string-named imports or `from src import *`. The LLM turns text into data;
  the numeric model prices.

## 6. When the network changes

The Spark's LAN address is a DHCP reservation on its Wi-Fi MAC; switching to
Ethernet needs a second reservation. The move to the US changes the address
entirely: mDNS (the Spark's `.local` name) or Tailscale keep `ssh spark`
working. The `Host spark` entry of the Zenbook's `~/.ssh/config` is the single
source of truth for where the Spark is, and everything on this page reaches
it through that entry (`ssh`, `git push spark`, the data push, the tunnel),
so a new address is an edit to that one entry.
The Gateway's API port is never opened to the LAN: a Spark-side `research`
role, if it ever comes, goes over Tailscale first (operator decision
2026-10-02).

## 7. The trading runtime on the Spark

**Status: built, not yet in force.** Until a cutover report
(`docs/spark_runtime_cutover_<date>.md`) exists, the Zenbook runs the runtime,
`docs/ibkr_runbook.md` describes it, and sections 1 to 3 above are the rules
in force. From the cutover on, this section replaces them wherever they
disagree.

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
  writes live state refuses a marked root (exit 2); the two mirror-filling
  scripts, `scripts/spark/pull_snapshot.sh` and `scripts/spark/mirror_live.sh`,
  refuse an unmarked root (exit 3).
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
  there, as `scura`, from `~/work/forecast-model` on the Spark:

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

`ExecMainStatus` is the probe's own exit status: 0 healthy, 1 a problem found and
alerted, 3 the probe crashed. A run that stopped at `ExecStartPre` (the owner-rule
pre-check) never started the probe: systemd resets `ExecMainStatus` to 0 and
leaves `ExecMainExitTimestamp` empty, and the unit is `failed`, so a 0 with no
exit time is not a healthy probe. `systemctl status ibkr-health.service` (or
`journalctl -u ibkr-health.service`) shows the failed pre-check.

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

- The recorder is restarted only if `systemctl is-active` says `active` both
  before the wait and again right before the restart. A deploy never starts
  the runtime: a recorder that is stopped (before the cutover, and after a
  rollback, it is stopped on purpose), or that stopped during the wait, is
  left alone.
- Exit 3 means a unit file changed or is not installed (a new unit file, for
  example): the script prints the `sudo install` lines; the account holder runs
  them and `sudo systemctl daemon-reload` on the Spark, then the deploy is run
  again. A new timer also needs `sudo systemctl enable --now <its name>`.
- When the recorder's state cannot be asked, the deploy stops with ssh's own
  status (for example 255, a lost connection) and says that the recorder was
  NOT restarted; it stops with 4 when systemd reports the unit unloaded (a
  missing `sudo systemctl daemon-reload`). Exit 2 is a usage error; any other
  status is the failing step's own.
- The research clone is a separate delivery: `git push spark main`
  (section 2).
- When `pyproject.toml` or the constraints file changed, deliver the code
  without a restart first, then install as `trading` from the runtime clone,
  which now holds the new files, then deploy again, so that the recorder is
  restarted after a fresh sweep on the new code with the new packages:

      bash scripts/spark/deploy.sh --no-restart
      "$SSH" spark-rt 'cd forecast-model && uv pip install --python .venv/bin/python -e ".[dev,ibkr,climate,mcp,web,trading]" -c "$(ls constraints/zenbook-*.txt | tail -n 1)"'
      bash scripts/spark/deploy.sh

  Before the first push the runtime clone still holds the old `pyproject.toml`
  and constraints file, so an install made then would put the old set back.

### 7.5 Scripts run by hand

As `trading`, in the runtime clone: `ssh spark-rt`, `cd forecast-model`, then
for example `.venv/bin/python scripts/run_gtm_paper.py`,
`.venv/bin/python scripts/run_ibkr_paper.py --mode approve`,
`.venv/bin/python scripts/fit_gtm_emos.py --trend --live`,
`.venv/bin/python scripts/backfill_ibkr_hourly.py --dry-run`,
`.venv/bin/python scripts/fetch_climate_forecasts.py --pilot`. The Gateway
settings come from the clone's `.env`. These scripts refuse a root that
carries the mirror marker, and from the Zenbook or as `scura` they cannot
reach the Gateway at all.

Of the three monthly rituals of `docs/ibkr_runbook.md` section 10, the
alert-channel test changes place in 7.3, and the other two here:

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
  second copy too. `scripts/scheduling/register_spark_pull_task.ps1`
  registers that task enabled, and a second run replaces the registration and
  so enables the task again. The task is disabled on purpose in two windows:
  between its registration for the cutover and the passing of the gate, and
  after a rollback. In those windows run the script only followed by
  `Disable-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull'`.
- `scripts/spark/push_snapshot.sh` refuses a marked source, so nothing is
  pushed from the Zenbook any more. Neither copy script deletes anything.
- The parity check of section 4 compares two mirrors now. Both must hold the
  same bytes: refresh both right after a sweep has finished (the recorder's
  heartbeat file, `recorder_state.json` of 7.3, shows when), then score.

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

A late check means the thing that pings it is not running. For `spark-health`
that is one of four things, and each has its own first place to look:

- The Spark itself is off or offline: it does not answer over ssh.
- The probe crashed. A probe that dies on an uncaught exception, an import failure
  included, exits 3, which is neither pinged nor a successful run, so the unit is
  `failed`: `systemctl status ibkr-health.service` shows it, and
  `journalctl -u ibkr-health.service -n 50 --no-pager` the traceback.
- The owner-rule pre-check failed. `ibkr-health.service` stops before the probe when
  `ibgw-owner-rule.service` is not active, and the Gateway's ports would then be
  unguarded: look at `ibgw-owner-rule.service` first, with
  `systemctl status ibgw-owner-rule.service`, before anything else.
- The wrapper ran but sent no ping: `HEARTBEAT_URL_HEALTH` is unset (or is not an
  https address), or the ping itself failed. The probe ran (exit 0 or 1), so the unit
  is not `failed`; `journalctl -u ibkr-health.service -n 50 --no-pager` shows
  `heartbeat: not sent` or `heartbeat: failed`.

The ping addresses are capabilities. On the Spark they live in
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
`~/quant_data_zenbook_final` (nothing is deleted there; the SQLite journal,
WAL and SHM files go with their databases, unlike in the other copy scripts:
every writer is stopped by then, so a journal that exists belongs to its
database). It ends by printing what the account holder does, in this order:
disable the units on the Spark, disable the pull task `\Spark\Spark Pull`
(left enabled it refuses the unmarked root and never pings, so `offbox-pull`
goes late within three days), check the copied book, which a stop that landed
mid-transaction can have left half written:

    python scripts/spark/restore_check.py --from ~/quant_data_zenbook_final --scratch <a fresh directory>

(it must print `RESTORE OK`; delete the scratch afterwards), put the kept root
back with `scripts/scheduling/swap_live_root_for_mirror.ps1` and its `-Undo`
switch, re-enable the four scheduled tasks, log in on the Zenbook. Only exit 0
allows that login. Exit 4 means the Spark may still hold the session (it could not be
reached, the connection to it was lost, a stop was refused, or the check found
a Gateway): do not log in on the Zenbook. The script prints
`Do NOT log in on the Zenbook` then, and also when it is interrupted (Ctrl-C,
a closed terminal, a kill) after it has first asked the Spark and before the
check has passed, because the Spark may be stopped only half way.
Exit 2 is a usage error. Exit 3 is a kept root that is missing or carries the
mirror marker: nothing was copied and the Spark was not touched, so it may
still hold the session (do not log in; `--no-copy` releases it). Exit 5 is a
copy that failed after the session was released; the message gives rsync's own
status (`copy failed (rsync exit N)`): run the script again.
`--no-copy` runs the Spark side only, for a rehearsal.

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

The ceiling bounds host memory only. CUDA allocations are not charged to a
cgroup on the Spark (2026-10-11: `docker stats` showed the vLLM container at
6.36 GiB while vLLM held about 87 GiB), so a job's GPU side is bounded by
`analysis/gpu_budget.py` instead: every torch job calls `apply_gpu_budget()`
before its first CUDA allocation, and
`tests/test_architecture/test_gpu_budget_guard.py` fails for a file that
imports torch without it. The budget comes from the launcher:

    bash scripts/spark/research_run.sh --memory 8G --gpu-budget 20G -- ~/work/wx-dl/.venv/bin/python <job>

`--gpu-budget` hands the job `RESEARCH_GPU_BUDGET_GIB`; the job may ask for
less, never for more. The cap covers PyTorch's own allocator, not the CUDA
context, cuBLAS workspaces or another framework's allocator, so leave a few
GiB of headroom. Host and GPU memory come out of the same 128 GB: the ceiling
plus the budget must fit what is free with the LLM where it is (section 2).
With Flash-Next loaded that is nothing, so GPU jobs wait until `qwenflash` is
stopped. Keep GPU jobs in the research clone: the guard sees only the repo.

The check that the ceiling does not see CUDA memory runs with no LLM loaded
and a torch venv (the research `.venv` has none):

    bash scripts/spark/research_run.sh --memory 1G -- ~/work/wx-dl/.venv/bin/python scripts/spark/cgroup_cuda_check.py --alloc-gib 2 --out ~/llm_eval/cgroup_cuda_check.json

Exit 0 with `VERDICT: cuda_outside_cgroup` confirms it. Exit 137 from the
launcher would mean the ceiling does cover CUDA memory, and this paragraph is
then wrong. Exit 1 is no verdict; exit 3 says why the machine cannot run the
check (most often: the LLM is still loaded).
