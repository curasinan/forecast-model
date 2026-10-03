# DGX Spark runbook -- research mirror

Design: `docs/plans/2026-09-30-spark-research-split-design.md`. Parity evidence:
the newest `docs/spark_parity_*.md` with its `_zenbook.json` / `_spark.json`
payloads. This page is the operator's view.

Git Bash commands on this page assume `source scripts/spark/env.sh` from the
repo root. It defines `SSH` and `SCP` (the Windows OpenSSH `ssh.exe` and
`scp.exe`, which read the `Host spark` entry of `~/.ssh/config`), `REPO_WSL`
and `RUN_DATE` (today's date).

## 1. What the Spark is, and is not

The Spark (`spark-8f47`, aarch64, 128 GB unified memory) runs the research
half of this repo and the local vLLM model. It is NOT a trading machine:

| Role | clientId | Machine |
| --- | --- | --- |
| `live_trading` | 1 | Zenbook (`scripts/run_ibkr_paper.py`, operator-run) |
| `data_collector` | 2 | Zenbook, Task Scheduler `\IBKR\IBKR Recorder` |
| `research` | 3 | Zenbook, ad hoc (not the Spark in this phase) |
| `paper_sync` | 4 | Zenbook, Task Scheduler `\IBKR\IBKR Paper Sync` |

The Spark holds no client id, and two independent guards keep it from
trading even by accident:

- The Gateway's API is bound to localhost on the Zenbook, so there is nothing
  on the LAN to connect to.
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
  other command here, from Git Bash and the Bash tool alike. `git push` to
  GitHub remains the separate, normal step. The checkout followed
  `spark-research-split` while that branch was in flight; after its merge,
  switch it once: `git push spark main`, then
  `"$SSH" spark 'cd ~/work/forecast-model && git checkout main'`. From then
  on every `git push spark main` updates `main` in place.
- venv: `.venv` (uv-managed Python 3.14.2, pinned by the newest
  `constraints/zenbook-*.txt`). To rebuild it, on the Spark:
  `~/.local/bin/uv venv .venv --python 3.14.2`, then
  `~/.local/bin/uv pip install -e ".[dev,ibkr,climate,mcp,web,trading]" -c constraints/zenbook-2026-10-03.txt`
  (swap in a newer constraints file when one exists; `~/.local/bin` is not
  on the PATH of a non-interactive ssh command).
- Data: `~/quant_data` -- a MIRROR pushed from the Zenbook (section 3). Same
  default root as the Zenbook on purpose; no `IBKR_DATA_ROOT` /
  `CLIMATE_DATA_ROOT` overrides anywhere.
- `.env`: exactly one line, `LLM_MODEL=Qwen3.8-Flash-Next-NVFP4` (mode 600).
  No API keys.
- LLM stack: `~/stack/docker-compose.yml` (repo `curasinan/spark-llm-stack`);
  vLLM on `127.0.0.1:8000`, Open WebUI 8080, Portainer 9000, Phoenix 6006/4317,
  all loopback-only behind ufw (22/tcp is the only open port). Images are
  pinned by digest: never `docker compose pull`, never edit the compose file
  without the operator. NVIDIA driver packages are on `apt-mark hold`: no
  `apt upgrade`.
- Memory: with vLLM loaded 22-24 GiB is available and the realistic budget
  for forecast work is about 15 GiB. Read it with `free -h` (`docker stats`
  does not show vLLM's CUDA memory). For anything larger, finish the LLM
  batch first, then `docker compose stop qwenflash` (from `~/stack`).
- Tests, from the Zenbook:
  `"$SSH" spark 'cd ~/work/forecast-model && HOME=$(mktemp -d) .venv/bin/python -m pytest -q'`.
  The throwaway `HOME` matters: parts of the suite write under
  `~/quant_data/polymarket_weather` of whatever home they see, which on the
  Spark is the mirror root (the Zenbook runs use a throwaway `USERPROFILE` /
  `HOME` for the same reason; parity report, section 1).

## 3. Pushing data (Zenbook -> Spark, never back)

On the Zenbook, outside 17:25-17:40 local (the paper-sync task writes the
SQLite DB then). PowerShell:

    wsl -d Ubuntu -- bash /mnt/c/Users/asus/OneDrive/Desktop/new_work/scripts/spark/push_snapshot.sh

Git Bash (MSYS would rewrite a bare `/mnt/...` argument into a Windows path;
`env.sh` defines `REPO_WSL` with the `//mnt` prefix that survives):

    source scripts/spark/env.sh
    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh"

`env.sh` pins `REPO_WSL` to the one checkout it was written in (the
`spark-research-split` worktree); from any other checkout, reassign it right
after sourcing, in the same form, for example
`REPO_WSL=//mnt/c/Users/asus/OneDrive/Desktop/new_work` for the main
checkout. Both forms take `--include-raw`, and `--help` prints the script's
usage header. The scripts under `scripts/spark/*.sh` must stay LF
(`.gitattributes` enforces it on checkout): an editor that saves CRLF breaks
them under WSL's bash with `$'\r': command not found`.

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
   sub-second parquet write copies a torn sweep: just run it again.
4. Print the Spark's snapshot identity and save it as
   `~/quant_data_snapshot/identity_spark.json` (written only after a
   successful verify, so a failed one never leaves an empty identity).

To prove a push left the live root alone, bracket it with
`live_manifest.py` (read-only; it is the one tool here meant for the live
root) and `diff` the two files (parity report, section 3):

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
`paper/gtm_signals.json` are produced on the Zenbook only (`fit_gtm_emos.py`
and `run_gtm_paper.py` there); on the Spark, scoring is `parity_score.py`,
which writes nothing under `paper/`.

## 4. Parity check (repeat after any numerical-stack change on either side)

    # Zenbook, Git Bash, from the repo root, with the work committed.
    # Both machines must run the same commit: main, or the branch both are on.
    source scripts/spark/env.sh
    git push spark main
    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh"
    python scripts/spark/parity_score.py --root ~/quant_data_snapshot --out ~/quant_data_snapshot/parity/zenbook.json
    "$SSH" spark 'cd ~/work/forecast-model && .venv/bin/python scripts/spark/parity_score.py --root ~/quant_data --out ~/parity_spark.json'
    "$SCP" spark:parity_spark.json ~/quant_data_snapshot/parity/spark.json
    python scripts/spark/compare_json.py ~/quant_data_snapshot/parity/zenbook.json ~/quant_data_snapshot/parity/spark.json --ignore identity.machine --rtol 1e-8 --atol 1e-10 --min-numeric 1000

`PARITY OK` at the registered tolerance is the gate (2026-10-03: 1536
numeric leaves, max relative difference 3.8e-10); the `identity.snapshot`
block, which is not ignored, proves both machines scored the same bytes at
the same commit. `--min-numeric 1000` is a floor on the numeric leaves
compared (the real payload has about 1500): an accidentally empty signals
list must not pass as parity. The compare ignores `identity.machine`,
`git_dirty` included, so look at it yourself: both payloads should say
`"git_dirty": false` (`grep git_dirty` on the two files), because a dirty
tracked tree means the code that scored is not the commit the identity
names. The tolerance is not 1e-15 because the frame's ERA5->NOAA bridge is
refit with LAPACK on each machine and reduction order differs by CPU kernel
(measured x86 kernel-to-kernel noise: 3e-10 relative); 1e-8 is still six
orders below the smallest price step that matters.

`verify_mirror.py` and `parity_score.py` are for mirrors and frozen roots.
On the Zenbook score `~/quant_data_snapshot`, which the push script has just
frozen, never the live `~/quant_data`: the scorer reads and hashes its inputs
at different moments, so on a root that moves (the live one, whenever the
recorder writes) the identity can describe bytes it did not score. It raises
if a newer sweep lands between its hashing and its scoring, but that is the
one movement it can see. It also refuses an identity whose code SHA is
unknown, which is what a checkout without git gives, so score from a git
checkout on both machines.

A refit comparison is report-only. Run `scripts/fit_gtm_emos.py --trend` with
`--root ~/quant_data_snapshot/climate_data` on the Zenbook (always pass the
snapshot root: without `--root` the fit rewrites the live root's operational
`models/gtm_emos.json`) and with `--root ~/quant_data/climate_data` on the
Spark, then compare the two `models/gtm_emos.json` files with
`compare_json.py --rtol 1e-6 --atol 1e-9 --ignore fingerprint` (the two
`reports/gtm_emos_diagnostics.json` files also need `fitted_at_utc` and
`artifact_paths` ignored). The optimiser amplifies the same kernel
differences, and fresh artifacts carry a `fingerprint` string that changes
with any last bit: on 2026-10-03 one coefficient of 50 differed by 1.2e-6
relative and the compare printed `PARITY FAILED`; the design asks for an
investigation only above 1e-4. A refit overwrites the model and diagnostics
under the root it ran on, so push again before the next scoring parity run.

## 5. Using the local model

- From the Zenbook: open the tunnel with `ssh -L 8000:127.0.0.1:8000 spark`
  (Git Bash: `"$SSH" -L 8000:127.0.0.1:8000 spark`), then the default
  `LLM_BASE_URL=http://127.0.0.1:8000/v1` works unchanged. `LLM_MODEL` is
  required wherever the client runs (environment variable or that checkout's
  `.env`). The Spark's `.env` holds only that one line; `.env.example`
  documents every `LLM_` setting, `LLM_CONNECT_TIMEOUT_S` and
  `LLM_READ_TIMEOUT_S` included.
- The client only ever talks to that loopback tunnel. It ignores proxy
  environment variables (`trust_env=False`) and therefore also
  `SSL_CERT_FILE` / `SSL_CERT_DIR`, so an `https://` base URL with a private
  CA is unsupported by design. The test that pins this
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
  `restart: always`). It is ready when
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
  `LLM_LIVE=1`:

      "$SSH" spark 'cd ~/work/forecast-model && .venv/bin/python scripts/llm_smoke.py'
      "$SSH" spark 'cd ~/work/forecast-model && LLM_LIVE=1 .venv/bin/python -m pytest tests/test_llm/test_live.py -q'

- Boundary: `src.llm` is never imported under `climate/`, `src/trading/`,
  `src/venues/`, `src/edge/`, `src/domain/`, `src/math/`, `src/models/`,
  `monitoring/`, nor in `scripts/run_ibkr_paper.py`,
  `scripts/sync_ibkr_paper.py`, `scripts/record_ibkr_ticks.py` or
  `scripts/run_gtm_paper.py` (`tests/test_architecture/test_llm_boundary.py`;
  a literal `REQUIRED_SCOPE` list in that file pins the set, so narrowing it
  takes an edit to the list). `src/collectors/` is deliberately outside: it
  is where text-to-data processing may live. The scan reads imports only: it
  cannot see transitive imports, `importlib` / string-named imports or
  `from src import *`. The LLM turns text into data; the numeric model
  prices.

## 6. When the network changes

The Spark's LAN address (192.168.1.31) is a DHCP reservation on its Wi-Fi
MAC; switching to Ethernet needs a second reservation. The move to the US
changes the address entirely: mDNS (`spark-8f47.local`) or Tailscale keep
`ssh spark` working. Everything on this page reaches the Spark through the
`Host spark` entry of the Zenbook's `~/.ssh/config` (`ssh`, `git push spark`,
the data push, the tunnel), so a new address is an edit to that one entry.
The Gateway's API port is never opened to the LAN: a Spark-side `research`
role, if it ever comes, goes over Tailscale first (operator decision
2026-10-02).
