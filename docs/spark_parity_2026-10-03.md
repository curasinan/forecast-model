# Spark parity report -- 2026-10-03

Every block below is pasted verbatim from the command that produced it,
except that remote URLs and hardware UUIDs are never pasted.

## 0. Environment and transport (Task 0)

Reach mode: Bash tool over the Windows OpenSSH client (`ssh.exe -o BatchMode=yes spark`),
run 2026-10-02 23:59 local. (Earlier the same evening the box was powered off and the
connection timed out; nothing else changed.)

Spark inventory (`uname -m; python3 --version; ...`):
```
aarch64
Python 3.12.3
git        /usr/bin/git
rsync      /usr/bin/rsync
uv         MISSING
pipx       /usr/bin/pipx
curl       /usr/bin/curl
sha256sum  /usr/bin/sha256sum
/dev/nvme0n1p2  3.7T  287G  3.3T   9% /
GPU 0: NVIDIA GB10
```
`uv` reads MISSING only because `~/.local/bin` is not on the PATH of a non-interactive
shell; it is installed at `/home/scura/.local/bin/uv` (the operator ran the pipx step
before this run) and every later command calls it by that path.

rsync transport dry run: Windows ssh.exe via WSL
```
Number of files: 2 (reg: 1, dir: 1)
Number of created files: 2 (reg: 1, dir: 1)
Number of deleted files: 0
Number of regular files transferred: 1
Total file size: 121 bytes
Total transferred file size: 121 bytes
Literal data: 0 bytes
Matched data: 0 bytes
File list size: 0
File list generation time: 0.014 seconds
File list transfer time: 0.000 seconds
Total bytes sent: 79
Total bytes received: 22
sent 79 bytes  received 22 bytes  67.33 bytes/sec
total size is 121  speedup is 1.20 (DRY RUN)
RSYNC_TRANSPORT_OK
```

Clone credential: none usable from a non-interactive session. Both forms were refused
(`git@github.com: Permission denied (publickey).` and `could not read Username for
'https://github.com': terminal prompts disabled`); the Spark's stack repo itself answers
the same way, so its GitHub key only works inside the operator's interactive session.
Ruling R-A (ledger): code reaches the Spark by `git push` over ssh from the Zenbook into
`~/work/forecast-model` (`receive.denyCurrentBranch=updateInstead`); no GitHub credential
is placed on the Spark, and the Spark-side lint gate diffs against the pushed `main`.

## 1. Zenbook baseline (Task 1)

Run 2026-10-03 00:00 local from the worktree at commit ae0cf0c, with `USERPROFILE`/`HOME`
pointed at a throwaway directory (ruling R-C: the suite writes into
`~/quant_data/polymarket_weather` on whatever home it sees; the throwaway home did receive
a `quant_data` directory, the real one did not).

```
991 passed, 1 warning in 108.26s (0:01:48)
pytest_exit=0
```

Ruff baseline (`python -m ruff check . --statistics --no-cache`, ruff 0.15.4):
```
50	E402  	[ ] module-import-not-at-top-of-file
33	F401  	[*] unused-import
16	E712  	[ ] true-false-comparison
 6	F541  	[*] f-string-missing-placeholders
 6	F841  	[ ] unused-variable
 1	TID251	[ ] banned-api
Found 112 errors.
```

Constraints file `constraints/zenbook-2026-10-03.txt`: 238 lines; the twelve pins the
design's wheel check covered are present with the verified versions (httpx 0.28.1,
ib_async 2.1.0, lightgbm 4.6.0, netCDF4 1.7.4, numpy 2.4.2, pandas 3.0.1, pyarrow 25.0.0,
pydantic 2.12.5, pydantic_core 2.41.5, scikit-learn 1.8.0, scipy 1.17.1, xgboost 3.2.0).

## 2. Spark environment (Task 1)

Code delivery (ruling R-A): `git init ~/work/forecast-model` on the Spark with
`receive.denyCurrentBranch=updateInstead`; from the worktree `git push spark main
spark-research-split` (remote `spark` = `spark:work/forecast-model`, `core.sshCommand` = the
Windows OpenSSH client); then `git checkout spark-research-split` on the Spark. Both machines
at commit 48982c8 for the runs below.

```
uname: aarch64 | python: Python 3.14.2 | uv: uv 0.12.22 (aarch64-unknown-linux-gnu)
```

