# Spark research split (Option B) -- design

**Date:** 2026-09-30. **Status:** approved by the account holder 2026-10-01.
**Plan:** `docs/plans/2026-10-01-spark-research-split.md`.
Dated artifact names below (`constraints/zenbook-<date>.txt`,
`docs/spark_parity_<date>.md`) take the date of the run that produces them.

## 0. One-paragraph goal

Run the research half of this repo on the DGX Spark (`spark-8f47`, aarch64,
128 GB) against a one-way mirror of the Zenbook's data, reach the Spark's
local vLLM model through a config-driven client, and prove parity with
evidence -- without touching the trading runtime, the IB Gateway, or the
paper book. The Zenbook stays the only machine that talks to the Gateway.

Why this split (assessment of 2026-09-29): the repo is a 24/7 trading
runtime (recorder, paper sync, health probe, climate labels, Telegram, and
the paper book that is the go-live evidence) plus a research codebase.
Nothing in the live climate lane calls an LLM; no dependency needs a GPU;
every frozen package version resolves as a cp314 aarch64 wheel (verified
2026-09-30 with a pip dry-run: cdsapi 0.7.7, ib_async 2.1.0, lightgbm 4.6.0,
netCDF4 1.7.4, numpy 2.4.2, pandas 3.0.1, pyarrow 25.0.0,
pydantic_core 2.41.5, scikit-learn 1.8.0, scipy 1.17.1, xarray 2026.7.0,
xgboost 3.2.0).

## 1. Non-goals (explicit)

- No scheduled task, recorder, paper sync, health probe, or label collector
  on the Spark. No Gateway access from the Spark: no `IBKR_PORT`, no
  Trusted-IP change, no Windows firewall change. (Phase 2 if ever; own spec.)
- No LLM anywhere in the pricing or ordering path. No predictor changes.
- The synthetic-question pilot's methodology is a separate spec. This work
  delivers only the client and a smoke check. (First pilot task will be
  establishing the model's knowledge cutoff, because the leakage filter
  must key on that, not on the document date.)
- No change to the Zenbook's Python, venv, or Task Scheduler launchers.
- No CDS downloads on the Spark in this phase (`~/.cdsapirc` is not copied).
- No data ever flows Spark -> Zenbook. Only git (code, docs, reports).

## 2. Machine roles and invariants

| Role | clientId | Runs on | On the Spark |
| --- | --- | --- | --- |
| `live_trading` | 1 | Zenbook, `scripts/run_ibkr_paper.py` (operator) | never |
| `data_collector` | 2 | Zenbook, Task Scheduler `\IBKR\IBKR Recorder` | never |
| `research` | 3 | Zenbook, ad hoc | not in this phase |
| `paper_sync` | 4 | Zenbook, Task Scheduler `\IBKR\IBKR Paper Sync` | never |

Invariants, each with the mechanism that enforces it:

1. **The Spark cannot reach the Gateway.** Its `.env` and shell carry no
   `IBKR_PORT` / `IBKR_TRADING_MODE`, so every Gateway-touching entrypoint
   fails at startup with the existing pydantic `ValidationError` (loud-failure
   rule, migration spec Stage 7). Independently, the Gateway API stays bound
   to localhost on the Zenbook, so even a misconfigured Spark has nothing to
   connect to. Two guards, both already in place; this design adds none and
   removes none.
2. **Data flows one way.** `scripts/spark/push_snapshot.sh` copies
   Zenbook -> Spark. Nothing copies back. Spark-side outputs (refits, parity
   payloads, LLM experiments) are ephemeral; the next push re-overwrites any
   canonical file they touched. The operational `models/gtm_emos.json` and
   `paper/gtm_signals.json` are produced on the Zenbook only.
3. **Same default data root on both machines: `~/quant_data`.** No
   `IBKR_DATA_ROOT` / `CLIMATE_DATA_ROOT` overrides on the Spark. The draft's
   `/opt/data` is dropped: an env-var root must be set identically in every
   context (unit, shell, cron), which is the "two processes disagree about a
   file" failure class this repo already paid for on 2026-08-25. The Spark's
   home is on the same 4 TB NVMe; a symlink `~/quant_data -> /opt/...` is
   allowed if wanted, an env var is not.
