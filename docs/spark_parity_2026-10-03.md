# Spark parity report -- 2026-10-03

Design: `docs/plans/2026-09-30-spark-research-split-design.md` (section 5).
Plan: `docs/plans/2026-10-01-spark-research-split.md`.
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
