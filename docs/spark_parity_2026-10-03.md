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