4. **No secrets on the Spark.** Its `.env` holds exactly `LLM_MODEL=...`.
   No Anthropic/NewsAPI/GitHub keys (the research half does not need them;
   verified: the GTM signal path builds `FlatCommissionFeeModel(0.01)` with
   no `verified_on`, so it needs neither the fee pair nor a date -- the
   monthly fee ritual stays a one-file ritual on the Zenbook).
5. **Scripts that must never run on the Spark** (documented in the runbook,
   guarded by invariant 1 or by a missing credential): `record_ibkr_ticks.py`,
   `sync_ibkr_paper.py`, `run_ibkr_paper.py`, `backfill_ibkr_hourly.py`,
   `verify_ibkr_entitlements.py`, `collect_climate_labels.py` (the ledger is
   canonical on the Zenbook; a Spark run would fork it),
   `fetch_climate_forecasts.py` (no `.cdsapirc`).

## 3. Spark environment

- **Clone:** `~/work/forecast-model`, `origin` =
  `github.com/curasinan/forecast-model`. During this work the Spark
  clone tracks the feature branch (the parity scripts must exist on both
  machines at the same commit; the report records that SHA); after the
  merge it tracks `main`. Uses the GitHub credential the Spark already has
  for `spark-llm-stack` (assumption A1).
  Line endings: the Zenbook has `core.autocrlf=true`, so the index is LF and
  a Linux clone checks out LF; no conversion needed.
- **Python:** `uv` on the Spark only (`uv python install 3.14`; the Zenbook
  runs 3.14.2 from `C:\Python314`, untouched). `.venv` inside the clone.
- **Install:** `uv pip install -e ".[dev,ibkr,climate,mcp,web]"
  -c constraints/zenbook-2026-09-30.txt`. The extras mirror what the
  Zenbook actually has installed (fastapi/uvicorn/jinja2/mcp present;
  py-clob-client absent), so test collection matches.
- **Constraints file:** `constraints/zenbook-<date>.txt` = `pip freeze`
  of the Zenbook's global 3.14 at parity time (editable and `@ file://`
  entries filtered out), committed as evidence of the environment that
  produced the parity numbers. Constraints only bind
  packages that get installed, so Windows-only entries are inert. Any pin
  that cannot be satisfied on aarch64 is relaxed and recorded in the parity
  report as a deviation (expected: none, see section 0).
- **Evidence captured:** `uname -m`, `python -V`, the install resolution
  summary, `pytest -q` and `ruff check .` results -> parity report.

## 4. Data mirror: `scripts/spark/push_snapshot.sh`

Bash, LF-only, run inside WSL Ubuntu on the Zenbook (WSL has rsync; Git
Bash does not). Invocation from PowerShell:
`wsl -d Ubuntu -- bash /mnt/c/Users/asus/OneDrive/Desktop/new_work/scripts/spark/push_snapshot.sh [--include-raw]`

Steps, in order, each failing loudly:

1. **Local freeze.** `rsync -rlt --delete --exclude climate_data/raw/`
   from `/mnt/c/Users/asus/quant_data/` to
   `/mnt/c/Users/asus/quant_data_snapshot/` (outside OneDrive). `-rlt`, not
   `-a`: both sides are NTFS through WSL's drvfs, where preserving
   owner/group/perms only produces warnings. This frozen copy is what the
   Zenbook-side parity run reads, so both machines score identical bytes
   while the live roots keep moving (the recorder writes every 20 min).