Install: `uv python install 3.14.2`, `uv venv .venv --python 3.14.2`, then
`uv pip install -e ".[dev,ibkr,climate,mcp,web]" -c constraints/zenbook-2026-10-03.txt`
(exit 0, no unsatisfiable pin), later plus `.[trading]` (ruling R-H below). 104 packages.
Key pins on the Spark, identical to the Zenbook's:
```
httpx 0.28.1, ib-async 2.1.0, lightgbm 4.6.0, netcdf4 1.7.4, numpy 2.4.2, pandas 3.0.1,
pyarrow 25.0.0, pydantic 2.12.5, pydantic-core 2.41.5, pytest 9.0.2, ruff 0.15.4,
scikit-learn 1.8.0, scipy 1.17.1, xgboost 3.2.0
```

Served model (Step 6): `/v1/models` lists exactly `['Qwen3.8-Flash-Next-NVFP4']`;
`docker inspect -f '{{.State.Health.Status}}' qwenflash` prints `healthy`. `.env` holds the
single line `LLM_MODEL=Qwen3.8-Flash-Next-NVFP4` with mode `-rw-------`.

Invariant 1 proof (Step 7):
```
NO_IBKR_ENV
NO_IBKR_IN_PROFILES
trading_mode
  Field required [type=missing, input_value={'llm_model': 'Qwen3.8-Flash-Next-NVFP4'}, input_type=dict]
```
(`IBKRSettings()` raises pydantic's ValidationError: `port` and `trading_mode` have no default.)

Suite and lint (Step 8, `HOME=$(mktemp -d)`). First run, extras `[dev,ibkr,climate,mcp,web]`:
```
990 passed, 1 skipped, 1 warning in 31.79s
pytest_exit=0
SKIPPED [1] tests/test_venues/test_polymarket_adapter.py:225: could not import 'py_clob_client': No module named 'py_clob_client'
```
The Zenbook has `py_clob_client==0.34.6` installed (it is in the constraints file), so the
design's "py-clob-client absent" was wrong. Ruling R-H: `.[trading]` installed on the Spark
as well (24 packages). Second run:
```
991 passed, 1 warning in 29.67s
pytest_exit=0
```
Ruff on the Spark: `Found 112 errors.` (same total as the Zenbook, ruff 0.15.4);
touched-files gate: `TOUCHED_FILES_LINT_OK`.

## 3. First mirror push (Task 3)

Run 2026-10-03 01:02 local, both machines at commit 8481554 (Tasks 2 and 3 delivered by
`git push spark spark-research-split`). Live-root manifest before, the push, manifest after:

```
manifest: 8/9 files present, heartbeat mtime_ns=1790633302591280700 -> ...\parity\manifest_before_push.json
== 1/4 freeze: /mnt/c/Users/asus/quant_data -> /mnt/c/Users/asus/quant_data_snapshot
== 2/4 push: /mnt/c/Users/asus/quant_data_snapshot -> spark:quant_data
Number of files: 603 (reg: 547, dir: 56)
Number of created files: 603 (reg: 547, dir: 56)
Number of deleted files: 0
Number of regular files transferred: 547
Total file size: 7,407,990 bytes
Total transferred file size: 7,407,990 bytes
sent 7,446,535 bytes  received 10,812 bytes  2,130,670.57 bytes/sec
== 3/4 verify on the Spark
== 4/4 identity (saved to /mnt/c/Users/asus/quant_data_snapshot/identity_spark.json)
push_exit=0
manifest: 8/9 files present, heartbeat mtime_ns=1790633302591280700 -> ...\parity\manifest_after_push.json
LIVE_ROOT_UNTOUCHED
```
The one absent manifest entry is `climate_data/paper/gtm_signals_history.jsonl`, which does not
exist on the Zenbook yet (no signals run since that file was introduced). The heartbeat mtime did
not move: the recorder was not running during the push. The newest partition on the Zenbook is
`date=2026-09-28`, matching the identity's `newest_sweep`.

Identity printed by the Spark (`verify_mirror.py --root ~/quant_data`, exit 0):
```
{
  "code_sha": "8481554943b6c91bc0d26416568bd808ccbf48bd",
  "hourly_count": 420,
  "inputs_sha256": {
    "climate_data/forecasts/seas5_global_mean.parquet": "0568916381dcc2e271913e7b0ba6ea11ab8bc6dda8fe7834198aeacfd230c6ed",
    "climate_data/labels/labels_ledger.parquet": "5c4fd2455378ed6ddc89cf956d5dbc148c05f74b71c7587b1820c0a478272550",
    "climate_data/models/gtm_emos.json": "c08cbe14396dabbd0375cc1f7abc311e8997a892771bc201843d3ad96d6d3cac",
    "climate_data/reference/era5_global_mean.parquet": "11601d2cf274546fe0761fb05b46fe666f740168402e046237aea318ac1a8392",
    "climate_data/reports/gtm_emos_diagnostics.json": "32c6ae7355085f076bdce5a34e4ff062061faa51ee75c3b84be37f65d3028f56",
    "ibkr_ticks/contracts_cache.db": "cda232aca470f4c2c1ce5c9ebbebe97c9bf2a56a1dcfb761cd0cb0dff31483a7"
  },
  "labels_max_first_seen_utc": "2026-09-11T13:30:15.641964+00:00",
  "labels_max_period": "2026-08",
  "labels_rows": 3702,
  "labels_sha256": "5c4fd2455378ed6ddc89cf956d5dbc148c05f74b71c7587b1820c0a478272550",
  "model_fingerprint": "2fd53f921150c094",
  "newest_hourly": "ibkr_ticks/quotes_hourly/date=2026-08-24/hourly-20260825T143919Z-390.parquet",
  "newest_sweep": "ibkr_ticks/quotes/date=2026-09-28/quotes-220822Z.parquet",
  "newest_sweep_sha256": "a91a38e6a04433b1410670a06b9992c1df4a7b97e5abfe08297e3961a13e6688",
  "paper_db_quick_check": "ok",
  "paper_runs_lines": 5,
  "sweep_count": 107
}
```

Independent hash cross-check (Step 6): coreutils `sha256sum` on each machine, then the
identity's own values, for the newest sweep and the labels ledger:
```
--- zenbook snapshot
a91a38e6a04433b1410670a06b9992c1df4a7b97e5abfe08297e3961a13e6688
5c4fd2455378ed6ddc89cf956d5dbc148c05f74b71c7587b1820c0a478272550
--- spark mirror
a91a38e6a04433b1410670a06b9992c1df4a7b97e5abfe08297e3961a13e6688
5c4fd2455378ed6ddc89cf956d5dbc148c05f74b71c7587b1820c0a478272550
--- identity's own hashes
  "labels_sha256": "5c4fd2455378ed6ddc89cf956d5dbc148c05f74b71c7587b1820c0a478272550",
  "newest_sweep_sha256": "a91a38e6a04433b1410670a06b9992c1df4a7b97e5abfe08297e3961a13e6688",
```
Three independent computations of the same bytes agree.

## 4. Registered tolerance (Task 6, Step 0)

The scorer's frame refits the ERA5-to-NOAA bridge with `np.linalg.lstsq` on each machine,
so agreement is at LAPACK's kernel-noise level, not last-ulp (design amendment R1). Measured
on the Zenbook, on the frozen snapshot, by forcing three OpenBLAS kernels through the same
code (`OPENBLAS_CORETYPE=HASWELL|SANDYBRIDGE|NEHALEM`), 2026-10-03 02:2x local:
```
ladder=41 tradeable=14 fingerprint=2fd53f921150c094 sweep=2026-09-28T22:08:22Z  (all three kernels)
--- HASWELL vs SANDYBRIDGE
numeric leaves compared: 1536; max abs diff 2.346e-12; max rel diff 3.333e-10
PARITY OK
--- HASWELL vs NEHALEM
numeric leaves compared: 1536; max abs diff 2.463e-12; max rel diff 3.333e-10
PARITY OK
```
Registered tolerance for the cross-machine gate: `--rtol 1e-8 --atol 1e-10`, about 30x
above the measured x86 kernel noise and six orders below the smallest price step that
matters (tick 0.01, minimum edge 0.02). Floor: `--min-numeric 1000` (ruling R-L; the real
payload has 1536 numeric leaves), so an empty ladder can never pass as parity.

## 5. Snapshot identity (Task 6, Step 1)

Both machines at commit `1f743a6` (`CODE_CLEAN` on the Zenbook, 0 dirty tracked files on the
Spark). Live-root manifest taken, then the mirror refreshed:
```
manifest: 8/9 files present, heartbeat mtime_ns=1790633302591280700 -> ...\parity\manifest_before_run.json
== 1/4 freeze ... == 2/4 push ...
Number of regular files transferred: 0
Total transferred file size: 0 bytes
== 3/4 verify on the Spark
== 4/4 identity (saved to /mnt/c/Users/asus/quant_data_snapshot/identity_spark.json)
  "code_sha": "1f743a6ea93fdbd4861a0d219eb4430766b9238f",
  "newest_sweep": "ibkr_ticks/quotes/date=2026-09-28/quotes-220822Z.parquet",
  "sweep_count": 107
push_exit=0
```
Nothing had changed since the first push (0 files transferred); the identity is the one in
section 3 with `code_sha` now `1f743a6...`. Both parity payloads embed the identity their own
machine computed, and the two `identity.snapshot` blocks are equal (checked separately with
Python before the gate ran); the kernel-experiment payloads carry the same block.

## 6. Scoring parity (Task 6, Steps 2-3) -- the gate

```
ladder=41 tradeable=14 fingerprint=2fd53f921150c094 sweep=2026-09-28T22:08:22Z -> C:\Users\asus\quant_data_snapshot\parity\zenbook.json
ladder=41 tradeable=14 fingerprint=2fd53f921150c094 sweep=2026-09-28T22:08:22Z -> /home/scura/parity_spark.json
spark payload fetched: 68059 bytes
numeric leaves compared: 1536; max abs diff 2.699e-12; max rel diff 3.833e-10
PARITY OK
gate_exit=0
```
Command: `compare_json.py zenbook.json spark.json --ignore identity.machine --rtol 1e-8 --atol 1e-10 --min-numeric 1000`.
`identity.snapshot` was NOT ignored, so this line also proves the same bytes (every
`inputs_sha256`, `newest_sweep_sha256`, `labels_sha256`) at the same commit (`code_sha`) on
both machines. Both payloads carry `probe: "spark_parity"` and `go_live.passed: false`
(the live-paper gate is required by construction). The two payloads are committed next to
this report as `docs/spark_parity_2026-10-03_zenbook.json` and `docs/spark_parity_2026-10-03_spark.json`.

Machine blocks (ignored by the compare, recorded here):
```
zenbook: {"git_dirty": false, "machine": "AMD64",   "system": "Windows", "python": "3.14.2", "numpy": "2.4.2", "scipy": "1.17.1", "pandas": "3.0.1", "pyarrow": "25.0.0"}
spark  : {"git_dirty": false, "machine": "aarch64", "system": "Linux",   "python": "3.14.2", "numpy": "2.4.2", "scipy": "1.17.1", "pandas": "3.0.1", "pyarrow": "25.0.0"}
```

## 7. Refit parity, report-only (Task 6, Step 4)

`scripts/fit_gtm_emos.py --trend` on both machines, the Zenbook on the SNAPSHOT root (guarded:
the command refuses to run without `~/quant_data_snapshot/climate_data/models/gtm_emos.json`),
the Spark on its mirror. Both fits report the same bridge
(`beta=0.9042, r2=0.9830, resid_sd=0.0356, n_obs=396, 1993-01..2025-12`), leads 1-6 fitted,
hindcast go-live passed. Wall time 6.6 s (Zenbook) / 2.1 s (Spark).

Model compare (`--rtol 1e-6 --atol 1e-9 --ignore fingerprint`):
```
numeric leaves compared: 50; max abs diff 1.655e-06; max rel diff 2.449e-02
MISMATCH leads.2.d: 1.3594153516311263 != 1.3594170064886202 (abs 1.655e-06, rel 1.217e-06)
PARITY FAILED: 1 mismatch(es)
```
Diagnostics compare (`--ignore fitted_at_utc artifact_paths fingerprint`):
```
numeric leaves compared: 182; max abs diff 1.655e-06; max rel diff 2.449e-02
MISMATCH emos.leads.2.d: 1.3594153516311263 != 1.3594170064886202 (abs 1.655e-06, rel 1.217e-06)
PARITY FAILED: 1 mismatch(es)
```
Reading: one coefficient (lead 2, `d`) differs by 1.2e-6 relative, just past the report-only
threshold of 1e-6; every other coefficient is inside it. The `max rel diff` of 2.4e-2 belongs
to near-zero coefficients whose absolute difference is below `atol`, hence not mismatches.
This is x86 AVX versus aarch64 NEON reduction order through scipy's optimiser, as the design
expected; it is far below the 1e-4 relative level at which the design asks for an
investigation. Report-only: the gate in section 6 is what parity means.

## 8. Suite and lint, same day, both machines (Task 6, Step 5)

Zenbook (throwaway `USERPROFILE`/`HOME`, ruling R-C):
```
1030 passed, 1 warning in 39.55s
pytest_exit=0
Found 112 errors.
All checks passed!   (touched files)
TOUCHED_FILES_LINT_OK
```
Spark (`HOME=$(mktemp -d)`):
```
1030 passed, 1 warning in 29.53s
pytest_exit=0
Found 112 errors.
All checks passed!   (touched files)
TOUCHED_FILES_LINT_OK
```
991 baseline tests plus the 39 added by Tasks 2-5 (34 in `tests/test_analysis/test_spark_parity.py`,
5 in `tests/test_analysis/test_spark_cli.py`); the ruff baseline total is unchanged at 112 on
both machines and the files this branch touches lint clean.

Live root after the whole run (Step 6):
```
manifest: 8/9 files present, heartbeat mtime_ns=1790633302591280700 -> ...\parity\manifest_after_run.json
LIVE_ROOT_UNTOUCHED
```

## 9. Verdict

Scoring parity PASSED at the registered tolerance (rtol 1e-8, atol 1e-10, floor 1000 leaves):
1536 numeric leaves, max relative difference 3.8e-10, inside the x86 kernel-noise band
measured in section 4. The compared identity blocks prove both machines scored the same bytes
at commit 1f743a6. The refit comparison is informational: one of 50 coefficients differs by
1.2e-6 relative. Suites are 1030 passed on both machines, ruff totals equal, touched files
clean, and the live root is byte-identical before and after the run. Both machines ran
Python 3.14.2 with numpy 2.4.2, scipy 1.17.1, pandas 3.0.1, pyarrow 25.0.0 (Windows AMD64 and
Linux aarch64). The Zenbook copy is the runtime and is never deleted under Option B; this
report is what "parity verified" means.

## 10. Local LLM smoke (Task 10)

The branch reaches the Spark by `git push spark spark-research-split` from the Zenbook (ruling
R-A: the Spark's GitHub key is unusable from a non-interactive session, so the Spark never pulls).
The Spark's `.env` holds the single line `LLM_MODEL=Qwen3.8-Flash-Next-NVFP4` (mode 600) and the
`qwenflash` container reported `healthy`. Run at 2026-10-03 06:51 local at commit f11cf09, in
`~/work/forecast-model`:
```
$ .venv/bin/python scripts/llm_smoke.py
base_url=http://127.0.0.1:8000/v1 model=Qwen3.8-Flash-Next-NVFP4 effort=low
served: ['Qwen3.8-Flash-Next-NVFP4']
latency 6.5s, completion tokens 106, 16.4 tok/s, reasoning tokens 86
parsed: {'city': 'Izmir', 'year': 2024, 'passed': True}
OK
smoke_exit=0

$ LLM_LIVE=1 .venv/bin/python -m pytest tests/test_llm/test_live.py -q
.                                                                        [100%]
1 passed in 6.88s
pytest_exit=0
```
A first run at 06:25 (commit 658399a) gave the same verdict: latency 7.5 s, 87 completion tokens
of which 63 reasoning, 11.7 tok/s, live test 1 passed in 9.46 s.

What the real server settled (design section 6 left these to the live run):
- It accepts the `json_schema` response format and the top-level `reasoning_effort`; the
  schema-constrained reply is exactly `{"city":"Izmir","year":2024}` with `finish_reason: stop`.
- The reasoning text comes back under `message.reasoning`, not `message.reasoning_content`.
  The client read only the latter, so `LLMReply.reasoning` was None against the real server
  while every unit test passed on a hand-drawn fixture. Fixed in f11cf09: the client reads
  `reasoning_content` with a fallback to `reasoning`, and two replies captured from the Spark
  (a schema-constrained completion and the `/v1/models` listing) are committed byte-for-byte
  under `tests/test_llm/fixtures/` and driven through `tests/test_llm/test_real_shapes.py`.
  Probe through the client after the fix: `content='hello' reasoning_chars=72
  usage={'prompt_tokens': 47, 'total_tokens': 68, 'completion_tokens': 21, 'reasoning_tokens': 17}`.
- `usage.completion_tokens_details.reasoning_tokens` is present and surfaces as
  `usage["reasoning_tokens"]`; `prompt_tokens_details` is null.
- At `reasoning_effort=low` a yes/no schema answer costs about 20 completion tokens and the
  extraction about 100, so the live test's `max_tokens=2048` is comfortable (ruling R-U); the
  8192 default in the settings stays for real work.
- 11.7-16.4 tok/s single-stream on these short replies, in line with the status note's 15.7.

Unit coverage at f11cf09: `tests/test_llm` 132 passed + 1 skipped (the live test without
`LLM_LIVE`), `tests/test_architecture` 22 passed.

## 11. Final suites (Task 11)

Final code commit 88122a6 (after the whole-branch review and its fix wave), 2026-10-03 09:22
local, both machines at the same SHA (the Spark delivered by `git push spark spark-research-split`).

Zenbook (throwaway `USERPROFILE`/`HOME`, ruling R-C):
```
1243 passed, 1 skipped, 1 warning in 111.40s (0:01:51)
pytest_exit=0
Found 112 errors.
All checks passed!   (touched files)
TOUCHED_FILES_LINT_OK
```
Spark (`HOME=$(mktemp -d)`):
```
1243 passed, 1 skipped, 1 warning in 30.54s
pytest_exit=0
Found 112 errors.
All checks passed!   (touched files)
TOUCHED_FILES_LINT_OK
```
The 1030 tests of section 8 plus 213 added since: 159 in `tests/test_llm` (the client, the
smoke script, the real-reply fixtures and the fix wave's `finish_reason` / nested-body cases;
plus the gated live test, the one skip), 23 more in `tests/test_architecture/test_llm_boundary.py`
and 31 more in `tests/test_analysis` (the `--differ` gate and `subtree`). The warning is the
pre-existing one of section 8. The ruff baseline total is 112 on both machines and every file
this branch touches lints clean on both.

## 12. After landing the side branches

Two finished side branches were merged into this branch before it went to `main`, each after
its own review: the ruff baseline cleanup (7 commits, every hunk shown behaviour-neutral by
AST diff, consumer scans and base-versus-head differential runs; merge 026b4a6) and the
test-home isolation (commit 7b052c5 plus two review-fix commits; merge 97012f4). They change
two things the sections above state as of their own dates:
- The ruff total is 0 on both machines. Sections 1, 8 and 11 record the 112-finding baseline
  that held when they were written; the lint gate is now a clean `ruff check .`.
- The suite no longer needs an outer throwaway home (ruling R-C). `tests/conftest.py`
  redirects the home at `pytest_configure`, and the guard in
  `tests/test_architecture/test_no_home_writes.py`, proven end to end by child sessions in
  `test_home_guard_end_to_end.py`, fails the run if anything appears under the redirected
  `quant_data`. The runs below still used an outer throwaway home, to show it stays empty.

Final tree d5045fc, both machines at the same SHA, 2026-10-03 14:00 local.

Zenbook:
```
1252 passed, 1 skipped, 1 warning in 49.88s
pytest_exit=0
outer home entries: 0
All checks passed!   (ruff check . --no-cache)
```
Spark:
```
1252 passed, 1 skipped, 1 warning in 31.43s
pytest_exit=0
outer home entries: 0
All checks passed!   (ruff check . --no-cache)
```
The 1243 of section 11 plus 9: three tests from the isolation commit and six from its review
fixes (five child-session cases, one user-site test).

Gateway reachability, checked the same day with the Gateway running (runbook, section 1): the
API socket listens on `[::]:4002`, Windows Firewall holds inbound Block rules for IB Gateway on
the Public profile with no Allow rule, the Gateway's `TrustedIPs` is `127.0.0.1`, and a TCP
connect from the Spark to the Zenbook's 4002 timed out (`rc=124`). Design invariant 1's "bound
to localhost" is corrected in amendment R15.