2. **Push.** `rsync -rlt --info=stats1 --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r
   --exclude climate_data/raw/` (plus `climate_data/raw/` when
   `--include-raw`) from the snapshot to `spark:quant_data/`. The explicit
   `--chmod` stops drvfs's 777 mode bits from landing on the Spark.
   **No `--delete`**: Spark-side ephemeral outputs may coexist; canonical
   files are overwritten whenever they differ.
   Transport: the Windows `ssh.exe` via `-e /mnt/c/Windows/System32/OpenSSH/ssh.exe`
   so the Windows agent and the `spark` alias are reused (Task 0 spike;
   fallback: a WSL-native key added to the Spark's `authorized_keys`).
3. **Remote verify.** `ssh spark '~/work/forecast-model/.venv/bin/python
   scripts/spark/verify_mirror.py'` opens the newest sweep parquet's
   metadata, the labels ledger, the hourly-bars tree's newest file, and runs
   `PRAGMA quick_check` on `ibkr_ticks/paper/ibkr_paper.db`; exit 1 lists
   every unreadable file (never a silent skip).
4. **Identity.** Prints and saves the snapshot identity (section 5.1).

Known hazards, handled: the recorder's sweep parquet write is **not
atomic** (`pq.write_table` straight to the final path, unlike
`climate/storage.py`), so a push landing inside that sub-second window
copies a torn file -- step 3 catches it, re-push fixes it. Follow-up chip,
not this plan: make the recorder write tmp-then-`os.replace` (touching the
recorder means restarting the task, orphan trap and all). Avoid pushing at
17:30 +/- 5 min (paper sync writes the SQLite DB); `quick_check` catches a
torn copy anyway.

Repo change required: `.gitattributes` gets `scripts/spark/*.sh text eol=lf`
so the Windows working tree (CRLF under autocrlf) does not hand WSL's bash a
script full of carriage returns.

## 5. Parity protocol (evidence, not eyeballing)

The recorder was DOWN at the 2026-09-30 health-probe run (probe output:
"unexpected disconnect with no reconnect since, down for 1160 min"; paper
sync 52.6 h stale; newest sweep `date=2026-09-28/quotes-220822Z.parquet`).
`scripts/run_gtm_paper.py` therefore refuses to write signals (staleness
gate H2) -- correctly. Parity must not depend on a fresh sweep, so it uses a
dedicated research-only scorer that calls the same library code and never
writes the operational files.

**Code layout.** `scripts/` is not a package, so the logic lives in
`analysis/spark_parity.py` (the repo's research-tooling package:
`check_parquet`, `check_sqlite`, `snapshot_identity`, `score_snapshot`,
`compare_json`), and the three scripts under `scripts/spark/` are thin
argparse CLIs over it. Tests import the module, never the scripts.

### 5.1 Snapshot identity

`verify_mirror.py --identity` returns
`{newest_sweep, sweep_count, labels_rows, labels_max_date,
model_fingerprint (climate.fit.model_fingerprint of models/gtm_emos.json),
paper_db_quick_check}`. Both parity payloads embed the identity they
computed from *their own* files; the comparison asserts these match. That
is the independent witness that both machines scored the same bytes
(bug-ledger lesson: a check written by the same process it certifies
proves nothing).

### 5.2 `scripts/spark/parity_score.py`

`--root R/climate_data --ticks-root R/ibkr_ticks --model R/climate_data/models/gtm_emos.json --out X.json`
(R = `~/quant_data_snapshot` on the Zenbook, `~/quant_data` on the Spark).
Loads the frame with `climate.dataset.build_training_frame`, the model with
`climate.emos.EmosModel` exactly as the entrypoint does, then calls
`climate.paper.run_paper(frame, model, ticks_root=..., universe_json=<repo
data/ibkr_universe_live.json>, paper_days=0, resolved_months=0,
require_live_paper=False)`. Drops `ts_utc` (wall clock), adds
`identity: {snapshot: <5.1>, machine: {platform.machine, python, numpy,
scipy, pandas, pyarrow versions}}`, writes `--out`. It never writes
`paper/gtm_signals.json`, never appends `gtm_signals_history.jsonl`, and
applies no staleness gate -- the docstring says so and says why (a parity
probe, not an operator entrypoint; the gate belongs to the entrypoint).

### 5.3 `scripts/spark/compare_json.py`

`A B --rtol 1e-9 --atol 1e-12 --ignore identity.machine` -> exit 0 on parity.
Recursive structural compare; floats within tolerance; NaN == NaN; None vs
NaN is a mismatch; ints/strings/bools exact; list length and dict key sets
exact. Prints every structural mismatch and, per numeric leaf, the max
absolute and relative difference. Unit-tested (identical, tolerance edge,
NaN pair, None-vs-NaN, missing key, extra key, ignore path).

Tolerance rationale: the scorer is elementwise Gaussian CDF and arithmetic
on the same doubles, so agreement should be exact or last-ulp; 1e-9
relative catches any real divergence (e.g. a different pandas ordering)
while ignoring rounding.

### 5.4 Refit parity (report-only)

`scripts/fit_gtm_emos.py --trend --root R` on both machines, **after** 5.2
(it overwrites R's `models/gtm_emos.json`). Compare the two model files with
`--rtol 1e-6`; record max differences. Report-only, not a gate: the fit goes
through scipy's optimiser, where x86 AVX and aarch64 NEON reduce sums in
different orders, so coefficient differences around 1e-8..1e-6 are floating
point, not bugs. A difference above 1e-4 is investigated before the report
is signed off.

### 5.5 Suite and lint

`python -m pytest -q` and `python -m ruff check .` on the Spark, compared
with a same-day Zenbook run (counts recorded from that run, not from
memory). The Spark run doubles as a check that the suite does not depend on
the operator's `.env` (the Spark's has only `LLM_MODEL`).

### 5.6 Report

`docs/spark_parity_2026-09-30.md`: identity, the commit SHA the scripts
ran at on both machines, both environments, install deviations (expected
none), suite/lint counts, `compare_json` output verbatim for 5.3 and 5.4,
verdict. The Zenbook copy is the runtime and is
never deleted under Option B, so this report is what "parity verified"
means in the draft, nothing more.

## 6. Local LLM client: `src/llm/`

Role decision encoded in the module docstring: the LLM is a data
processor (text -> structured), never the predictor.

- **`src/llm/settings.py`** -- `LocalLLMSettings(BaseSettings)`,
  `env_prefix="LLM_"`, `env_file=".env"`, `extra="ignore"` (same pattern as
  `IBKRSettings`). Fields: `model: str` (**required, no default** -- the
  model name is never hardcoded), `base_url = "http://127.0.0.1:8000/v1"`,
  `api_key = "EMPTY"` (vLLM ignores it), `max_tokens = 8192`,
  `reasoning_effort: Literal["low","medium","high"] = "low"`,
  `temperature: float | None = None` (omitted from the request when None),
  `connect_timeout_s = 10.0`, `read_timeout_s = 900.0` (8192 tokens at
  ~15 tok/s is ~520 s; queueing on top).
- **`src/llm/client.py`** -- `LocalLLMClient(settings, transport=None)` on
  `httpx` (already a core dependency; no new package):
  - `models() -> list[str]` (GET `/models`).
  - `assert_model_served()` -> raises `LLMConfigError` listing the served
    ids when `settings.model` is not among them. Config-over-hardcode made
    checkable.
  - `chat(messages, *, json_schema=None, schema_name="reply",
    max_tokens=None) -> LLMReply`. Sends `model`, `messages`, `max_tokens`,
    `reasoning_effort`, optional `temperature`, and when a schema is given
    `response_format={"type":"json_schema","json_schema":{"name":...,"schema":...}}`.
  - `LLMReply`: `content`, `reasoning` (from `reasoning_content`, may be
    None), `parsed` (JSON-decoded content when a schema was given),
    `usage` (prompt/completion tokens, reasoning tokens when reported),
    `latency_s`, `model` (as reported by the server).
  - Errors: `LLMError` base; `LLMHTTPError` (status + body excerpt);
    `LLMOutputError` (schema given, content not valid JSON; carries the raw
    text); `LLMConfigError`. No retries (callers decide). No streaming in
    this phase.
- **`scripts/llm_smoke.py`** -- operator check: `assert_model_served()`,
  one schema-constrained extraction on a fixed toy paragraph, prints served
  models, latency, tokens/s, reasoning tokens; non-zero exit on any failure.
  Run on the Spark, or on the Zenbook through
  `ssh -L 8000:127.0.0.1:8000 spark`.
- **Tests** `tests/test_llm/`: settings (missing `LLM_MODEL` ->
  `ValidationError`; prefix and defaults), client via `httpx.MockTransport`
  (request body fields; `response_format` only with a schema; parses
  content/reasoning/usage; invalid JSON -> `LLMOutputError` with raw;
  HTTP 500 -> `LLMHTTPError`; served-model mismatch -> `LLMConfigError`).
  `tests/test_llm/test_live.py` skipped unless `LLM_LIVE=1`.
- **Boundary test** `tests/test_architecture/test_llm_boundary.py`, same
  AST style as `test_import_bans.py`: no `src.llm` import under `climate/`,
  `src/trading/`, `src/venues/`, `src/edge/`, `src/domain/`, `monitoring/`,
  nor in `scripts/run_ibkr_paper.py`, `scripts/sync_ibkr_paper.py`,
  `scripts/record_ibkr_ticks.py`, `scripts/run_gtm_paper.py`. This is the
  "LLM is not the predictor" decision as a failing test, not a sentence in
  a doc.
- **`.env.example`**: an `LLM_*` block with the request rules from the
  Spark handover (max_tokens >= 8192, reasoning_effort low, cold start
  11-14 min, sidecar warm-up command).

## 7. Documentation and memory

- `docs/spark_runbook.md`: layout on the Spark, connecting (`ssh spark`,
  the tunnel), `push_snapshot.sh` usage and timing rule, the never-on-Spark
  list, the role/machine table, LLM request rules and warm-up, the parity
  procedure, the US-move note (IP changes; Tailscale keeps addresses stable;
  if the Gateway ever goes on the LAN, do it on Tailscale addresses from day
  one).
- `docs/ibkr_runbook.md` role table: a "machine" column (all Zenbook).
- `docs/MIGRATION_CHECKPOINT.md`: a short "Spark research mirror" pointer.
- Memory `dgx-spark.md`: outcome and the push/parity commands.

## 8. Testing strategy (TDD where there is logic)

| Unit | Test |
| --- | --- |
| `analysis/spark_parity.compare_json` | unit tests, section 5.3 |
| `analysis/spark_parity.check_parquet` / `check_sqlite` / `snapshot_identity` | unit tests with tmp files: valid, truncated parquet, truncated sqlite |
| `analysis/spark_parity.score_snapshot` | unit test on a synthetic frame/model/sweep fixture: `ts_utc` absent, identity blocks present, never touches `paper/` |
| `scripts/spark/*.py` CLIs | one smoke test each via `main(argv)` on the fixtures above (exit codes) |
| `src/llm/*` | section 6 |
| `test_llm_boundary.py` | self-test: a temp file importing `src.llm` under a banned dir is caught |
| `push_snapshot.sh` | `bash -n`, Task 0 spike, then the real parity push (evidence in the report) |

Bug-ledger items addressed: manual-eyeball-verification (compare_json exit
codes and recorded counts are the assertions); nan-not-json-serialisable
(payloads may hold NaN; compare is NaN-aware, scorer keeps Python's JSON
round-trip); permanently-skipped-sweep (verify_mirror lists every failure,
never skips); self-witnessed-preregistration (identity computed
independently on each machine and asserted equal).

## 9. Sequence (for the plan)

0. Spike: `ssh spark uname -m`; WSL `rsync -e ssh.exe --dry-run` to the
   Spark; GitHub clone credential on the Spark. Decides transport and who
   runs Spark-side commands (Claude over `ssh spark` if the sandbox allows,
   else copy-paste blocks for the operator).
1. Constraints file + Spark clone/venv/install + suite and lint on the Spark.
2. `.gitattributes`, `analysis/spark_parity.py` mirror checks + identity
   (TDD), `verify_mirror.py` CLI, `push_snapshot.sh`.
3. `compare_json` + `score_snapshot` (TDD) and their CLIs.
4. Parity run (5.1-5.5) + report (5.6).
5. `src/llm` settings + client (TDD), boundary test, `.env.example`.
6. `llm_smoke.py`, live run on the Spark, `test_live.py`.
7. Runbook, role table, checkpoint pointer, memory. Full suite and lint on
   both machines at the final commit. Commit, PR.

## 10. Assumptions to confirm

- A1. The Spark's existing GitHub credential may be used to clone
  `forecast-model`.
- A2. `uv` on the Spark only; the Zenbook's global Python stays as is.
- A3. `~/quant_data` on the Spark; `/opt/data` dropped.
- A4. The `src.llm` boundary test is wanted (encodes the role decision).
- A5. Pilot methodology is a separate spec; this work stops at the smoke check.
- A6. Spark-side steps are run by Claude over `ssh spark` when possible.

## 11. Amendments after plan review (2026-10-01)

The plan critic assembled the plan's code into a scratch overlay, ran its
tests and mutations, rehearsed the push script and measured the parity
noise on a copy of the real snapshot. What that changed in this design:

- **R1. The 5.3 premise was wrong.** `build_training_frame` refits the
  ERA5->NOAA bridge with `np.linalg.lstsq` on each machine
  (`climate/bridge.py:142`), so agreement is at LAPACK's kernel-noise
  level, not last-ulp: identical runs agree exactly, but x86 kernel
  variants (Haswell vs Sandybridge/Nehalem) differ by max rel 3.3e-10 and
  a different solver by 1.0e-9, against the spec's rtol 1e-9. The gate is
  now a REGISTERED tolerance, measured on the Zenbook by a kernel-variation
  experiment before the cross-machine run (plan Task 6 Step 0; expected
  `--rtol 1e-8 --atol 1e-10`, about 30x above the measured noise and six
  orders below the smallest price step that matters), and the payload
  carries the bridge coefficients so a failure can be localised. The
  module defaults (1e-9 / 1e-12) stay for unit tests; the gate passes the
  registered values explicitly.
- **R2. Refit compare.** Fresh artifacts from `climate.fit.write_artifacts`
  carry a `fingerprint` key that changes with any last bit, so the
  report-only refit compares ignore `fingerprint` too (5.4).
- **R3. The probe cannot pose as a verdict.** `score_snapshot` keeps
  `require_live_paper=True` (so `go_live.passed` is False by construction
  on both machines) and adds a top-level `probe: "spark_parity"` marker;
  5.2's "require_live_paper=False" is withdrawn.
- **R4. Identity is stronger and tested.** Besides the newest-sweep and
  ledger hashes, the compared block carries a sha256 for every scoring
  input (SEAS5, ERA5, labels, model, diagnostics, contracts cache) and the
  git SHA the code ran at; tests assert the hashes against `hashlib`,
  that a byte flip changes them, and that the newest of several sweeps
  and hourly files is the one checked. `verify_mirror` requires the SEAS5
  and ERA5 products.
- **R5. The live root is proven untouched, not assumed.** A manifest
  (sha256, size, mtime) of the paper book, ledgers, signals, model and
  diagnostics is taken before and after every push and the parity run and
  diffed. The freeze skips the recorder's `state/`, `*.tmp` and SQLite
  journals, retries once on rsync's "vanished file" exit, and never
  deletes the evidence directory inside the snapshot.
- **R6. Evidence location.** Parity payloads from the Spark are copied to
  `~/quant_data_snapshot/parity/` (never into `~/quant_data`) and the two
  compared payloads are committed next to the report. Section 1's "only
  git flows back" is read as: no DATA ever flows back; evidence files do,
  into the snapshot's evidence directory and then into git.
- **R7. Lint gate.** The repo's ruff baseline is not clean (112 findings in
  legacy code as of 2026-10-01); "ruff clean" in 5.5 becomes: zero findings
  in files the branch touches, and the same total on both machines.
- **R8. Tooling facts.** Git Bash rewrites bare `/mnt/...` arguments before
  `wsl.exe` sees them (the `//mnt` form is used); WSL's default user is
  root; shell state does not survive between the assistant's Bash calls,
  so every block sources `scripts/spark/env.sh`; `.gitattributes` is
  created first (Task 0) so no shell file is ever checked out CRLF; the
  suite writes under `~/quant_data/polymarket_weather`, so Spark suite runs
  use an isolated HOME.
- **R9. CLI shape deviations from 5.2/5.3, deliberate:** `parity_score.py`
  takes one `--root` (the quant-data root) plus `--out`, not three paths;
  `compare_json.py` prints aggregate max diffs plus every mismatch rather
  than every leaf, and refuses to pass when fewer than `--min-numeric`
  leaves were compared; `verify_mirror.py` always prints the identity on
  success (no `--identity` flag).
- **R10. Empty LLM env values** (`LLM_TEMPERATURE=`) are treated as unset
  (`env_ignore_empty`), matching `.env.example`; the LLM tests clear
  `LLM_*` from the process environment.
- **R11. No tool attribution; repository renamed (2026-10-02).** By
  account-holder decision commits carry no Co-Authored-By trailer or
  generated-with footer, and the history was rewritten once to remove the
  existing ones; the plan's commit commands follow suit. The GitHub
  repository is now `curasinan/forecast-model` (private). The same rewrite
  replaced the two real IBKR account identifiers with placeholders
  (`U0000000` live, `DU0000000` paper); the real values live only in the
  untracked `docs/local/` and would reach the Spark over ssh, never through
  git. `docs/history_rewrite_2026-10-02_sha_map.txt` maps old hashes to new.
- **R12. Spark status update (2026-10-02, supersedes the handover note).**
  The served model name is `Qwen3.8-Flash-Next-NVFP4`; readiness is
  `docker inspect -f '{{.State.Health.Status}}' qwenflash` = `healthy`;
  `ple-warm.service` warms the sidecar before docker and every service has
  `restart: always`, so nothing is warmed or started by hand and the cold
  start is about 10 minutes. The compose images are pinned by digest: no
  `docker compose pull`, no edits to the compose file without the operator;
  NVIDIA driver packages are on `apt-mark hold`, so no `apt upgrade`. With
  vLLM loaded about 24 GiB is available and about 15 GiB is the realistic
  budget for forecast work (`free -h`, not `docker stats`). The Gateway's
  API port is never opened to the LAN; any future Spark-side Gateway access
  goes over Tailscale first, which replaces the LAN-opening steps this
  design mentions. The Spark `.env` keeps its single `LLM_MODEL` line
  (invariant 4) with mode 600; the status note's "copy .env by scp" is read
  as that one-line file unless the account holder decides the keys should
  travel too.
- **R13. The boundary test guards two more directories (2026-10-03).**
  Section 6's list of guarded paths is superseded. Ruling R-Q extended the
  `src.llm` boundary test to `src/math` and `src/models` (the sizing inputs
  the guarded modules import: EV and Kelly, `EdgeSignal`), so the guarded
  set is `climate/`, `src/trading/`, `src/venues/`, `src/edge/`,
  `src/domain/`, `src/math/`, `src/models/` and `monitoring/`, plus
  `scripts/run_ibkr_paper.py`, `scripts/sync_ibkr_paper.py`,
  `scripts/record_ibkr_ticks.py` and `scripts/run_gtm_paper.py`. A literal
  `REQUIRED_SCOPE` list in `tests/test_architecture/test_llm_boundary.py`
  pins that set: widening the configuration needs no edit there, narrowing
  it does, on purpose. `src/collectors` stays outside (ruling R-R):
  collectors are where text-to-data processing may legitimately live, which
  is the LLM's assigned role. The cost of that carve-out: two guarded
  modules, `src/venues/polymarket.py` and `src/edge/market_selector.py`,
  import `src/collectors/polymarket.py`, so an `src.llm` import added there
  would reach guarded code by a path the scan cannot see.
- **R14. Real-server facts from the live run (2026-10-03).** Section 6's
  `LLMReply.reasoning` "(from `reasoning_content`, may be None)" was wrong
  for the Spark: its vLLM returns the reasoning text under
  `message.reasoning`, so the field was None against the real server while
  every unit test passed on a hand-drawn fixture. The client now reads
  `reasoning_content` with a fallback to `reasoning`, and its unit tests
  include byte copies of two captured replies (a schema-constrained
  completion and the `/v1/models` listing) under `tests/test_llm/fixtures/`,
  driven through `tests/test_llm/test_real_shapes.py`. The rule: a fixture
  that stands for the real server's reply is a capture, never hand-drawn,
  and is replaced only by a new capture, never edited by hand (tests of
  malformed replies build their own bodies by necessity).
- **R15. Invariant 1's mechanism, corrected (2026-10-03).** Section 2's
  invariant 1 says every Gateway-touching entrypoint fails at startup with
  the pydantic `ValidationError`; that is true only of the entrypoints that
  build `IBKRSettings`: `record_ibkr_ticks.py`, `run_ibkr_paper.py`,
  `sync_ibkr_paper.py`, and `backfill_ibkr_hourly.py` unless it is given both
  `--port` and `--mode`. `verify_ibkr_entitlements.py` and
  `probe_ibkr_history.py` build no settings: they dial `127.0.0.1` directly
  (the first by default) and simply fail to connect on the Spark. The
  mechanism is therefore two facts, the Spark has no `IBKR_*` environment and
  the Gateway API cannot be reached from the LAN, plus the never-run
  list in the runbook (`docs/spark_runbook.md`, section 1), which is the
  guard for everything else. Section 2 is left as written. Its "bound to
  localhost" is also wrong in mechanism: checked on 2026-10-03 with the
  Gateway running, the API socket listens on every interface (`[::]:4002`);
  the LAN is closed by Windows Firewall (inbound Block rules for IB Gateway on
  the Public profile, no Allow rule) and by the Gateway's `TrustedIPs=127.0.0.1`,
  and a TCP connect from the Spark to the Zenbook's 4002 times out. The
  runbook carries the re-check command, because a network-profile change or a
  Windows "allow access" prompt can change this without any change in the repo.
- **R16. Delivery, install, `LLMReply`, guard scope and the gate, corrected
  after the final review (2026-10-03).** Section 3 and assumption A1 are left
  as written; where they disagree with this entry, this entry rules.
  (a) Code reaches the Spark by push, never by pull (ruling R-A). A1 (the
  Spark's GitHub credential may clone `forecast-model`) was found false on
  2026-10-02: its key works only in the operator's interactive session and
  there is no credential helper. `~/work/forecast-model` is a non-bare
  checkout with `receive.denyCurrentBranch=updateInstead`, and
  `git push spark <branch>` from the Zenbook (remote
  `spark:work/forecast-model`, over the same Windows `ssh.exe` and
  `Host spark` entry as every other command) updates the checked-out branch
  in place. Section 3's clone from `origin`, its GitHub credential and every
  pull on the Spark are superseded.
  (b) The install is `uv pip install -e ".[dev,ibkr,climate,mcp,web,trading]"
  -c constraints/zenbook-2026-10-03.txt`. Section 3 said the Zenbook has no
  `py-clob-client`; it has 0.34.6, so the `trading` extra is installed on the
  Spark too (ruling R-H: without it one test was skipped and the two
  machines' counts differed). The constraints file carries the date the
  parity report was created, not 2026-09-30 (ruling R-E).
  (c) `LLMReply` gains an additive last field, `finish_reason` (ruling R-Y),
  read from `choices[0].finish_reason`: a string, or None when absent or
  null; anything else is a bad shape. When schema content is not valid JSON
  the `LLMOutputError` names the ending unless it is `stop`, so a reply cut
  off at `length` is told apart from invalid JSON. Section 6's field list is
  otherwise unchanged.
  (d) R13's guarded set gains `scripts/fit_gtm_emos.py`, which writes the
  operational model, and `scripts/collect_climate_labels.py`, which writes the
  training labels (ruling R-Z): thin CLIs over `climate/` but outside it, so an
  `src.llm` import there would feed the model or its labels unseen. Both join
  `GUARDED_FILES` and the literal `REQUIRED_SCOPE`.
  (e) `compare_json.py` gains `--differ PATH` (repeatable), and the gate uses
  it. `--ignore identity.machine` keeps the machine blocks out of the numbers
  because the two machines differ there by design, but alone it let a payload
  pass against a copy of itself; `--differ identity.machine` fails unless both
  payloads hold the block and the blocks are not identical. The CLI also
  prints a `machines:` line and warns on `git_dirty=true`. The 2026-10-03
  report's own gate command predates the flag and stays as evidence; its two
  machine blocks differ (report, section 6).
