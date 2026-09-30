# Spark Research Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run this repo's research half on the DGX Spark against a one-way data mirror with proven parity, and add a config-driven client for the Spark's local vLLM model, without touching the trading runtime.

**Architecture:** The Zenbook keeps every Gateway role and every scheduled task. The Spark gets a git clone, a uv-managed Python 3.14 venv pinned to the Zenbook's package versions, and an rsync mirror of `~/quant_data` pushed from WSL. A research-only parity toolkit (`analysis/spark_parity.py` plus three thin CLIs under `scripts/spark/`) scores a frozen snapshot on both machines and compares the payloads with an independently computed identity block. A small `src/llm` package talks to vLLM over `httpx`, and an architecture test keeps it out of the pricing and ordering path.

**Tech Stack:** Python 3.14, pydantic-settings, httpx, pandas/pyarrow, pytest, ruff; bash + rsync inside WSL Ubuntu; uv on the Spark only.

**Spec:** `docs/plans/2026-09-30-spark-research-split-design.md`

## Global Constraints

- Work on branch `spark-research-split` in the worktree `.claude/worktrees/spark-research-split`. The checkout at the repo root is the live runtime on `main`; never switch its branch or edit files there.
- The Spark never has `IBKR_PORT` or `IBKR_TRADING_MODE` set anywhere; its `.env` holds exactly one line, `LLM_MODEL=<served id>`.
- Data flows Zenbook -> Spark only. Never copy anything from the Spark into `~/quant_data` on the Zenbook. Parity outputs from the Spark are evidence files and land only under `~/quant_data_snapshot/parity/`.
- Same data root on both machines: `~/quant_data`. No `IBKR_DATA_ROOT` / `CLIMATE_DATA_ROOT` on the Spark.
- No new runtime dependency: the LLM client uses `httpx` (already a core dependency).
- No `src.llm` import under `climate/`, `src/trading/`, `src/venues/`, `src/edge/`, `src/domain/`, `monitoring/`, nor in `scripts/run_ibkr_paper.py`, `scripts/sync_ibkr_paper.py`, `scripts/record_ibkr_ticks.py`, `scripts/run_gtm_paper.py`.
- Never run on the Spark: `record_ibkr_ticks.py`, `sync_ibkr_paper.py`, `run_ibkr_paper.py`, `backfill_ibkr_hourly.py`, `verify_ibkr_entitlements.py`, `collect_climate_labels.py`, `fetch_climate_forecasts.py`.
- Lint gate: the repo's ruff baseline is NOT clean (112 findings in legacy scripts, `src/ui`, `src/storage`, `src/edge` as of 2026-10-01, measured with `ruff check . --statistics --no-cache`). The gate is therefore (a) zero findings in the files this branch touches: `git diff --name-only origin/main...HEAD -- '*.py' | xargs python -m ruff check`, and (b) the same total finding count on both machines with the pinned ruff. Never "fix" baseline findings in runtime code as part of this branch.
- Test gate: the full suite green at every commit, with pytest's own exit code checked (`python -m pytest -q 2>&1 | tail -3; echo "pytest_exit=${PIPESTATUS[0]}"` -> `pytest_exit=0`). Counts are copied from actual runs, never from memory; counts quoted for the new test files are scoped to those files.
- Shell state does not survive between Bash tool calls. Every Windows-side block below starts with `source scripts/spark/env.sh` (created in Task 0), which defines `SSH`, `SCP`, `REPO_WSL`, `COAUTHOR` and `RUN_DATE`. Windows-side commands run in Git Bash from the worktree unless marked PowerShell or WSL.
- Git Bash rewrites a standalone `/mnt/...` argument into a Windows path before `wsl.exe` sees it (MSYS path conversion), so `REPO_WSL` is defined with a double slash (`//mnt/c/...`, verified 2026-10-01 to arrive intact). Composite `bash -lc '...'` strings are unaffected. Never `export MSYS_NO_PATHCONV=1` globally: it would also stop `~/quant_data_snapshot` from reaching python.exe correctly.
- Every Spark-side command below is prefixed `"$SSH" spark '...'`. If Task 0 finds the Bash tool cannot reach the Spark, hand those exact commands to the operator as copy-paste blocks and paste their output back; nothing else changes.
- Commit messages end with the Co-Authored-By trailer this session's attribution reminder specifies. `env.sh` holds it as `COAUTHOR` (currently `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`); the executor re-checks it against the live reminder before the first commit, because the trailer goes into append-only history.
- Committed reports never contain remote URLs from `git remote -v` or hardware UUIDs.
- Editing: the Bash tool mangles long heredocs (apostrophes, backslashes). Write new files with the Write tool, change existing ones with Edit, keep Bash for commands.
- `<date>` in file names is the calendar date of the run that produces the file (`date +%F`).

## File structure

Create:
- `analysis/spark_parity.py` -- mirror checks, snapshot identity, machine info, scorer, JSON compare. Pure functions, no CLI parsing.
- `scripts/spark/verify_mirror.py`, `scripts/spark/parity_score.py`, `scripts/spark/compare_json.py` -- thin argparse CLIs over the module.
- `scripts/spark/push_snapshot.sh` -- WSL bash: freeze locally, rsync to the Spark, verify remotely.
- `.gitattributes` -- keeps the shell script LF in the Windows working tree.
- `constraints/zenbook-<date>.txt` -- frozen package versions from the Zenbook.
- `src/llm/__init__.py`, `src/llm/settings.py`, `src/llm/client.py` -- local LLM settings and client.
- `scripts/llm_smoke.py` -- operator check of the served model and a schema round-trip.
- `tests/test_analysis/conftest.py` (synthetic quant-data root), `tests/test_analysis/test_spark_parity.py`, `tests/test_analysis/test_spark_cli.py`.
- `tests/test_llm/__init__.py`, `tests/test_llm/test_settings.py`, `tests/test_llm/test_client.py`, `tests/test_llm/test_live.py`.
- `tests/test_architecture/test_llm_boundary.py`.
- `docs/spark_runbook.md`, `docs/spark_parity_<date>.md`.

Modify:
- `.env.example` (LLM block), `docs/ibkr_runbook.md` (role table gains a machine column), `docs/MIGRATION_CHECKPOINT.md` (new section 9), memory file `dgx-spark.md`.

---

### Task 0: Spike -- reach the Spark, prove the rsync transport, find the clone credential

**Files:**
- Create: `docs/spark_parity_<date>.md` (skeleton with section 0 only)

**Interfaces:**
- Produces: the facts every later Spark-side step depends on: whether the Bash tool reaches the Spark, which rsync transport works, the clone URL.

- [ ] **Step 1: Create `.gitattributes` and the shell environment file, then reach the Spark**

Create `.gitattributes` first, so the two shell files below are never checked out with CRLF (WSL's bash and Git Bash's `source` both choke on `\r`):
```
# Shell files under scripts/spark/ are read by WSL's bash and sourced by Git Bash
# straight from the Windows working tree, where core.autocrlf=true would otherwise
# check them out with CRLF ("$'\r': command not found" on every line).
scripts/spark/*.sh text eol=lf
```

Create `scripts/spark/env.sh` (sourced at the start of every Windows-side block, never executed):
```bash
# Shared shell variables for the Spark work. From the worktree, in Git Bash:
#   source scripts/spark/env.sh
# Shell state does not survive between Claude's Bash tool calls, hence one file.
SSH=/c/Windows/System32/OpenSSH/ssh.exe   # Windows OpenSSH: reads ~/.ssh/config (Host spark)
SCP=/c/Windows/System32/OpenSSH/scp.exe
# Double slash on purpose: Git Bash (MSYS) rewrites a standalone /mnt/... argument into
# C:/Program Files/Git/mnt/... before wsl.exe sees it; //mnt/... arrives intact (verified).
REPO_WSL=//mnt/c/Users/asus/OneDrive/Desktop/new_work/.claude/worktrees/spark-research-split
# Verify against the session's live attribution reminder before the first commit.
COAUTHOR="Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
RUN_DATE=$(date +%F)
export SSH SCP REPO_WSL COAUTHOR RUN_DATE
```
Then run:
```bash
source scripts/spark/env.sh
"$SSH" -o BatchMode=yes spark 'uname -m; python3 --version; for t in git rsync uv pipx curl sha256sum; do printf "%-10s " "$t"; command -v "$t" || echo MISSING; done; df -h /home | tail -1; nvidia-smi -L | head -1 | cut -d"(" -f1'
```
Expected: `aarch64`, a `Python 3.12.x` line, `git`, `rsync` and `sha256sum` present, `uv`/`pipx` likely MISSING, free space in the TB range, one GPU name (the UUID is cut on purpose: the report is committed).
If the command fails: `Host spark` in `~/.ssh/config` uses an on-disk IdentityFile, so no agent is involved; the cause is network, sandbox egress or `known_hosts`. Record "operator-run mode" in section 0 and from here on hand every `"$SSH" spark '...'` block to the operator.

- [ ] **Step 2: Prove the WSL rsync transport with a dry run**

Run (Git Bash; this is WSL executing rsync with the Windows ssh client as transport; `--dry-run` connects and compares but writes nothing):
```bash
wsl.exe -d Ubuntu -- bash -lc 'rsync -rlt --dry-run --stats -e /mnt/c/Windows/System32/OpenSSH/ssh.exe /mnt/c/Users/asus/quant_data/ibkr_ticks/state/ spark:quant_data_probe/ && echo RSYNC_TRANSPORT_OK'
```
Expected: a `Number of files:` stats block and `RSYNC_TRANSPORT_OK`. This proves the handshake and the file-list exchange only; the bulk data path through `ssh.exe` is first exercised by the real push in Task 3, which is why that task cross-checks sha256 sums at both ends.
If it fails with an interop or exec error (`ssh.exe: not found`, `Exec format error`, or a hang on agent access): fallback is a WSL-native key. Inside WSL run `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519_wsl` and add to WSL's `~/.ssh/config`:
```
Host spark
  HostName 192.168.1.31
  User scura
  IdentityFile ~/.ssh/id_ed25519_wsl
```
WSL's default user here is root (verified: `$HOME` = `/root`), so the key lands in `/root/.ssh/`. Adding the public key to the Spark's `authorized_keys` is an account change on the operator's machine: ASK before running `"$SSH" spark 'cat >> ~/.ssh/authorized_keys' < "//wsl$/Ubuntu/root/.ssh/id_ed25519_wsl.pub"`. Re-run the dry run with `-e ssh` instead of the Windows path. Record which transport won; `push_snapshot.sh` (Task 3) reads it from `SPARK_SSH`.

- [ ] **Step 3: Find the clone credential**

Run:
```bash
"$SSH" spark 'cd ~/stack && git remote -v | head -2; echo "--- ssh form"; git ls-remote --heads git@github.com:curasinan/algorithmic-trade.git 2>&1 | head -2; echo "--- https form"; git ls-remote --heads https://github.com/curasinan/algorithmic-trade.git 2>&1 | head -2'
```
Expected: whichever form prints a `refs/heads/main` line is `CLONE_URL` for Task 1. If neither works, stop and ask the operator to add a read-only deploy key for `algorithmic-trade` on the Spark; do not create credentials yourself.

- [ ] **Step 4: Write the report skeleton**

Create `docs/spark_parity_<date>.md` with the Write tool:
```markdown
# Spark parity report -- <date>

Design: `docs/plans/2026-09-30-spark-research-split-design.md` (section 5).
Plan: `docs/plans/2026-10-01-spark-research-split.md`.
Every block below is pasted verbatim from the command that produced it,
except that remote URLs and hardware UUIDs are never pasted.

## 0. Environment and transport (Task 0)

Reach mode: <"Bash tool over ssh.exe" or "operator-run">.

Spark inventory (`uname -m; python3 --version; ...`):
```
<paste>
```

rsync transport dry run: <"Windows ssh.exe via WSL" or "WSL-native key">
```
<paste>
```

Clone URL: `<CLONE_URL>` (from `git ls-remote`).
```
The `<paste>` markers are filled in this same step with the real outputs of Steps 1-3 before moving on; the file never contains a bare marker at commit time.

No commit yet: Task 1 commits this file together with the constraints file.

---

### Task 1: Zenbook baseline, constraints file, Spark clone + venv + suite

**Files:**
- Create: `constraints/zenbook-<date>.txt`
- Modify: `docs/spark_parity_<date>.md` (sections 1 and 2)

**Interfaces:**
- Consumes: `CLONE_URL` from Task 0.
- Produces: a Spark clone at `~/work/algorithmic-trade` tracking `spark-research-split`, with `.venv/bin/python` (3.14) and the suite green; the constraints file every later install uses.

- [ ] **Step 1: Record the Zenbook baseline from a fresh run**

Run (from the worktree):
```bash
source scripts/spark/env.sh
python -m pytest -q 2>&1 | tail -3; echo "pytest_exit=${PIPESTATUS[0]}"
python -m ruff check . --statistics --no-cache 2>&1 | tail -8
python -m ruff --version
```
Expected: a `N passed` line, `pytest_exit=0`, a `Found 112 errors.` baseline line (the number may drift if `main` moved; whatever it is today is the baseline), and the ruff version. Paste all of it into the report under `## 1. Zenbook baseline (Task 1)`. The touched-files lint gate (`git diff --name-only origin/main...HEAD -- '*.py' | xargs python -m ruff check`) is trivially green here because the branch touches no Python file yet.

- [ ] **Step 2: Freeze the Zenbook environment**

Run:
```bash
mkdir -p constraints
{ echo "# pip freeze of the Zenbook's global Python $(python -c 'import platform;print(platform.python_version())') on $(date +%F); editable and @-URL entries removed"; python -m pip freeze --exclude-editable | grep -v ' @ '; } > "constraints/zenbook-$(date +%F).txt"
grep -E '^(numpy|pandas|scipy|scikit-learn|xgboost|lightgbm|pyarrow|netCDF4|ib_async|pydantic|pydantic_core|httpx)==' constraints/zenbook-*.txt
```
Expected: 12 lines: `numpy==2.4.2`, `pandas==3.0.1`, `scipy==1.17.1`, `scikit-learn==1.8.0`, `xgboost==3.2.0`, `lightgbm==4.6.0`, `pyarrow==25.0.0`, `netCDF4==1.7.4`, `ib_async==2.1.0`, `pydantic==2.12.5`, `pydantic_core==2.41.5`, `httpx==0.28.1`. Any other value means the Zenbook changed since the spec's wheel check; re-run that check (`pip install --dry-run --only-binary=:all: --platform manylinux_2_28_aarch64 --python-version 3.14 --implementation cp --abi cp314 --target /tmp/x <pkg>==<ver>`) for the changed package before continuing.

- [ ] **Step 3: Commit and push the branch (the Spark must be able to clone it)**

Run:
```bash
source scripts/spark/env.sh
git add .gitattributes constraints/ docs/spark_parity_*.md docs/plans/2026-10-01-spark-research-split.md scripts/spark/env.sh
git commit -m "Freeze the Zenbook environment and start the Spark parity report" -m "$COAUTHOR"
git push -u origin spark-research-split
```

- [ ] **Step 4: Install uv on the Spark (operator step: needs sudo or the vendor installer)**

Hand this block to the operator to run in their own Spark session, then continue:
```bash
sudo apt-get install -y pipx && pipx install uv && pipx ensurepath && ~/.local/bin/uv --version
```
Expected: `uv 0.x.y`. (Ubuntu 24.04 is PEP 668 "externally managed", so `pip install uv` into the system Python is refused; pipx is the packaged way.)

- [ ] **Step 5: Clone, create the venv, install with the constraints**

Run (replace `CLONE_URL` with the form Task 0 found; the constraints glob picks the file Step 2 wrote):
```bash
source scripts/spark/env.sh
"$SSH" spark 'mkdir -p ~/work && cd ~/work && git clone --branch spark-research-split CLONE_URL algorithmic-trade && cd algorithmic-trade && git log --oneline -1'
"$SSH" spark 'cd ~/work/algorithmic-trade && ~/.local/bin/uv python install 3.14.2 && ~/.local/bin/uv venv .venv --python 3.14.2 && .venv/bin/python -V'
"$SSH" spark 'cd ~/work/algorithmic-trade && ~/.local/bin/uv pip install --python .venv/bin/python -e ".[dev,ibkr,climate,mcp,web]" -c "$(ls constraints/zenbook-*.txt | tail -1)" 2>&1 | tail -15'
```
Expected: the branch's latest commit, `Python 3.14.2`, and an `Installed N packages` line with no `No solution found`. If uv reports 3.14.2 unavailable, use `3.14` and record the exact version installed. If a single pin has no aarch64 wheel, edit that one line in the constraints file to the nearest version that resolves, commit it as "Relax <pkg> pin for aarch64 (no cp314 wheel at <ver>)", push, `git pull --ff-only` on the Spark, re-run the install, and record the deviation in the report.

- [ ] **Step 6: Write the Spark `.env` with the served model id**

Run:
```bash
source scripts/spark/env.sh
"$SSH" spark 'curl -s --max-time 10 http://127.0.0.1:8000/v1/models || echo VLLM_DOWN'
```
Expected: JSON whose `data[0].id` is the served model name. `VLLM_DOWN` means the container is not up. Starting containers on the operator's box is the operator's call: ASK, and hand over `cd ~/stack && docker compose config --services` (confirms the service name; the handover note says `qwenflash`) followed by `cat /opt/models/Qwen3.8-Flash-Next-NVFP4/ple-sidecar/ples_nvfp4/* > /dev/null && docker compose up -d qwenflash`. The cold start is 11-14 min, longer than one Bash tool call should wait, so poll in separate calls (`"$SSH" spark 'curl -s --max-time 5 http://127.0.0.1:8000/v1/models | head -c 200 || echo NOT_YET'`) every few minutes until JSON appears. Then, with the id in hand:
```bash
"$SSH" spark "printf 'LLM_MODEL=%s\n' '<served id>' > ~/work/algorithmic-trade/.env && cat ~/work/algorithmic-trade/.env"
```
Expected: exactly one line. Nothing else ever goes into this file.

- [ ] **Step 7: Prove the Spark cannot reach the Gateway (design invariant 1)**

Run:
```bash
"$SSH" spark 'env | grep "^IBKR_" || echo NO_IBKR_ENV; grep -l "IBKR_" ~/.bashrc ~/.profile /etc/environment 2>/dev/null || echo NO_IBKR_IN_PROFILES; cd ~/work/algorithmic-trade && .venv/bin/python -c "from src.venues.ibkr.settings import IBKRSettings; IBKRSettings()" 2>&1 | tail -1'
```
Expected: `NO_IBKR_ENV`, `NO_IBKR_IN_PROFILES`, and a pydantic `ValidationError` line naming `port` and `trading_mode` (the loud-failure rule). Paste all three under `## 2. Spark environment (Task 1)`; the runbook cites this as the proof. `collect_climate_labels.py` needs neither a credential nor the Gateway, so for it the guard is documentation only; a forked ledger would surface in Task 6 as a `labels_sha256` mismatch in the identity compare.

- [ ] **Step 8: Run the suite and lint on the Spark**

Run (an isolated HOME keeps the suite's own `~/quant_data/polymarket_weather` writes out of the mirror):
```bash
"$SSH" spark 'cd ~/work/algorithmic-trade && HOME=$(mktemp -d) .venv/bin/python -m pytest -q 2>&1 | tail -3; echo "pytest_exit=${PIPESTATUS[0]}"; .venv/bin/python -m ruff check . --statistics --no-cache 2>&1 | tail -3; git diff --name-only origin/main...HEAD -- "*.py" | xargs -r .venv/bin/python -m ruff check && echo TOUCHED_FILES_LINT_OK'
```
Expected: the same `passed` count as Step 1, `pytest_exit=0`, the same `Found N errors.` total as the Zenbook baseline, and `TOUCHED_FILES_LINT_OK`. A test failure is a finding, not something to skip: a test that depends on the operator's `.env`, on Windows path separators, or on `Path.home()` contents gets fixed in the repo (tests only), committed, and re-run on both machines.

- [ ] **Step 9: Record and commit**

Paste `uname -m`, `.venv/bin/python -V`, `uv --version`, the install tail, the invariant proof and the suite/lint output into the report under `## 2. Spark environment (Task 1)`. Then:
```bash
source scripts/spark/env.sh
git add docs/spark_parity_*.md constraints/
git commit -m "Record the Spark environment and its first green suite run" -m "$COAUTHOR"
git push
```

---

### Task 2: Mirror integrity and snapshot identity (`analysis/spark_parity.py`, part 1) + `verify_mirror.py`

**Files:**
- Create: `analysis/spark_parity.py`
- Create: `tests/test_analysis/conftest.py`
- Create: `tests/test_analysis/test_spark_parity.py`
- Create: `scripts/spark/verify_mirror.py`
- Create: `tests/test_analysis/test_spark_cli.py`

**Interfaces:**
- Produces (module `analysis.spark_parity`):
  - `TICKS_DIR = "ibkr_ticks"`, `CLIMATE_DIR = "climate_data"`, `PAPER_DB_REL = ("paper", "ibkr_paper.db")`, `RUNS_JSONL_REL = ("paper", "ibkr_paper_runs.jsonl")`, `CONTRACTS_DB = "contracts_cache.db"`, `SEAS5_REL`, `ERA5_REL`, `DIAG_REL`, `INPUT_FILES`, `MANIFEST_FILES`, `HEARTBEAT_REL`, `REPO_ROOT`
  - `check_parquet(path: Path) -> str | None`
  - `check_sqlite(path: Path) -> str | None`
  - `hourly_files(ticks_root: Path) -> list[Path]`
  - `verify_mirror(root: Path) -> list[str]`
  - `inputs_sha256(root: Path) -> dict[str, str | None]`
  - `code_identity(repo_root: Path = REPO_ROOT) -> dict[str, Any]` (`{"git_sha": str, "dirty": bool | None}`)
  - `snapshot_identity(root: Path) -> dict[str, Any]` (includes `inputs_sha256` and `code_sha`)
  - `operational_manifest(root: Path) -> dict[str, dict[str, Any] | None]` (per file: `sha256`, `size`, `mtime_ns`)
- Produces (test fixtures in `tests/test_analysis/conftest.py`): `spark_root` (a `SparkRoot` named tuple: `root`, `sweep`, `ledger`, `model`, `seas5`, `era5`), `sweep_writer`, `hourly_writer`, `emos_identity_model`, `dec_2026_frame`, `universe_json`.
- Produces (`scripts/spark/verify_mirror.py`, `scripts/spark/live_manifest.py`): `main(argv) -> int` each.

- [ ] **Step 1: Write the shared fixtures**

Create `tests/test_analysis/conftest.py`:
```python
"""Fixtures for the Spark parity tooling tests (analysis/spark_parity.py).

A synthetic quant-data root with the layout the tooling assumes:
ibkr_ticks/quotes/date=*/quotes-HHMMSSZ.parquet (recorder-shaped, the 13
SWEEP_SCHEMA columns), climate_data/labels/labels_ledger.parquet
(climate.storage LEDGER_COLUMNS), climate_data/models/gtm_emos.json (a
one-lead identity EMOS model) and the two reduced CDS products that
build_training_frame reads. The sweep columns are spelled out here on
purpose so these tests never import the recorder, which drags in the venue
layer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import pytest

from climate.emos import EmosModel, LeadParams
from climate.storage import LEDGER_COLUMNS

SWEEP_COLUMNS = [
    "ts_utc", "con_id", "symbol", "expiry", "strike", "right", "bid", "ask",
    "last", "close", "bid_size", "ask_size", "data_type",
]
SWEEP_TS = "2026-09-28T22:08:22Z"
SWEEP_REL = Path("ibkr_ticks") / "quotes" / "date=2026-09-28" / "quotes-220822Z.parquet"


class SparkRoot(NamedTuple):
    root: Path
    sweep: Path
    ledger: Path
    model: Path
    seas5: Path
    era5: Path


def _sweep_rows(ts_utc: str = SWEEP_TS) -> list[dict]:
    """One GTM Dec-2026 market: YES leg con_id 1, NO leg con_id 2."""
    base = dict(
        ts_utc=ts_utc, symbol="GTM", expiry="20261231", strike=1.2,
        last=float("nan"), close=float("nan"), bid_size=10.0, ask_size=10.0,
        data_type="delayed",
    )
    return [
        {**base, "con_id": 1, "right": "C", "bid": 0.40, "ask": 0.44},
        {**base, "con_id": 2, "right": "P", "bid": 0.55, "ask": 0.59},
    ]


def _write_sweep(path: Path, ts_utc: str = SWEEP_TS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_sweep_rows(ts_utc), columns=SWEEP_COLUMNS).to_parquet(path, index=False)
    return path


def _identity_model() -> EmosModel:
    model = EmosModel(trend=False)
    model.params[1] = LeadParams(
        lead_month=1, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1
    )
    return model


@pytest.fixture
def spark_root(tmp_path: Path) -> SparkRoot:
    root = tmp_path / "quant_data"
    sweep = _write_sweep(root / SWEEP_REL)
    climate = root / "climate_data"
    ledger = climate / "labels" / "labels_ledger.parquet"
    ledger.parent.mkdir(parents=True)
    pd.DataFrame(
        [["gtm", "2026-07", 1.18, "2026-08-15T00:00:00Z", "live_first_print", False, "u"]],
        columns=LEDGER_COLUMNS,
    ).to_parquet(ledger, index=False)
    model = climate / "models" / "gtm_emos.json"
    model.parent.mkdir(parents=True)
    _identity_model().to_json(model)
    seas5 = climate / "forecasts" / "seas5_global_mean.parquet"
    seas5.parent.mkdir(parents=True)
    pd.DataFrame(
        {"init": ["2026-08"], "lead": [1], "member": [0], "value_K": [288.1]}
    ).to_parquet(seas5, index=False)
    era5 = climate / "reference" / "era5_global_mean.parquet"
    era5.parent.mkdir(parents=True)
    pd.DataFrame({"month": ["2026-07"], "value_K": [288.0]}).to_parquet(era5, index=False)
    return SparkRoot(root=root, sweep=sweep, ledger=ledger, model=model, seas5=seas5, era5=era5)


@pytest.fixture
def sweep_writer() -> Callable[[Path, str, str], Path]:
    """(root, 'YYYY-MM-DD', 'HHMMSS') -> a new recorder-shaped sweep parquet."""

    def _write(root: Path, date: str, hhmmss: str) -> Path:
        ts = f"{date}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}Z"
        rel = Path("ibkr_ticks") / "quotes" / f"date={date}" / f"quotes-{hhmmss}Z.parquet"
        return _write_sweep(root / rel, ts)

    return _write


@pytest.fixture
def hourly_writer() -> Callable[[Path, str, str], Path]:
    """(root, 'YYYY-MM-DD', name) -> a small parquet under quotes_hourly/date=.../."""

    def _write(root: Path, date: str, name: str) -> Path:
        path = root / "ibkr_ticks" / "quotes_hourly" / f"date={date}" / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"con_id": [1], "open": [0.4], "close": [0.41]}).to_parquet(path, index=False)
        return path

    return _write


@pytest.fixture
def emos_identity_model() -> EmosModel:
    return _identity_model()


@pytest.fixture
def dec_2026_frame() -> pd.DataFrame:
    """Training-frame shape used by climate.paper.run_paper, one target month."""
    return pd.DataFrame(
        {
            "target_month": ["2026-12"],
            "lead_month": [1],
            "y_C": [np.nan],
            "fc_mean_C": [1.30],
            "fc_sd_C": [0.12],
            "target_year_frac": [2026.958],
            "month_of_year": [12],
        }
    )


@pytest.fixture
def universe_json(tmp_path: Path) -> Path:
    """Stage-0 snapshot shape (data/ibkr_universe_live.json) for the two legs."""
    contracts = {
        "1": {"conId": 1, "symbol": "GTM", "localSymbol": "GTM_1226_1.2_YES",
              "lastTradeDateOrContractMonth": "20261231"},
        "2": {"conId": 2, "symbol": "GTM", "localSymbol": "GTM_1226_1.2_NO",
              "lastTradeDateOrContractMonth": "20261231"},
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps({"contracts": contracts}), encoding="utf-8")
    return path
```

- [ ] **Step 2: Write the failing tests for checks, verify, identity**

Create `tests/test_analysis/test_spark_parity.py`:
```python
"""analysis/spark_parity.py: mirror checks, identity, scorer, compare.

Design: docs/plans/2026-09-30-spark-research-split-design.md section 5.
No network, no real data: the spark_root fixture (conftest) builds a
synthetic quant-data root in tmp_path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import analysis.spark_parity as sp

# --------------------------------------------------------------------------- #
# check_parquet / check_sqlite
# --------------------------------------------------------------------------- #


def test_check_parquet_accepts_a_complete_file(spark_root):
    assert sp.check_parquet(spark_root.sweep) is None


def test_check_parquet_names_a_truncated_file(spark_root):
    data = spark_root.sweep.read_bytes()
    spark_root.sweep.write_bytes(data[: len(data) // 2])
    err = sp.check_parquet(spark_root.sweep)
    assert err  # non-empty error text


def test_check_sqlite_ok_then_truncated(tmp_path):
    db = tmp_path / "paper.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(2000)])
    conn.commit()
    conn.close()
    assert sp.check_sqlite(db) is None
    db.write_bytes(db.read_bytes()[:1024])
    assert sp.check_sqlite(db)


def test_check_sqlite_rejects_a_non_database_file(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(bytes(range(256)))
    assert sp.check_sqlite(junk)


# --------------------------------------------------------------------------- #
# verify_mirror
# --------------------------------------------------------------------------- #


def test_verify_mirror_clean_root_has_no_failures(spark_root):
    assert sp.verify_mirror(spark_root.root) == []


def test_verify_mirror_lists_every_broken_file(spark_root):
    spark_root.sweep.write_bytes(bytes(range(256)))
    spark_root.model.write_text("{not json", encoding="utf-8")
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 2
    assert any("quotes-220822Z.parquet" in f for f in failures)
    assert any("gtm_emos.json" in f for f in failures)


def test_verify_mirror_reports_missing_required_files(tmp_path):
    root = tmp_path / "empty"
    (root / "ibkr_ticks").mkdir(parents=True)
    (root / "climate_data").mkdir()
    failures = sp.verify_mirror(root)
    assert any("no sweep parquet" in f for f in failures)
    assert any("labels_ledger.parquet: missing" in f for f in failures)
    assert any("gtm_emos.json: missing" in f for f in failures)


def test_verify_mirror_checks_optional_databases_when_present(spark_root):
    paper_db = spark_root.root / "ibkr_ticks" / "paper" / "ibkr_paper.db"
    paper_db.parent.mkdir(parents=True)
    paper_db.write_bytes(bytes(range(256)))
    cache_db = spark_root.root / "ibkr_ticks" / "contracts_cache.db"
    cache_db.write_bytes(bytes(range(256)))
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 2
    assert any("ibkr_paper.db" in f for f in failures)
    assert any("contracts_cache.db" in f for f in failures)


# --------------------------------------------------------------------------- #
# snapshot_identity
# --------------------------------------------------------------------------- #


def test_snapshot_identity_describes_the_files(spark_root):
    ident = sp.snapshot_identity(spark_root.root)
    assert ident["newest_sweep"] == "ibkr_ticks/quotes/date=2026-09-28/quotes-220822Z.parquet"
    assert ident["sweep_count"] == 1
    assert ident["newest_hourly"] is None and ident["hourly_count"] == 0
    assert ident["labels_rows"] == 1
    assert ident["labels_max_period"] == "2026-07"
    assert ident["labels_max_first_seen_utc"] == "2026-08-15T00:00:00Z"
    assert len(ident["model_fingerprint"]) >= 8
    assert len(ident["newest_sweep_sha256"]) == 64
    assert len(ident["labels_sha256"]) == 64
    assert ident["paper_db_quick_check"] is None
    assert ident["paper_runs_lines"] is None


def test_snapshot_identity_counts_paper_runs_and_checks_the_db(spark_root):
    runs = spark_root.root / "climate_data" / "paper" / "ibkr_paper_runs.jsonl"
    runs.parent.mkdir(parents=True)
    runs.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    db = spark_root.root / "ibkr_ticks" / "paper" / "ibkr_paper.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    ident = sp.snapshot_identity(spark_root.root)
    assert ident["paper_runs_lines"] == 2
    assert ident["paper_db_quick_check"] == "ok"


def test_snapshot_identity_round_trips_through_json(spark_root):
    ident = sp.snapshot_identity(spark_root.root)
    assert json.loads(json.dumps(ident, sort_keys=True)) == ident


def test_identity_hashes_match_hashlib_and_change_on_a_byte_flip(spark_root):
    before = sp.snapshot_identity(spark_root.root)
    assert before["newest_sweep_sha256"] == hashlib.sha256(spark_root.sweep.read_bytes()).hexdigest()
    assert before["labels_sha256"] == hashlib.sha256(spark_root.ledger.read_bytes()).hexdigest()
    data = bytearray(spark_root.sweep.read_bytes())
    data[len(data) // 2] ^= 0xFF
    spark_root.sweep.write_bytes(bytes(data))
    after = sp.snapshot_identity(spark_root.root)
    assert after["newest_sweep_sha256"] != before["newest_sweep_sha256"]
    assert after["labels_sha256"] == before["labels_sha256"]


def test_verify_and_identity_use_the_newest_sweep_and_hourly(
    spark_root, sweep_writer, hourly_writer
):
    newest_sweep = sweep_writer(spark_root.root, "2026-09-29", "101010")
    old_hourly = hourly_writer(spark_root.root, "2026-09-27", "bars-a")
    newest_hourly = hourly_writer(spark_root.root, "2026-09-28", "bars-b")
    ident = sp.snapshot_identity(spark_root.root)
    assert ident["newest_sweep"].endswith("date=2026-09-29/quotes-101010Z.parquet")
    assert ident["sweep_count"] == 2
    assert ident["newest_hourly"].endswith("date=2026-09-28/bars-b.parquet")
    assert ident["hourly_count"] == 2
    # a torn OLDER file is not this check's business; a torn NEWEST one is
    spark_root.sweep.write_bytes(bytes(range(256)))
    old_hourly.write_bytes(bytes(range(256)))
    assert sp.verify_mirror(spark_root.root) == []
    newest_sweep.write_bytes(bytes(range(256)))
    newest_hourly.write_bytes(bytes(range(256)))
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 2
    assert any("quotes-101010Z.parquet" in f for f in failures)
    assert any("bars-b.parquet" in f for f in failures)


def test_verify_mirror_requires_the_cds_inputs(spark_root):
    spark_root.seas5.unlink()
    spark_root.era5.write_bytes(bytes(range(256)))
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 2
    assert any("seas5_global_mean.parquet: missing" in f for f in failures)
    assert any("era5_global_mean.parquet" in f and "missing" not in f for f in failures)


def test_inputs_sha256_lists_every_input_present_or_not(spark_root):
    hashes = sp.inputs_sha256(spark_root.root)
    assert set(hashes) == {"/".join(p) for p in sp.INPUT_FILES}
    expected = hashlib.sha256(spark_root.model.read_bytes()).hexdigest()
    assert hashes["climate_data/models/gtm_emos.json"] == expected
    assert hashes["ibkr_ticks/contracts_cache.db"] is None
    assert sp.snapshot_identity(spark_root.root)["inputs_sha256"] == hashes


def test_code_identity_reports_the_commit_and_survives_a_non_repo(spark_root, tmp_path):
    here = sp.code_identity()
    assert len(here["git_sha"]) == 40
    assert isinstance(here["dirty"], bool)
    assert sp.snapshot_identity(spark_root.root)["code_sha"] == here["git_sha"]
    assert sp.code_identity(tmp_path) == {"git_sha": "unknown", "dirty": None}


def test_operational_manifest_lists_files_and_tracks_changes(spark_root):
    manifest = sp.operational_manifest(spark_root.root)
    assert set(manifest) == {"/".join(p) for p in sp.MANIFEST_FILES}
    entry = manifest["climate_data/models/gtm_emos.json"]
    assert set(entry) == {"sha256", "size", "mtime_ns"}
    assert entry["size"] == spark_root.model.stat().st_size
    assert manifest["ibkr_ticks/paper/ibkr_paper.db"] is None
    text = spark_root.model.read_text(encoding="utf-8")
    spark_root.model.write_text(text + "\n", encoding="utf-8")
    changed = sp.operational_manifest(spark_root.root)
    assert changed["climate_data/models/gtm_emos.json"]["sha256"] != entry["sha256"]
    assert {k for k in manifest if manifest[k] != changed[k]} == {"climate_data/models/gtm_emos.json"}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_analysis/test_spark_parity.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'analysis.spark_parity'`.

- [ ] **Step 4: Write the module (part 1)**

Create `analysis/spark_parity.py`:
```python
"""Spark research-mirror parity tooling.

Design: docs/plans/2026-09-30-spark-research-split-design.md, section 5.

Research-only. Nothing here writes an operational file: no
``paper/gtm_signals.json``, no history line, no ledger. The CLIs under
``scripts/spark/`` are thin wrappers over these functions.

Layout assumed under a quant-data root (``~/quant_data`` on both machines,
``~/quant_data_snapshot`` for the Zenbook-side frozen copy)::

    <root>/ibkr_ticks/quotes/date=YYYY-MM-DD/quotes-HHMMSSZ.parquet
    <root>/ibkr_ticks/quotes_hourly/date=*/*.parquet          (optional)
    <root>/ibkr_ticks/contracts_cache.db                       (optional)
    <root>/ibkr_ticks/paper/ibkr_paper.db                      (optional)
    <root>/climate_data/labels/labels_ledger.parquet
    <root>/climate_data/models/gtm_emos.json
    <root>/climate_data/paper/ibkr_paper_runs.jsonl            (optional)
    <root>/climate_data/reports/gtm_emos_diagnostics.json      (optional)
"""

from __future__ import annotations

import glob
import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from climate.emos import EmosModel
from climate.fit import MODEL_REL, model_fingerprint
from climate.ladder import list_sweep_files
from climate.storage import ledger_path

REPO_ROOT = Path(__file__).resolve().parents[1]

TICKS_DIR = "ibkr_ticks"
CLIMATE_DIR = "climate_data"
CONTRACTS_DB = "contracts_cache.db"
PAPER_DB_REL = ("paper", "ibkr_paper.db")
RUNS_JSONL_REL = ("paper", "ibkr_paper_runs.jsonl")
SEAS5_REL = ("forecasts", "seas5_global_mean.parquet")
ERA5_REL = ("reference", "era5_global_mean.parquet")
DIAG_REL = ("reports", "gtm_emos_diagnostics.json")
HEARTBEAT_REL = (TICKS_DIR, "state", "recorder_state.json")

# Everything the scoring probe reads, hashed into the compared identity block
# (relative to the quant-data root). Absent entries hash to None on both sides.
INPUT_FILES = (
    (CLIMATE_DIR, *SEAS5_REL),
    (CLIMATE_DIR, *ERA5_REL),
    (CLIMATE_DIR, "labels", "labels_ledger.parquet"),
    (CLIMATE_DIR, *MODEL_REL),
    (CLIMATE_DIR, *DIAG_REL),
    (TICKS_DIR, CONTRACTS_DB),
)

# The operational files a push or a refit must never change on the LIVE root:
# manifested (sha256, size, mtime) before and after, and diffed.
MANIFEST_FILES = (
    (TICKS_DIR, *PAPER_DB_REL),
    (TICKS_DIR, "paper", "ibkr_sync_runs.jsonl"),
    (TICKS_DIR, "paper", "permid_map.json"),
    (CLIMATE_DIR, "paper", "gtm_signals.json"),
    (CLIMATE_DIR, "paper", "gtm_signals_history.jsonl"),
    (CLIMATE_DIR, *RUNS_JSONL_REL),
    (CLIMATE_DIR, *MODEL_REL),
    (CLIMATE_DIR, *DIAG_REL),
    (CLIMATE_DIR, "labels", "labels_ledger.parquet"),
)

# --------------------------------------------------------------------------- #
# Mirror integrity
# --------------------------------------------------------------------------- #


def check_parquet(path: Path) -> str | None:
    """None when ``path`` is a complete parquet file, else the error text.

    Reads the whole table, not just the footer: a torn copy of a small
    sweep file can still carry a plausible footer offset.
    """
    try:
        pq.read_table(path)
    except Exception as exc:  # noqa: BLE001 -- any reader error IS the finding
        return f"{type(exc).__name__}: {exc}"
    return None


def check_sqlite(path: Path) -> str | None:
    """None when ``PRAGMA quick_check`` answers ``ok``, else the error text.

    Opens read-only and closes explicitly (Windows keeps an open handle
    alive until GC, which would block a later rewrite in tests).
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
    answers = [str(r[0]) for r in rows]
    if answers != ["ok"]:
        return "quick_check: " + "; ".join(answers)
    return None


def hourly_files(ticks_root: Path) -> list[Path]:
    """Backfilled hourly-bar parquets under quotes_hourly/date=*/, oldest first."""
    pattern = os.path.join(str(ticks_root), "quotes_hourly", "date=*", "*.parquet")
    return [Path(p) for p in sorted(glob.glob(pattern))]


def verify_mirror(root: Path) -> list[str]:
    """Every unreadable or missing canonical file under ``root``, as 'relpath: error'.

    Never skips silently: a missing REQUIRED file (newest sweep, labels
    ledger, model) is a failure; the optional trees (hourly bars, the two
    SQLite files) are checked only when present. Empty list = readable.
    """
    root = Path(root)
    ticks = root / TICKS_DIR
    climate = root / CLIMATE_DIR
    failures: list[str] = []

    def _rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    sweeps = list_sweep_files(ticks)
    if not sweeps:
        failures.append(f"{TICKS_DIR}/quotes: no sweep parquet found")
    else:
        err = check_parquet(sweeps[-1])
        if err:
            failures.append(f"{_rel(sweeps[-1])}: {err}")

    hourly = hourly_files(ticks)
    if hourly:
        err = check_parquet(hourly[-1])
        if err:
            failures.append(f"{_rel(hourly[-1])}: {err}")

    # The three inputs build_training_frame reads are REQUIRED: a mirror
    # without them cannot be scored, and a torn copy must be named here.
    for required in (climate.joinpath(*SEAS5_REL), climate.joinpath(*ERA5_REL), ledger_path(climate)):
        if not required.exists():
            failures.append(f"{_rel(required)}: missing")
        else:
            err = check_parquet(required)
            if err:
                failures.append(f"{_rel(required)}: {err}")

    model = climate.joinpath(*MODEL_REL)
    if not model.exists():
        failures.append(f"{_rel(model)}: missing")
    else:
        try:
            EmosModel.from_json(model)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"{_rel(model)}: {type(exc).__name__}: {exc}")

    for db in (ticks / CONTRACTS_DB, ticks.joinpath(*PAPER_DB_REL)):
        if db.exists():
            err = check_sqlite(db)
            if err:
                failures.append(f"{_rel(db)}: {err}")
    return failures


# --------------------------------------------------------------------------- #
# Snapshot identity
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_identity(root: Path) -> dict[str, Any]:
    """What was scored, computed from the files under ``root`` on THIS machine.

    Both parity payloads carry the identity their own machine computed;
    ``compare_json`` then asserts the two blocks are equal, which is the
    independent witness that both runs read the same bytes (a check
    written by the process it certifies proves nothing).
    """
    root = Path(root)
    ticks = root / TICKS_DIR
    climate = root / CLIMATE_DIR
    sweeps = list_sweep_files(ticks)
    hourly = hourly_files(ticks)
    ledger = ledger_path(climate)
    ledger_df = pd.read_parquet(ledger) if ledger.exists() else None
    model_path = climate.joinpath(*MODEL_REL)
    runs = climate.joinpath(*RUNS_JSONL_REL)
    paper_db = ticks.joinpath(*PAPER_DB_REL)

    def _rel(p: Path) -> str:
        return p.relative_to(root).as_posix()

    labels_rows = int(len(ledger_df)) if ledger_df is not None else None
    has_labels = bool(labels_rows)
    return {
        "newest_sweep": _rel(sweeps[-1]) if sweeps else None,
        "newest_sweep_sha256": _sha256(sweeps[-1]) if sweeps else None,
        "sweep_count": len(sweeps),
        "newest_hourly": _rel(hourly[-1]) if hourly else None,
        "hourly_count": len(hourly),
        "labels_rows": labels_rows,
        "labels_sha256": _sha256(ledger) if ledger.exists() else None,
        "labels_max_period": (
            str(ledger_df["period"].astype(str).max()) if has_labels else None
        ),
        "labels_max_first_seen_utc": (
            str(ledger_df["first_seen_utc"].astype(str).max()) if has_labels else None
        ),
        "model_fingerprint": (
            model_fingerprint(EmosModel.from_json(model_path)) if model_path.exists() else None
        ),
        "paper_runs_lines": (
            sum(1 for _ in runs.open("rb")) if runs.exists() else None
        ),
        "paper_db_quick_check": (
            (check_sqlite(paper_db) or "ok") if paper_db.exists() else None
        ),
        "inputs_sha256": inputs_sha256(root),
        "code_sha": code_identity()["git_sha"],
    }


def inputs_sha256(root: Path) -> dict[str, str | None]:
    """sha256 of every scoring input under ``root`` (None when absent), keyed by relpath."""
    root = Path(root)
    out: dict[str, str | None] = {}
    for parts in INPUT_FILES:
        path = root.joinpath(*parts)
        out["/".join(parts)] = _sha256(path) if path.exists() else None
    return out


def code_identity(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Commit the scoring code ran at, and whether the tree was dirty.

    ``git_sha`` goes into the COMPARED identity block, so two payloads only
    count as comparable when both machines ran the same commit. ``dirty``
    is informational (the report is edited on the Zenbook while a run
    happens) and belongs in the ignored machine block.
    """

    def _git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    sha = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {"git_sha": sha or "unknown", "dirty": bool(status) if status is not None else None}


# --------------------------------------------------------------------------- #
# Operational manifest (proof the LIVE root was not touched)
# --------------------------------------------------------------------------- #


def operational_manifest(root: Path) -> dict[str, dict[str, Any] | None]:
    """(sha256, size, mtime_ns) of every operational file under ``root``; None when absent.

    Taken before and after anything that reads the LIVE root (a push, a
    refit) and diffed byte-for-byte: an equal manifest is the proof that
    the paper book, the ledgers and the operational model were not touched.
    """
    root = Path(root)
    out: dict[str, dict[str, Any] | None] = {}
    for parts in MANIFEST_FILES:
        path = root.joinpath(*parts)
        if path.exists():
            stat = path.stat()
            out["/".join(parts)] = {
                "sha256": _sha256(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            }
        else:
            out["/".join(parts)] = None
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_analysis/test_spark_parity.py -q`
Expected: 17 passed.

- [ ] **Step 6: Write the CLI and its test**

Create `scripts/spark/verify_mirror.py`:
```python
"""Verify a mirrored quant-data root and print its snapshot identity.

    python scripts/spark/verify_mirror.py --root ~/quant_data

Exit 0: every canonical file opens; stdout is the identity JSON (design
section 5.1). Exit 1: stderr lists every unreadable or missing file --
never a silent skip. Read-only; safe on the live Zenbook root too.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import snapshot_identity, verify_mirror  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a mirrored quant-data root.")
    parser.add_argument("--root", required=True, help="quant-data root, e.g. ~/quant_data")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    failures = verify_mirror(root)
    for line in failures:
        print(f"UNREADABLE {line}", file=sys.stderr)
    if failures:
        print(f"verify_mirror: {len(failures)} problem(s) under {root}", file=sys.stderr)
        return 1
    print(json.dumps(snapshot_identity(root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Create `scripts/spark/live_manifest.py`:
```python
"""Manifest the operational files under a quant-data root (sha256, size, mtime).

    python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/manifest_before.json

Taken before and after anything that reads the LIVE root (a push, a refit)
and compared with a plain `diff`: identical files mean the paper book, the
ledgers and the operational model were not touched. Read-only. The recorder
heartbeat's mtime is printed for information but not written, because it
legitimately changes every sweep.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import HEARTBEAT_REL, operational_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manifest the operational files of a root.")
    parser.add_argument("--root", required=True, help="quant-data root, e.g. ~/quant_data")
    parser.add_argument("--out", required=True, help="where to write the manifest JSON")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    manifest = operational_manifest(root)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    heartbeat = root.joinpath(*HEARTBEAT_REL)
    hb_mtime = heartbeat.stat().st_mtime_ns if heartbeat.exists() else None
    present = sum(1 for v in manifest.values() if v is not None)
    print(f"manifest: {present}/{len(manifest)} files present, heartbeat mtime_ns={hb_mtime} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Create `tests/test_analysis/test_spark_cli.py`:
```python
"""Thin CLIs under scripts/spark/: exit codes and outputs on the synthetic root.

Same import pattern as tests/test_climate/test_noaa_labels.py: the scripts
directory goes on sys.path and the script module is imported by name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import live_manifest as manifest_cli  # noqa: E402
import verify_mirror as verify_cli  # noqa: E402


def test_live_manifest_cli_writes_the_manifest(spark_root, tmp_path, capsys):
    out = tmp_path / "m.json"
    rc = manifest_cli.main(["--root", str(spark_root.root), "--out", str(out)])
    assert rc == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["climate_data/models/gtm_emos.json"]["size"] == spark_root.model.stat().st_size
    assert "heartbeat mtime_ns=None" in capsys.readouterr().out


def test_verify_mirror_cli_exit_0_prints_identity(spark_root, capsys):
    rc = verify_cli.main(["--root", str(spark_root.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["sweep_count"] == 1


def test_verify_mirror_cli_exit_1_lists_failures(spark_root, capsys):
    spark_root.sweep.write_bytes(bytes(range(256)))
    rc = verify_cli.main(["--root", str(spark_root.root)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "quotes-220822Z.parquet" in captured.err
    assert captured.out == ""
```

- [ ] **Step 7: Run all new tests, lint, and the architecture tests**

Run:
```bash
python -m pytest tests/test_analysis/test_spark_parity.py tests/test_analysis/test_spark_cli.py -q
python -m ruff check analysis scripts/spark tests/test_analysis
python -m pytest tests/test_architecture tests/test_analysis -q
```
Expected: `20 passed` (17 + 3) for the two new files, ruff reporting no findings for the three new paths, and the architecture plus pre-existing analysis tests still green (the new files import no venue SDK).

- [ ] **Step 8: Commit**

```bash
source scripts/spark/env.sh
git add analysis/spark_parity.py scripts/spark/verify_mirror.py scripts/spark/live_manifest.py tests/test_analysis/
git commit -m "Add mirror integrity checks, the snapshot identity and the live manifest for Spark parity" -m "$COAUTHOR"
```

---

### Task 3: `push_snapshot.sh` and the first real push, bracketed by live-root manifests

**Files:**
- Create: `scripts/spark/push_snapshot.sh`
- Modify: `docs/spark_parity_<date>.md` (section 3)

**Interfaces:**
- Consumes: `scripts/spark/verify_mirror.py` and `scripts/spark/live_manifest.py` on the Spark and the Zenbook (Task 2, pulled to the Spark in Step 4); `.gitattributes` (Task 0).
- Produces: `~/quant_data_snapshot` on the Zenbook (frozen copy) and `~/quant_data` on the Spark (mirror), both verified; the identity JSON at `~/quant_data_snapshot/identity_spark.json`; the evidence directory `~/quant_data_snapshot/parity/`.

- [ ] **Step 1: Confirm the LF attribute is in force**

Run:
```bash
git check-attr eol scripts/spark/push_snapshot.sh scripts/spark/env.sh
```
Expected: `eol: lf` for both (from the `.gitattributes` committed in Task 1).

- [ ] **Step 2: Write the push script**

Create `scripts/spark/push_snapshot.sh`:
```bash
#!/usr/bin/env bash
# Push a frozen copy of the Zenbook's quant data to the Spark. One way, never back.
#
# Runs INSIDE WSL Ubuntu (rsync lives there; Git Bash has none). From the Zenbook:
#   PowerShell: wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/spark/push_snapshot.sh [--include-raw]
#   Git Bash:   wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh" [--include-raw]
#               (REPO_WSL from scripts/spark/env.sh starts with //mnt on purpose: Git Bash
#               rewrites a bare /mnt/... argument into a Windows path before wsl.exe sees it)
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
# climate_data/raw/ (7.6 GB of CDS downloads, re-downloadable) is excluded unless
# --include-raw. Avoid 17:25-17:40 local: the paper-sync task writes the SQLite DB.
set -euo pipefail

SRC="${QUANT_DATA_SRC:-/mnt/c/Users/asus/quant_data}"
SNAP="${QUANT_DATA_SNAPSHOT:-/mnt/c/Users/asus/quant_data_snapshot}"
DEST="${SPARK_DEST:-spark:quant_data}"
SSH="${SPARK_SSH:-/mnt/c/Windows/System32/OpenSSH/ssh.exe}"
REMOTE_REPO="${SPARK_REMOTE_REPO:-~/work/algorithmic-trade}"
REMOTE_PY="${SPARK_REMOTE_PY:-.venv/bin/python}"

INCLUDE_RAW=0
for arg in "$@"; do
  case "$arg" in
    --include-raw) INCLUDE_RAW=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "push_snapshot: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

EXCLUDES=(--exclude "ibkr_ticks/state/" --exclude "*.tmp" --exclude "*-journal"
          --exclude "*.db-wal" --exclude "*.db-shm")
if [ "$INCLUDE_RAW" -eq 0 ]; then
  EXCLUDES+=(--exclude "climate_data/raw/")
fi
# Evidence that lives inside the snapshot: protected from the freeze's --delete
# (rsync never deletes excluded paths on the receiver) and never pushed.
KEEP=(--exclude "/parity/" --exclude "/identity_spark.json")

freeze() {
  rsync -rlt --delete "${EXCLUDES[@]}" "${KEEP[@]}" "$SRC/" "$SNAP/"
}

echo "== 1/4 freeze: $SRC -> $SNAP"
mkdir -p "$SNAP"
set +e
freeze
rc=$?
set -e
if [ "$rc" -eq 24 ]; then
  echo "   a source file vanished mid-freeze (rsync 24, a writer raced us); retrying once"
  freeze
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

echo "== 2/4 push: $SNAP -> $DEST"
rsync -rlt --stats --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r "${EXCLUDES[@]}" "${KEEP[@]}" \
  -e "$SSH" "$SNAP/" "$DEST/"

echo "== 3/4 verify on the Spark"
TMP_ID="$SNAP/identity_spark.json.partial"
if "$SSH" spark "cd $REMOTE_REPO && $REMOTE_PY scripts/spark/verify_mirror.py --root ~/quant_data" > "$TMP_ID"; then
  mv "$TMP_ID" "$SNAP/identity_spark.json"
else
  rc=$?
  rm -f "$TMP_ID"
  echo "push_snapshot: verify_mirror failed on the Spark (exit $rc); the previous identity is untouched" >&2
  exit "$rc"
fi

echo "== 4/4 identity (saved to $SNAP/identity_spark.json)"
cat "$SNAP/identity_spark.json"
```
If Task 0 chose the WSL-native key, the operator invokes the script with `SPARK_SSH=ssh` in the environment; nothing in the file changes.

- [ ] **Step 3: Syntax-check inside WSL and confirm LF**

Run (Git Bash):
```bash
source scripts/spark/env.sh
wsl.exe -d Ubuntu -- bash -n "$REPO_WSL/scripts/spark/push_snapshot.sh" && echo SYNTAX_OK
wsl.exe -d Ubuntu -- file "$REPO_WSL/scripts/spark/push_snapshot.sh" "$REPO_WSL/scripts/spark/env.sh"
wsl.exe -d Ubuntu -- bash -lc "cd $REPO_WSL && QUANT_DATA_SRC=/tmp/ps_src QUANT_DATA_SNAPSHOT=/tmp/ps_snap SPARK_SSH=/bin/false bash scripts/spark/push_snapshot.sh; echo rehearsal_exit=\$?"
```
Expected: `SYNTAX_OK`; two `file` lines WITHOUT "with CRLF line terminators"; and a rehearsal on empty temp dirs that prints `== 1/4` and `== 2/4` and then fails at the push with `rehearsal_exit=` non-zero (there is no `/tmp/ps_src`, so the freeze fails first with rsync's exit 23; the point is that the script parses, runs, and stops loudly). If CRLF shows up, strip it in the working tree (`git add --renormalize` fixes only the index, not the file bash reads): `wsl.exe -d Ubuntu -- sed -i 's/\r$//' "$REPO_WSL/scripts/spark/push_snapshot.sh" "$REPO_WSL/scripts/spark/env.sh"`, then re-check.

- [ ] **Step 4: Commit, push, pull on the Spark**

```bash
source scripts/spark/env.sh
git add scripts/spark/push_snapshot.sh
git commit -m "Add the WSL rsync push that mirrors quant data to the Spark" -m "$COAUTHOR"
git push
"$SSH" spark 'cd ~/work/algorithmic-trade && git pull --ff-only && git log --oneline -1'
```
Expected: the Spark reports the commit just pushed.

- [ ] **Step 5: Manifest the live root, push (outside 17:25-17:40 local), manifest again**

Run (Git Bash):
```bash
source scripts/spark/env.sh
mkdir -p ~/quant_data_snapshot/parity
python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/manifest_before_push.json
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh"
python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/manifest_after_push.json
diff ~/quant_data_snapshot/parity/manifest_before_push.json ~/quant_data_snapshot/parity/manifest_after_push.json && echo LIVE_ROOT_UNTOUCHED
```
Expected: the four `== n/4` lines, an rsync `--stats` block for step 2, an identity JSON from step 3 whose `newest_sweep` equals the newest partition on the Zenbook (`ls ~/quant_data/ibkr_ticks/quotes | tail -1`), then `LIVE_ROOT_UNTOUCHED`. If step 3 exits 1 naming the newest sweep, the push raced the recorder: run the script again. If the `diff` shows a changed file, a scheduled task wrote it inside the window (16:30 labels, 17:30 paper sync): record which, and bracket a second push to show the pair agrees.

- [ ] **Step 6: Cross-check the bulk transfer with independent hashes**

Run:
```bash
source scripts/spark/env.sh
REL=$(python -c "import json, pathlib; print(json.load(open(pathlib.Path.home() / 'quant_data_snapshot' / 'identity_spark.json'))['newest_sweep'])")
sha256sum ~/quant_data_snapshot/"$REL" ~/quant_data_snapshot/climate_data/labels/labels_ledger.parquet | cut -c1-64
"$SSH" spark "sha256sum ~/quant_data/$REL ~/quant_data/climate_data/labels/labels_ledger.parquet | cut -c1-64"
grep -E '"(newest_sweep_sha256|labels_sha256)"' ~/quant_data_snapshot/identity_spark.json
```
Expected: the two `sha256sum` pairs identical, and the identity file's own `newest_sweep_sha256` / `labels_sha256` equal to them (three independent computations of the same bytes: coreutils on each machine, and our code on the Spark).

- [ ] **Step 7: Record and commit**

Paste the whole run, the `LIVE_ROOT_UNTOUCHED` line and the hash pairs into the report under `## 3. First mirror push (Task 3)`, then:
```bash
source scripts/spark/env.sh
git add docs/spark_parity_*.md
git commit -m "Record the first verified mirror push to the Spark" -m "$COAUTHOR"
```

---

### Task 4: `compare_json` (module part 2) + `compare_json.py` CLI

**Files:**
- Modify: `analysis/spark_parity.py` (append)
- Modify: `tests/test_analysis/test_spark_parity.py` (append)
- Create: `scripts/spark/compare_json.py`
- Modify: `tests/test_analysis/test_spark_cli.py` (append)

**Interfaces:**
- Produces: `CompareResult` (fields `mismatches: list[str]`, `n_numeric: int`, `max_abs: float`, `max_rel: float`; property `ok`) and `compare_json(left, right, *, rtol=1e-9, atol=1e-12, ignore=()) -> CompareResult`; CLI `main(argv) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_analysis/test_spark_parity.py`:
```python
# --------------------------------------------------------------------------- #
# compare_json
# --------------------------------------------------------------------------- #


def test_compare_identical_documents_ok():
    doc = {"x": 1.0, "s": "a", "l": [1, 2, {"k": True}], "n": None}
    res = sp.compare_json(doc, json.loads(json.dumps(doc)))
    assert res.ok
    assert res.n_numeric == 3
    assert res.max_abs == 0.0


def test_compare_within_tolerance_records_max_diffs():
    res = sp.compare_json({"x": 1.0}, {"x": 1.0 + 1e-12})
    assert res.ok
    assert 0.9e-12 < res.max_abs < 1.1e-12


def test_compare_beyond_tolerance_fails_with_the_path():
    res = sp.compare_json({"a": {"b": [0.5, 0.25]}}, {"a": {"b": [0.5, 0.2500001]}})
    assert not res.ok
    assert res.mismatches[0].startswith("a.b[1]:")


def test_compare_nan_equals_nan_but_none_does_not():
    assert sp.compare_json({"v": float("nan")}, {"v": float("nan")}).ok
    res = sp.compare_json({"v": None}, {"v": float("nan")})
    assert not res.ok
    assert res.mismatches[0].startswith("v:")


def test_compare_missing_extra_keys_and_list_lengths():
    res = sp.compare_json({"a": 1, "b": 2, "l": [1]}, {"a": 1, "c": 3, "l": [1, 2]})
    assert {m.split(":")[0] for m in res.mismatches} == {"b", "c", "l"}


def test_compare_ignore_skips_a_dotted_subtree():
    left = {"identity": {"snapshot": {"n": 1}, "machine": {"python": "3.14.2"}}}
    right = {"identity": {"snapshot": {"n": 1}, "machine": {"python": "3.12.3"}}}
    assert not sp.compare_json(left, right).ok
    assert sp.compare_json(left, right, ignore=["identity.machine"]).ok


def test_compare_bool_and_int_are_distinct_but_int_and_float_are_numbers():
    assert not sp.compare_json({"t": True}, {"t": 1}).ok
    assert sp.compare_json({"t": 1}, {"t": 1.0}).ok
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_analysis/test_spark_parity.py -q -k compare`
Expected: 7 failed with `AttributeError: module 'analysis.spark_parity' has no attribute 'compare_json'`.

- [ ] **Step 3: Implement**

Append to `analysis/spark_parity.py` (add `import math` and `from dataclasses import dataclass, field` and `from typing import Iterable` to the imports at the top of the file):
```python
# --------------------------------------------------------------------------- #
# JSON compare
# --------------------------------------------------------------------------- #


@dataclass
class CompareResult:
    mismatches: list[str] = field(default_factory=list)
    n_numeric: int = 0
    max_abs: float = 0.0
    max_rel: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.mismatches


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare_json(
    left: Any,
    right: Any,
    *,
    rtol: float = 1e-9,
    atol: float = 1e-12,
    ignore: Iterable[str] = (),
    _path: str = "",
    _result: CompareResult | None = None,
) -> CompareResult:
    """Recursive structural compare of two JSON documents.

    Numbers agree when ``|a-b| <= atol + rtol*max(|a|,|b|)``; NaN equals
    NaN; None never equals NaN; bools, strings and None are exact; dict key
    sets and list lengths must match. ``ignore`` holds dotted paths
    (``identity.machine``) whose subtrees are skipped. Every mismatch is
    reported with its path; the result also carries the largest numeric
    differences seen, so a passing run still shows how close it was.
    """
    result = _result if _result is not None else CompareResult()
    ignored = set(ignore)
    if _path in ignored:
        return result
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right), key=str):
            sub = f"{_path}.{key}" if _path else str(key)
            if sub in ignored:
                continue
            if key not in left:
                result.mismatches.append(f"{sub}: missing on left")
            elif key not in right:
                result.mismatches.append(f"{sub}: missing on right")
            else:
                compare_json(left[key], right[key], rtol=rtol, atol=atol,
                             ignore=ignored, _path=sub, _result=result)
        return result
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            result.mismatches.append(f"{_path}: length {len(left)} != {len(right)}")
            return result
        for i, (a, b) in enumerate(zip(left, right, strict=True)):
            compare_json(a, b, rtol=rtol, atol=atol, ignore=ignored,
                         _path=f"{_path}[{i}]", _result=result)
        return result
    if _is_number(left) and _is_number(right):
        result.n_numeric += 1
        fa, fb = float(left), float(right)
        if math.isnan(fa) and math.isnan(fb):
            return result
        if math.isnan(fa) or math.isnan(fb) or math.isinf(fa) or math.isinf(fb):
            if fa != fb:
                result.mismatches.append(f"{_path}: {left!r} != {right!r}")
            return result
        diff = abs(fa - fb)
        scale = max(abs(fa), abs(fb))
        rel = diff / scale if scale > 0 else 0.0
        result.max_abs = max(result.max_abs, diff)
        result.max_rel = max(result.max_rel, rel)
        if diff > atol + rtol * scale:
            result.mismatches.append(
                f"{_path}: {left!r} != {right!r} (abs {diff:.3e}, rel {rel:.3e})"
            )
        return result
    if type(left) is not type(right) or left != right:
        result.mismatches.append(f"{_path}: {left!r} != {right!r}")
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_analysis/test_spark_parity.py -q`
Expected: 24 passed.

- [ ] **Step 5: Write the CLI and its test**

Create `scripts/spark/compare_json.py`:
```python
"""Compare two parity payloads (or any two JSON files) within a tolerance.

    python scripts/spark/compare_json.py left.json right.json \
        [--rtol 1e-9] [--atol 1e-12] [--ignore identity.machine ...]

Exit 0 on parity, 1 on any mismatch (each one printed with its path).
Design: docs/plans/2026-09-30-spark-research-split-design.md section 5.3.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import compare_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two JSON files within a tolerance.")
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--rtol", type=float, default=1e-9)
    parser.add_argument("--atol", type=float, default=1e-12)
    parser.add_argument("--ignore", nargs="*", default=[],
                        help="dotted paths to skip, e.g. identity.machine")
    parser.add_argument("--min-numeric", type=int, default=1,
                        help="fail when fewer numeric leaves were compared "
                             "(two empty documents must never pass as parity)")
    args = parser.parse_args(argv)
    left = json.loads(Path(args.left).expanduser().read_text(encoding="utf-8"))
    right = json.loads(Path(args.right).expanduser().read_text(encoding="utf-8"))
    result = compare_json(left, right, rtol=args.rtol, atol=args.atol, ignore=args.ignore)
    print(
        f"numeric leaves compared: {result.n_numeric}; "
        f"max abs diff {result.max_abs:.3e}; max rel diff {result.max_rel:.3e}"
    )
    for line in result.mismatches:
        print(f"MISMATCH {line}")
    if result.n_numeric < args.min_numeric:
        print(
            f"PARITY FAILED: only {result.n_numeric} numeric leaf/leaves compared "
            f"(floor {args.min_numeric})"
        )
        return 1
    if result.ok:
        print("PARITY OK")
        return 0
    print(f"PARITY FAILED: {len(result.mismatches)} mismatch(es)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

Append to `tests/test_analysis/test_spark_cli.py` (add `import compare_json as compare_cli  # noqa: E402` next to the other script import):
```python
def test_compare_json_cli_exit_codes(tmp_path, capsys):
    left = tmp_path / "a.json"
    right = tmp_path / "b.json"
    left.write_text(json.dumps({"x": 1.0, "identity": {"machine": {"python": "a"}}}),
                    encoding="utf-8")
    right.write_text(json.dumps({"x": 1.0, "identity": {"machine": {"python": "b"}}}),
                     encoding="utf-8")
    assert compare_cli.main([str(left), str(right)]) == 1
    assert "MISMATCH identity.machine.python" in capsys.readouterr().out
    assert compare_cli.main([str(left), str(right), "--ignore", "identity.machine"]) == 0
    assert "PARITY OK" in capsys.readouterr().out
    empty = tmp_path / "e.json"
    empty.write_text("{}", encoding="utf-8")
    assert compare_cli.main([str(empty), str(empty)]) == 1
    assert "floor" in capsys.readouterr().out
```

- [ ] **Step 6: Run, lint, commit**

```bash
source scripts/spark/env.sh
python -m pytest tests/test_analysis/test_spark_parity.py tests/test_analysis/test_spark_cli.py -q
python -m ruff check analysis scripts/spark tests/test_analysis
git add analysis/spark_parity.py scripts/spark/compare_json.py tests/test_analysis/
git commit -m "Add the tolerance-aware JSON compare for Spark parity" -m "$COAUTHOR"
```
Expected: `28 passed` (24 + 4), ruff reporting no findings for the three paths.

---

### Task 5: `machine_info`, `score_snapshot`, `score_snapshot_from_root` (module part 3) + `parity_score.py` CLI

**Files:**
- Modify: `analysis/spark_parity.py` (append)
- Modify: `tests/test_analysis/test_spark_parity.py` (append)
- Create: `scripts/spark/parity_score.py`
- Modify: `tests/test_analysis/test_spark_cli.py` (append)

**Interfaces:**
- Produces: `MACHINE_PACKAGES = ("numpy", "scipy", "pandas", "pyarrow")`, `PROBE_MARKER = "spark_parity"`, `machine_info() -> dict[str, Any]` (platform, python, package versions, `git_dirty`), `score_snapshot(frame, model, *, ticks_root, universe_json, identity, summary=None) -> dict[str, Any]` (the `run_paper` payload minus `ts_utc`, plus `probe`, `bridge` and `identity`; raises `FileNotFoundError` for a missing universe file), `score_snapshot_from_root(root, *, universe_json) -> dict[str, Any]`; CLI `main(argv) -> int` writing `--out`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_analysis/test_spark_parity.py` (add `import platform` and `import pytest` to the imports):
```python
# --------------------------------------------------------------------------- #
# machine_info / score_snapshot
# --------------------------------------------------------------------------- #


def test_machine_info_reports_platform_versions_and_dirtiness():
    info = sp.machine_info()
    assert info["python"] == platform.python_version()
    assert info["machine"] == platform.machine()
    assert set(sp.MACHINE_PACKAGES) <= set(info)
    assert isinstance(info["git_dirty"], bool)


def test_score_snapshot_strips_the_clock_marks_the_probe_and_never_writes(
    spark_root, universe_json, dec_2026_frame, emos_identity_model
):
    payload = sp.score_snapshot(
        dec_2026_frame,
        emos_identity_model,
        ticks_root=spark_root.root / "ibkr_ticks",
        universe_json=universe_json,
        identity=sp.snapshot_identity(spark_root.root),
    )
    assert "ts_utc" not in payload
    assert payload["probe"] == "spark_parity"
    assert payload["bridge"] is None  # the synthetic frame carries no bridge
    assert payload["sweep_ts_utc"] == "2026-09-28T22:08:22Z"
    assert payload["n_quotes"] == 1
    assert payload["n_ladder"] == 1
    assert payload["signals"][0]["target_month"] == "2026-12"
    assert payload["identity"]["snapshot"]["sweep_count"] == 1
    assert payload["identity"]["machine"]["python"] == platform.python_version()
    assert payload["go_live"]["passed"] is False  # live-paper gate required: never a verdict
    assert not (spark_root.root / "climate_data" / "paper").exists()
    json.dumps(payload, default=str)  # serialisable the way the entrypoint serialises


def test_score_snapshot_refuses_a_missing_universe_file(
    spark_root, tmp_path, dec_2026_frame, emos_identity_model
):
    with pytest.raises(FileNotFoundError):
        sp.score_snapshot(
            dec_2026_frame,
            emos_identity_model,
            ticks_root=spark_root.root / "ibkr_ticks",
            universe_json=tmp_path / "absent.json",
            identity=sp.snapshot_identity(spark_root.root),
        )


def test_score_snapshot_from_root_wires_model_identity_frame_and_bridge(
    spark_root, universe_json, dec_2026_frame, monkeypatch
):
    # build_training_frame needs real SEAS5/ERA5/NOAA inputs; stub it and check the wiring.
    dec_2026_frame.attrs["bridge_json"] = '{"alpha": 1.5, "beta": 0.9}'
    monkeypatch.setattr(sp, "build_training_frame", lambda root: dec_2026_frame)
    payload = sp.score_snapshot_from_root(spark_root.root, universe_json=universe_json)
    expected = sp.snapshot_identity(spark_root.root)["model_fingerprint"]
    assert payload["identity"]["snapshot"]["model_fingerprint"] == expected
    assert payload["model_fingerprint"] == expected
    assert payload["bridge"] == {"alpha": 1.5, "beta": 0.9}
    assert payload["n_ladder"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_analysis/test_spark_parity.py -q -k "machine_info or score_snapshot"`
Expected: 3 failed with `AttributeError`.

- [ ] **Step 3: Implement**

Append to `analysis/spark_parity.py` (add `import json`, `import platform`, `from importlib import metadata` to the imports, plus `from climate.dataset import build_training_frame` and `from climate.paper import run_paper`):
```python
# --------------------------------------------------------------------------- #
# Machine info and the scoring probe
# --------------------------------------------------------------------------- #

MACHINE_PACKAGES = ("numpy", "scipy", "pandas", "pyarrow")
PROBE_MARKER = "spark_parity"


def machine_info() -> dict[str, Any]:
    """Which machine and numerical stack produced a payload (ignored by the compare)."""
    info: dict[str, Any] = {
        "machine": platform.machine(),
        "system": platform.system(),
        "python": platform.python_version(),
        "git_dirty": code_identity()["dirty"],
    }
    for name in MACHINE_PACKAGES:
        try:
            info[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            info[name] = "missing"
    return info


def score_snapshot(
    frame: pd.DataFrame,
    model: EmosModel,
    *,
    ticks_root: Path,
    universe_json: Path | None,
    identity: dict[str, Any],
    summary: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """``climate.paper.run_paper`` on a frozen snapshot, minus the wall clock.

    A parity probe, not an operator entrypoint: no staleness gate (that
    belongs to scripts/run_gtm_paper.py, and this probe must work on a
    stale mirror); gate inputs pinned to zero with the live-paper gate
    REQUIRED, so ``go_live.passed`` is False by construction on both
    machines and can never be mistaken for an operational verdict; a
    top-level ``probe`` marker for the same reason; nothing written under
    ``paper/``. The bridge the frame was built with travels along, so a
    parity failure can be localised to the LAPACK fit rather than the CDF.
    """
    if universe_json is not None and not Path(universe_json).exists():
        raise FileNotFoundError(f"universe json not found: {universe_json}")
    payload = run_paper(
        frame,
        model,
        summary=summary,
        ticks_root=ticks_root,
        universe_json=universe_json,
        paper_days=0,
        resolved_months=0,
        require_live_paper=True,
    )
    payload.pop("ts_utc", None)
    payload["probe"] = PROBE_MARKER
    bridge_json = frame.attrs.get("bridge_json")
    payload["bridge"] = json.loads(bridge_json) if bridge_json else None
    payload["identity"] = {"snapshot": identity, "machine": machine_info()}
    return payload


def score_snapshot_from_root(root: Path, *, universe_json: Path | None) -> dict[str, Any]:
    """Load frame, model and diagnostics from ``root`` exactly like the entrypoint, then score."""
    root = Path(root)
    climate = root / CLIMATE_DIR
    frame = build_training_frame(climate)
    model = EmosModel.from_json(climate.joinpath(*MODEL_REL))
    summary = None
    diag_path = climate / "reports" / "gtm_emos_diagnostics.json"
    if diag_path.exists():
        diag = json.loads(diag_path.read_text(encoding="utf-8"))
        if diag.get("summary"):
            summary = pd.DataFrame(diag["summary"])
    return score_snapshot(
        frame,
        model,
        ticks_root=root / TICKS_DIR,
        universe_json=universe_json,
        identity=snapshot_identity(root),
        summary=summary,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_analysis/test_spark_parity.py -q`
Expected: 28 passed. If `test_score_snapshot_strips_the_clock...` fails inside `run_paper`, compare the fixture against `tests/test_climate/test_staleness_gates.py::_sweep_rows` (the recorder-shaped rows the entrypoint tests use) and fix the fixture, not the module. (A throwaway probe on 2026-10-01 already drove `run_paper` with this exact fixture to `n_quotes=n_ladder=n_tradeable=1`, `sweep_ts_utc` `2026-09-28T22:08:22Z`, target month `2026-12`, `go_live.passed` False, no `paper/` dir.)

- [ ] **Step 5: Write the CLI and its test**

Create `scripts/spark/parity_score.py`:
```python
"""Parity probe: score a frozen quant-data snapshot with the operational model.

    python scripts/spark/parity_score.py --root ~/quant_data_snapshot --out ~/quant_data_snapshot/parity/zenbook.json
    python scripts/spark/parity_score.py --root ~/quant_data --out ~/parity_spark.json   # on the Spark

Research-only (design section 5.2): never writes paper/gtm_signals.json or
a history line, applies no staleness gate. Compare two outputs with
scripts/spark/compare_json.py --ignore identity.machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.spark_parity import score_snapshot_from_root  # noqa: E402

DEFAULT_UNIVERSE = REPO_ROOT / "data" / "ibkr_universe_live.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a frozen quant-data snapshot.")
    parser.add_argument("--root", required=True, help="quant-data root (holds ibkr_ticks/ and climate_data/)")
    parser.add_argument("--out", required=True, help="where to write the payload JSON")
    parser.add_argument("--universe-json", default=str(DEFAULT_UNIVERSE),
                        help="Stage-0 universe snapshot for con_id -> local_symbol labels")
    args = parser.parse_args(argv)
    payload = score_snapshot_from_root(
        Path(args.root).expanduser(), universe_json=Path(args.universe_json).expanduser()
    )
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(
        f"ladder={payload['n_ladder']} tradeable={payload['n_tradeable']} "
        f"fingerprint={payload['model_fingerprint']} sweep={payload['sweep_ts_utc']} -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Append to `tests/test_analysis/test_spark_cli.py` (add `import parity_score as score_cli  # noqa: E402` next to the other script imports, and `import analysis.spark_parity as sp  # noqa: E402` below them; the `noqa` is needed because the script-directory `sys.path` insert above them makes every later import an E402 for ruff):
```python
def test_parity_score_cli_writes_a_clockless_payload(
    spark_root, universe_json, dec_2026_frame, tmp_path, monkeypatch
):
    monkeypatch.setattr(sp, "build_training_frame", lambda root: dec_2026_frame)
    out = tmp_path / "parity" / "out.json"
    rc = score_cli.main([
        "--root", str(spark_root.root), "--out", str(out), "--universe-json", str(universe_json),
    ])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "ts_utc" not in payload
    assert payload["identity"]["snapshot"]["sweep_count"] == 1
    assert not (spark_root.root / "climate_data" / "paper").exists()
```

- [ ] **Step 6: Run, lint, commit**

```bash
source scripts/spark/env.sh
python -m pytest tests/test_analysis/test_spark_parity.py tests/test_analysis/test_spark_cli.py -q
python -m ruff check analysis scripts/spark tests/test_analysis
git add analysis/spark_parity.py scripts/spark/parity_score.py tests/test_analysis/
git commit -m "Add the clockless scoring probe for Spark parity" -m "$COAUTHOR"
git push
```
Expected: `33 passed` (28 + 5), ruff reporting no findings for the three paths.

---

### Task 6: The parity run and its report

**Files:**
- Modify: `docs/spark_parity_<date>.md` (sections 4-9)
- Create: `docs/spark_parity_<date>_zenbook.json`, `docs/spark_parity_<date>_spark.json` (the two compared payloads, committed as evidence)

**Interfaces:**
- Consumes: Tasks 2-5 on both machines at the same commit; the mirror from Task 3.
- Produces: the signed-off parity report with its registered tolerance.

- [ ] **Step 0: Register the tolerance from a kernel-variation experiment**

Why: the design assumed elementwise arithmetic on identical doubles. It is not: `build_training_frame` refits the ERA5->NOAA bridge with `np.linalg.lstsq` on each machine (`climate/bridge.py:142`), and LAPACK reduces sums in a kernel-dependent order. Measured on 2026-10-01 on a copy of the real snapshot (41 ladder rows, 1520 numeric leaves): identical runs agree exactly; Haswell vs Sandybridge/Nehalem OpenBLAS kernels differ by max rel 3.3e-10; a different solver (QR) by 1.0e-9. aarch64 NEON will sit in that band or above. So the gate is a REGISTERED tolerance measured before the cross-machine run, not 1e-12. Run (Zenbook, on the snapshot; the bundled OpenBLAS honours the kernel override):
```bash
source scripts/spark/env.sh
for core in HASWELL SANDYBRIDGE NEHALEM; do
  OPENBLAS_CORETYPE=$core python scripts/spark/parity_score.py --root ~/quant_data_snapshot --out ~/quant_data_snapshot/parity/kernel_$core.json | tail -1
done
python scripts/spark/compare_json.py ~/quant_data_snapshot/parity/kernel_HASWELL.json ~/quant_data_snapshot/parity/kernel_SANDYBRIDGE.json --ignore identity.machine --rtol 1e-8 --atol 1e-10 | sed -n '1p;$p'
python scripts/spark/compare_json.py ~/quant_data_snapshot/parity/kernel_HASWELL.json ~/quant_data_snapshot/parity/kernel_NEHALEM.json --ignore identity.machine --rtol 1e-8 --atol 1e-10 | sed -n '1p;$p'
```
Expected: three `ladder=...` lines with one fingerprint, then two `numeric leaves compared` lines with `max rel diff` around 1e-10 and two `PARITY OK`. Registered tolerance: `--rtol 1e-8 --atol 1e-10`, roughly 30x above the measured x86 kernel noise and six orders below the smallest price step that matters (tick 0.01, minimum edge 0.02); a threshold-crossing flip would surface as a structural True/False mismatch, never as hidden rounding. If the measured max rel diff exceeds 1e-9, STOP and put the numbers to the operator before registering anything looser. Paste the outputs and the registered values under `## 4. Registered tolerance (Task 6)`.

- [ ] **Step 1: Same commit on both machines, manifest the live root, fresh mirror**

Run:
```bash
source scripts/spark/env.sh
git status --porcelain --untracked-files=no | grep -v '^ M docs/' && echo DIRTY_CODE || echo CODE_CLEAN
git log --oneline -1
"$SSH" spark 'cd ~/work/algorithmic-trade && git pull --ff-only && git log --oneline -1'
python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/manifest_before_run.json
wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh"
```
Expected: `CODE_CLEAN` (uncommitted code would make `code_sha` a lie; commit first), the same SHA on both lines, and the push ending with the identity JSON. The identity compare in Step 3 asserts the SHA equality; nothing here is eyeballed. Record the SHA and the identity JSON under `## 5. Snapshot identity (Task 6)`.

- [ ] **Step 2: Score on both machines (before any refit)**

Run:
```bash
source scripts/spark/env.sh
python scripts/spark/parity_score.py --root ~/quant_data_snapshot --out ~/quant_data_snapshot/parity/zenbook.json
"$SSH" spark 'cd ~/work/algorithmic-trade && .venv/bin/python scripts/spark/parity_score.py --root ~/quant_data --out ~/parity_spark.json'
"$SCP" spark:parity_spark.json ~/quant_data_snapshot/parity/spark.json
```
Expected: both print a `ladder=... fingerprint=... sweep=...` line with the same fingerprint and sweep timestamp. (`~/parity_spark.json` on the Spark is scratch; the copy under `parity/` is the evidence, and Step 7 commits it.)

- [ ] **Step 3: Compare (the gate)**

Run (the registered tolerance from Step 0):
```bash
python scripts/spark/compare_json.py ~/quant_data_snapshot/parity/zenbook.json ~/quant_data_snapshot/parity/spark.json --ignore identity.machine --rtol 1e-8 --atol 1e-10
```
Expected: `PARITY OK`, with the printed `max rel diff` inside the registered tolerance (record it; 1e-10..1e-9 is the expected band). `identity.snapshot` is NOT ignored, so the same line proves both machines scored the same bytes (`inputs_sha256`, `newest_sweep_sha256`, `labels_sha256`) at the same commit (`code_sha`). Paste the full output under `## 6. Scoring parity (Task 6)`. A mismatch is investigated before anything else happens: a differing `identity.snapshot` means the push raced the recorder or the commits differ (re-push or pull, re-score); a numeric mismatch confined to `bridge.*` and its dependants is the LAPACK fit diverging beyond the registered band; anything else is a real platform divergence. Both go to the operator with the offending paths.

- [ ] **Step 4: Refit on both machines (report-only)**

Run (the Zenbook writes into the SNAPSHOT copy, never the live root; the first line refuses to continue if the snapshot is missing, so a typo cannot fall back to the live default):
```bash
source scripts/spark/env.sh
test -f ~/quant_data_snapshot/climate_data/models/gtm_emos.json || { echo NO_SNAPSHOT; exit 1; }
python scripts/fit_gtm_emos.py --trend --root ~/quant_data_snapshot/climate_data 2>&1 | tail -5
"$SSH" spark 'cd ~/work/algorithmic-trade && .venv/bin/python scripts/fit_gtm_emos.py --trend --root ~/quant_data/climate_data 2>&1 | tail -5'
"$SCP" spark:quant_data/climate_data/models/gtm_emos.json ~/quant_data_snapshot/parity/gtm_emos_spark.json
"$SCP" spark:quant_data/climate_data/reports/gtm_emos_diagnostics.json ~/quant_data_snapshot/parity/diagnostics_spark.json
python scripts/spark/compare_json.py ~/quant_data_snapshot/climate_data/models/gtm_emos.json ~/quant_data_snapshot/parity/gtm_emos_spark.json --rtol 1e-6 --atol 1e-9 --ignore fingerprint
python scripts/spark/compare_json.py ~/quant_data_snapshot/climate_data/reports/gtm_emos_diagnostics.json ~/quant_data_snapshot/parity/diagnostics_spark.json --rtol 1e-6 --atol 1e-9 --ignore fitted_at_utc artifact_paths fingerprint
```
Expected: max abs/rel diffs in the 1e-8..1e-6 band (x86 AVX vs aarch64 NEON reduction order through scipy's optimiser; `max_rel` is dominated by near-zero coefficients, so read the per-leaf lines). `fingerprint` is ignored on both compares because `climate.fit.write_artifacts` merges the fit's fingerprint into fresh model and diagnostics files, and any last-bit coefficient difference changes that string; `fitted_at_utc` is a clock and `artifact_paths` are machine paths. Report-only: a `PARITY FAILED` here does not block, but any coefficient differing by more than 1e-4 relative is investigated before sign-off. Paste both outputs under `## 7. Refit parity, report-only (Task 6)`.

- [ ] **Step 5: Suite and lint, same day, both machines**

Run (the Spark suite under an isolated HOME so its `polymarket_weather` writes stay out of the mirror):
```bash
source scripts/spark/env.sh
python -m pytest -q 2>&1 | tail -3; echo "pytest_exit=${PIPESTATUS[0]}"
python -m ruff check . --statistics --no-cache 2>&1 | tail -3
git diff --name-only origin/main...HEAD -- '*.py' | xargs python -m ruff check && echo TOUCHED_FILES_LINT_OK
"$SSH" spark 'cd ~/work/algorithmic-trade && HOME=$(mktemp -d) .venv/bin/python -m pytest -q 2>&1 | tail -3; echo "pytest_exit=${PIPESTATUS[0]}"; .venv/bin/python -m ruff check . --statistics --no-cache 2>&1 | tail -3; git diff --name-only origin/main...HEAD -- "*.py" | xargs -r .venv/bin/python -m ruff check && echo TOUCHED_FILES_LINT_OK'
```
Expected: equal `passed` counts, `pytest_exit=0` twice, the same `Found N errors.` baseline total twice, `TOUCHED_FILES_LINT_OK` twice. Paste under `## 8. Suite and lint (Task 6)`.

- [ ] **Step 6: Prove the live root was untouched by the whole run**

Run:
```bash
python scripts/spark/live_manifest.py --root ~/quant_data --out ~/quant_data_snapshot/parity/manifest_after_run.json
diff ~/quant_data_snapshot/parity/manifest_before_run.json ~/quant_data_snapshot/parity/manifest_after_run.json && echo LIVE_ROOT_UNTOUCHED
```
Expected: `LIVE_ROOT_UNTOUCHED`. The operational model, the diagnostics, the paper book and the ledgers on the Zenbook have the same bytes as before Step 1; the refit in Step 4 only touched the snapshot. A diff names exactly which file changed and when (a scheduled task in the window is the benign explanation; anything else stops the sign-off).

- [ ] **Step 7: Evidence, verdict, commit**

Copy the two compared payloads next to the report and write the verdict:
```bash
source scripts/spark/env.sh
cp ~/quant_data_snapshot/parity/zenbook.json "docs/spark_parity_${RUN_DATE}_zenbook.json"
cp ~/quant_data_snapshot/parity/spark.json "docs/spark_parity_${RUN_DATE}_spark.json"
```
Add `## 9. Verdict` to the report: one paragraph stating whether scoring parity passed at the registered tolerance (the gate) and the max relative difference observed, the refit's max differences (informational), the suite counts and lint totals, the commit SHA, `LIVE_ROOT_UNTOUCHED`, and the Python plus numpy/scipy/pandas/pyarrow versions from both `identity.machine` blocks. The Zenbook copy is the runtime and is never deleted under Option B; this report is what "parity verified" means. Then:
```bash
git add docs/spark_parity_*
git commit -m "Record Spark scoring parity against the Zenbook snapshot" -m "$COAUTHOR"
git push
```

---

### Task 7: `src/llm/settings.py`

**Files:**
- Create: `src/llm/__init__.py`
- Create: `src/llm/settings.py`
- Create: `tests/test_llm/__init__.py`
- Create: `tests/test_llm/test_settings.py`

**Interfaces:**
- Produces: `LocalLLMSettings(BaseSettings)` with fields `model: str` (required), `base_url: str = "http://127.0.0.1:8000/v1"`, `api_key: str = "EMPTY"`, `max_tokens: int = 8192`, `reasoning_effort: Literal["low", "medium", "high"] = "low"`, `temperature: float | None = None`, `connect_timeout_s: float = 10.0`, `read_timeout_s: float = 900.0`; env prefix `LLM_`, env file `.env`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm/__init__.py` (empty) and `tests/test_llm/conftest.py` (an exported `LLM_BASE_URL` or `LLM_MAX_TOKENS` in the operator's shell would otherwise silently change the defaults these tests assert):
```python
"""Isolate the LLM tests from the operator's shell environment."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_") and key != "LLM_LIVE":
            monkeypatch.delenv(key, raising=False)
```
Then `tests/test_llm/test_settings.py`:
```python
"""LocalLLMSettings: LLM_ env prefix, required model, defaults from the Spark handover."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.llm.settings import LocalLLMSettings


def test_model_is_required_and_has_no_default(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(ValidationError):
        LocalLLMSettings(_env_file=None)


def test_env_prefix_and_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    settings = LocalLLMSettings(_env_file=None)
    assert settings.model == "qwen-test"
    assert settings.base_url == "http://127.0.0.1:8000/v1"
    assert settings.api_key == "EMPTY"
    assert settings.max_tokens == 8192
    assert settings.reasoning_effort == "low"
    assert settings.temperature is None
    assert settings.connect_timeout_s == 10.0
    assert settings.read_timeout_s == 900.0


def test_reasoning_effort_is_validated(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "max")
    with pytest.raises(ValidationError):
        LocalLLMSettings(_env_file=None)


def test_unrelated_env_is_ignored(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv("LLM_SOMETHING_ELSE", "x")
    assert LocalLLMSettings(_env_file=None).model == "qwen-test"


def test_empty_env_values_mean_unset(monkeypatch):
    # .env.example documents "LLM_TEMPERATURE=" as "use the server default";
    # without env_ignore_empty an empty string is a ValidationError on a float.
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv("LLM_TEMPERATURE", "")
    monkeypatch.setenv("LLM_MAX_TOKENS", "")
    settings = LocalLLMSettings(_env_file=None)
    assert settings.temperature is None
    assert settings.max_tokens == 8192
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_llm/test_settings.py -q`
Expected: `ModuleNotFoundError: No module named 'src.llm'`.

- [ ] **Step 3: Implement**

Create `src/llm/__init__.py`:
```python
"""Local LLM access (vLLM on the DGX Spark, OpenAI-compatible HTTP).

Role decision 2026-09-29 (docs/plans/2026-09-30-spark-research-split-design.md,
section 6): the LLM is a data processor -- text in, structured data out --
never the predictor. The numeric model prices;
tests/test_architecture/test_llm_boundary.py keeps this package out of
climate/, src/trading/, src/venues/, src/edge/, src/domain/, monitoring/
and the ordering scripts.
"""

from src.llm.client import (
    LLMConfigError,
    LLMError,
    LLMHTTPError,
    LLMOutputError,
    LLMReply,
    LocalLLMClient,
)
from src.llm.settings import LocalLLMSettings

__all__ = [
    "LLMConfigError",
    "LLMError",
    "LLMHTTPError",
    "LLMOutputError",
    "LLMReply",
    "LocalLLMClient",
    "LocalLLMSettings",
]
```
Note: `src/llm/client.py` does not exist until Task 8; to keep Task 7's tests green on their own, create `__init__.py` in Task 7 with ONLY the docstring and `from src.llm.settings import LocalLLMSettings` plus `__all__ = ["LocalLLMSettings"]`, and replace it with the full block above in Task 8 Step 3.

Create `src/llm/settings.py`:
```python
"""Settings for the local (vLLM, OpenAI-compatible) LLM.

``model`` is REQUIRED with no default: the served model name comes from
configuration, never from code (Spark handover decision), and
``LocalLLMClient.assert_model_served`` checks it against what the server
actually lists. Defaults mirror the measured request rules from the Spark
handover: ``max_tokens`` >= 8192 (the reasoning model needs room),
``reasoning_effort`` low (the default effort burns ~1.6k reasoning tokens
per call), a long read timeout (~15 tok/s single-stream, plus queueing).

Same pattern as ``src/venues/ibkr/settings.py``: pydantic-settings with an
env prefix, ``.env`` as the file source, unknown keys ignored.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class LocalLLMSettings(BaseSettings):
    # env_ignore_empty: an empty "LLM_TEMPERATURE=" line means "unset", as
    # .env.example documents, instead of a ValidationError on a float field.
    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # REQUIRED, no default: the id the server lists under /v1/models.
    model: str
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = "EMPTY"  # vLLM ignores it; sent as a bearer token for API parity
    max_tokens: int = 8192
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    temperature: float | None = None  # None -> omitted from the request (server default)
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 900.0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_llm/test_settings.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
source scripts/spark/env.sh
python -m ruff check src/llm tests/test_llm
git add src/llm tests/test_llm
git commit -m "Add LocalLLMSettings with a required, config-driven model name" -m "$COAUTHOR"
```

---

### Task 8: `src/llm/client.py`

**Files:**
- Create: `src/llm/client.py`
- Modify: `src/llm/__init__.py` (full export block from Task 7)
- Create: `tests/test_llm/test_client.py`

**Interfaces:**
- Consumes: `LocalLLMSettings` (Task 7).
- Produces: `LocalLLMClient(settings, transport=None)` with `models() -> list[str]`, `assert_model_served() -> None`, `chat(messages, *, json_schema=None, schema_name="reply", max_tokens=None) -> LLMReply`, context-manager `close()`; `LLMReply(content, reasoning, parsed, usage, latency_s, model)`; errors `LLMError`, `LLMHTTPError(status, body)`, `LLMOutputError(message, raw)`, `LLMConfigError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm/test_client.py`:
```python
"""LocalLLMClient against an httpx.MockTransport: request shape, parsing, errors.

No network. The handler records the request the client sent and answers
with the vLLM/OpenAI chat-completions shape (content + reasoning_content).
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.llm.client import (
    LLMConfigError,
    LLMHTTPError,
    LLMOutputError,
    LocalLLMClient,
)
from src.llm.settings import LocalLLMSettings


def _settings(**overrides) -> LocalLLMSettings:
    base = dict(model="qwen-test", base_url="http://llm.test/v1")
    base.update(overrides)
    return LocalLLMSettings(_env_file=None, **base)


def _completion(content: str, reasoning: str | None = "thinking") -> dict:
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "model": "qwen-test",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


def _client(handler, **overrides) -> LocalLLMClient:
    return LocalLLMClient(_settings(**overrides), transport=httpx.MockTransport(handler))


def test_chat_sends_configured_fields_and_parses_a_schema_reply():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion('{"answer": 42}'))

    with _client(handler) as client:
        reply = client.chat([{"role": "user", "content": "hi"}], json_schema={"type": "object"})

    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer EMPTY"
    body = seen["body"]
    assert body["model"] == "qwen-test"
    assert body["max_tokens"] == 8192
    assert body["reasoning_effort"] == "low"
    assert "temperature" not in body
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "reply", "schema": {"type": "object"}},
    }
    assert reply.content == '{"answer": 42}'
    assert reply.parsed == {"answer": 42}
    assert reply.reasoning == "thinking"
    assert reply.usage["completion_tokens"] == 5
    assert reply.usage["reasoning_tokens"] == 3
    assert reply.model == "qwen-test"
    assert reply.latency_s >= 0.0


def test_chat_without_schema_sends_no_response_format_and_leaves_parsed_none():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("plain text", reasoning=None))

    reply = _client(handler, temperature=0.2, max_tokens=64).chat(
        [{"role": "user", "content": "hi"}], max_tokens=32
    )
    assert "response_format" not in seen["body"]
    assert seen["body"]["temperature"] == 0.2
    assert seen["body"]["max_tokens"] == 32  # per-call override wins
    assert reply.parsed is None
    assert reply.reasoning is None


def test_chat_with_schema_raises_output_error_carrying_the_raw_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("not json at all"))

    with pytest.raises(LLMOutputError) as info:
        _client(handler).chat([{"role": "user", "content": "hi"}], json_schema={"type": "object"})
    assert info.value.raw == "not json at all"


def test_http_error_is_surfaced_with_status_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(LLMHTTPError) as info:
        _client(handler).chat([{"role": "user", "content": "hi"}])
    assert info.value.status == 500
    assert "boom" in info.value.body


def test_models_lists_served_ids_and_assert_model_served_checks_config():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "other-model"}, {"id": "qwen-test"}]})

    client = _client(handler)
    assert client.models() == ["other-model", "qwen-test"]
    client.assert_model_served()  # configured model is listed: no raise

    with pytest.raises(LLMConfigError) as info:
        _client(handler, model="not-served").assert_model_served()
    assert "other-model" in str(info.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_llm/test_client.py -q`
Expected: `ModuleNotFoundError: No module named 'src.llm.client'`.

- [ ] **Step 3: Implement**

Create `src/llm/client.py`:
```python
"""Thin OpenAI-compatible chat client for the local vLLM server, on httpx.

No retries (callers decide), no streaming (not needed for text -> structured
data). Structured output uses the server's ``response_format`` json_schema
mode; the decoded object is returned as ``LLMReply.parsed``. Reasoning text
(``reasoning_content``, produced by vLLM's qwen3 reasoning parser) is kept
separate from ``content`` so callers never accidentally parse it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import httpx

from src.llm.settings import LocalLLMSettings


class LLMError(RuntimeError):
    """Base class for every failure raised by LocalLLMClient."""


class LLMHTTPError(LLMError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class LLMOutputError(LLMError):
    """A schema was requested but the content is not valid JSON; ``raw`` keeps the text."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class LLMConfigError(LLMError):
    """LLM_MODEL does not match what the server serves."""


@dataclass
class LLMReply:
    content: str
    reasoning: str | None
    parsed: Any | None
    usage: dict[str, int]
    latency_s: float
    model: str


class LocalLLMClient:
    def __init__(
        self, settings: LocalLLMSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._http = httpx.Client(
            base_url=settings.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            transport=transport,
        )

    # -- lifecycle --------------------------------------------------------- #

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LocalLLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport --------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LLMError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise LLMHTTPError(response.status_code, response.text)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(f"{method} {path}: non-JSON body: {response.text[:200]}") from exc
        if not isinstance(data, dict):
            raise LLMError(f"{method} {path}: expected a JSON object, got {type(data).__name__}")
        return data

    # -- API ------------------------------------------------------------------ #

    def models(self) -> list[str]:
        """Ids the server lists under /models."""
        data = self._request("GET", "/models")
        return [str(item["id"]) for item in data.get("data", []) if item.get("id")]

    def assert_model_served(self) -> None:
        """Config over hardcode, made checkable: LLM_MODEL must be a served id."""
        served = self.models()
        if self.settings.model not in served:
            raise LLMConfigError(
                f"LLM_MODEL={self.settings.model!r} is not served; the server lists {served}"
            )

    def chat(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "reply",
        max_tokens: int | None = None,
    ) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens or self.settings.max_tokens,
            "reasoning_effort": self.settings.reasoning_effort,
        }
        if self.settings.temperature is not None:
            body["temperature"] = self.settings.temperature
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": dict(json_schema)},
            }
        started = time.perf_counter()
        data = self._request("POST", "/chat/completions", json=body)
        latency = time.perf_counter() - started
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed completion: {json.dumps(data)[:300]}") from exc
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content")
        usage_raw = data.get("usage") or {}
        usage = {k: int(v) for k, v in usage_raw.items() if isinstance(v, (int, float))}
        details = usage_raw.get("completion_tokens_details") or {}
        if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), (int, float)):
            usage["reasoning_tokens"] = int(details["reasoning_tokens"])
        parsed: Any | None = None
        if json_schema is not None:
            try:
                parsed = json.loads(content)
            except ValueError as exc:
                raise LLMOutputError(f"content is not valid JSON: {exc}", raw=content) from exc
        return LLMReply(
            content=content,
            reasoning=reasoning if isinstance(reasoning, str) else None,
            parsed=parsed,
            usage=usage,
            latency_s=latency,
            model=str(data.get("model", "")),
        )
```
Then replace `src/llm/__init__.py` with the full export block shown in Task 7 Step 3.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_llm -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
source scripts/spark/env.sh
python -m ruff check src/llm tests/test_llm
git add src/llm tests/test_llm
git commit -m "Add the httpx client for the local vLLM model with schema-constrained replies" -m "$COAUTHOR"
```

---

### Task 9: The LLM boundary test and the `.env.example` block

**Files:**
- Create: `tests/test_architecture/test_llm_boundary.py`
- Modify: `.env.example` (append after the IBKR block)

**Interfaces:**
- Produces: a red test whenever `src.llm` is imported under the guarded paths.

- [ ] **Step 1: Write the test (it must pass immediately on the clean tree and catch a planted import)**

Create `tests/test_architecture/test_llm_boundary.py`:
```python
"""Architecture guard: the LLM never enters the pricing or ordering path.

Role decision 2026-09-29 (docs/plans/2026-09-30-spark-research-split-design.md,
section 6): the LLM is a data processor, the numeric model predicts. Pure-AST
scan of the guarded directories and scripts, same approach as
test_import_bans.py. Three self-tests keep the guard honest: every configured
path must contribute files, a planted import in a temp tree must be
reported, and every import form (absolute, aliased, relative) must be seen.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

GUARDED_DIRS = (
    ("climate",),
    ("src", "trading"),
    ("src", "venues"),
    ("src", "edge"),
    ("src", "domain"),
    ("monitoring",),
)
GUARDED_FILES = (
    ("scripts", "run_ibkr_paper.py"),
    ("scripts", "sync_ibkr_paper.py"),
    ("scripts", "record_ibkr_ticks.py"),
    ("scripts", "run_gtm_paper.py"),
)
LLM_PACKAGE = "src.llm"


def _names_llm(module: str) -> bool:
    return module == LLM_PACKAGE or module.startswith(LLM_PACKAGE + ".")


def llm_imports(tree: ast.AST, package: tuple[str, ...] = ()) -> list[int]:
    """Line numbers of every import that binds anything from src.llm.

    ``package`` is the scanned file's package (its directory relative to
    the repo root); relative imports such as ``from ..llm import client``
    inside ``src/trading/x.py`` resolve against it.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_names_llm(alias.name) for alias in node.names):
                hits.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module or ""
            else:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                module = ".".join([*base, node.module]) if node.module else ".".join(base)
            imports_llm_name = module == "src" and any(a.name == "llm" for a in node.names)
            if _names_llm(module) or imports_llm_name:
                hits.append(node.lineno)
    return hits


def guarded_files(root: Path) -> dict[tuple[str, ...], list[Path]]:
    """Scanned files per configured entry; an entry with no files is a guard failure."""
    found: dict[tuple[str, ...], list[Path]] = {}
    for parts in GUARDED_DIRS:
        top = root.joinpath(*parts)
        found[parts] = sorted(top.rglob("*.py")) if top.is_dir() else []
    for parts in GUARDED_FILES:
        path = root.joinpath(*parts)
        found[parts] = [path] if path.is_file() else []
    return found


def violations(root: Path) -> list[str]:
    out: list[str] = []
    for files in guarded_files(root).values():
        for path in files:
            rel = path.relative_to(root)
            package = tuple(rel.parts[:-1])
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source, filename=rel.as_posix())
            except SyntaxError:
                out.append(f"{rel.as_posix()}: unparseable, cannot be guarded")
                continue
            out.extend(
                f"{rel.as_posix()}:{lineno}: imports {LLM_PACKAGE}"
                for lineno in llm_imports(tree, package)
            )
    return out


def test_llm_never_imported_in_pricing_or_ordering_path() -> None:
    found = violations(REPO_ROOT)
    assert not found, (
        "the LLM is a data processor, never the predictor (design section 6):\n"
        + "\n".join(found)
    )


def test_every_guarded_path_contributes_files() -> None:
    empty = [parts for parts, files in guarded_files(REPO_ROOT).items() if not files]
    assert not empty, f"guard configured for paths that no longer exist: {empty}"


def test_a_planted_import_in_a_temp_tree_is_reported(tmp_path: Path) -> None:
    (tmp_path / "climate").mkdir()
    (tmp_path / "climate" / "clean.py").write_text("import json\n", encoding="utf-8")
    (tmp_path / "climate" / "planted.py").write_text(
        "from src.llm.client import LocalLLMClient\n", encoding="utf-8"
    )
    (tmp_path / "src" / "trading").mkdir(parents=True)
    (tmp_path / "src" / "trading" / "relative.py").write_text(
        "from ..llm import client\n", encoding="utf-8"
    )
    assert violations(tmp_path) == [
        "climate/planted.py:1: imports src.llm",
        "src/trading/relative.py:1: imports src.llm",
    ]


def test_scanner_recognises_every_import_form() -> None:
    source = (
        "import src.llm\n"
        "import src.llm.client as c\n"
        "from src.llm import LocalLLMClient\n"
        "from src.llm.settings import LocalLLMSettings\n"
        "from src import llm\n"
        "from src.venues.base import VenueAdapter\n"
        "from . import something\n"
    )
    assert llm_imports(ast.parse(source), package=("src", "trading")) == [1, 2, 3, 4, 5]
```

- [ ] **Step 2: Run the tests**

Run: `python -m pytest tests/test_architecture -q`
Expected: all passed (the existing import-ban test plus 4 new).

- [ ] **Step 3: Prove the guard bites on the real tree too**

Run (plant, test, unplant; pytest prints only the assertion's first line in its summary, so grep for the path):
```bash
printf 'from src.llm import LocalLLMClient  # planted\n' >> climate/paths.py
python -m pytest tests/test_architecture/test_llm_boundary.py -q 2>&1 | grep -nE 'climate/paths.py|passed|failed'
git checkout -- climate/paths.py
git status --short climate/paths.py
```
Expected: a line `climate/paths.py:NN: imports src.llm`, a `1 failed, 3 passed` summary, then a clean `climate/paths.py`.

- [ ] **Step 4: Document the settings in `.env.example`**

Append to `.env.example` (after the IBKR block) with the Edit tool:
```
# Local LLM (vLLM on the DGX Spark, OpenAI-compatible; src/llm/settings.py).
# Role decision 2026-09-29: the LLM is a data processor (text -> structured
# data), never the predictor -- tests/test_architecture/test_llm_boundary.py
# enforces it. On the Spark the .env holds ONLY the LLM_MODEL line.
# LLM_MODEL=                      # REQUIRED where the client is used: the id the server
#                                 # lists under /v1/models (never hardcode; llm_smoke.py checks)
# LLM_BASE_URL=http://127.0.0.1:8000/v1
#                                 # on the Spark; from the Zenbook open a tunnel first:
#                                 #   ssh -L 8000:127.0.0.1:8000 spark
# LLM_API_KEY=EMPTY               # vLLM ignores it
# LLM_MAX_TOKENS=8192             # keep >= 8192: the reasoning model needs the room
# LLM_REASONING_EFFORT=low        # the default effort burns ~1.6k reasoning tokens per call
# LLM_TEMPERATURE=                # empty = server default
# LLM_READ_TIMEOUT_S=900          # ~15 tok/s single-stream; cold start is 11-14 min -- warm
#                                 # the sidecar first on the Spark:
#                                 #   cat /opt/models/Qwen3.8-Flash-Next-NVFP4/ple-sidecar/ples_nvfp4/* > /dev/null
```

- [ ] **Step 5: Commit**

```bash
source scripts/spark/env.sh
python -m ruff check tests/test_architecture
git add tests/test_architecture/test_llm_boundary.py .env.example
git commit -m "Guard the pricing and ordering path against LLM imports; document LLM_ settings" -m "$COAUTHOR"
```

---

### Task 10: `llm_smoke.py`, the live test, and a real round-trip on the Spark

**Files:**
- Create: `scripts/llm_smoke.py`
- Create: `tests/test_llm/test_live.py`
- Modify: `docs/spark_parity_<date>.md` (section 9)

**Interfaces:**
- Consumes: `LocalLLMClient`, `LocalLLMSettings`.
- Produces: `scripts/llm_smoke.py` `main(argv) -> int`; `tests/test_llm/test_live.py` (skipped unless `LLM_LIVE=1`).

- [ ] **Step 1: Write the smoke script**

Create `scripts/llm_smoke.py`:
```python
"""Is the local model up, and is LLM_MODEL what it actually serves?

    python scripts/llm_smoke.py                  # on the Spark
    ssh -L 8000:127.0.0.1:8000 spark             # from the Zenbook: tunnel first, then run

Exit 0 only when: the configured model is served, one schema-constrained
extraction round-trips as JSON, and the parsed object has every required
key. Prints latency, tokens/s and the reasoning-token count so a change in
serving flags (reasoning_effort, MTP) shows up here first.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.llm.client import LLMError, LocalLLMClient  # noqa: E402
from src.llm.settings import LocalLLMSettings  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "year": {"type": "integer"},
        "passed": {"type": "boolean"},
    },
    "required": ["city", "year", "passed"],
    "additionalProperties": False,
}
PARAGRAPH = (
    "In 2024 the city council of Izmir voted on extending the tram line to the "
    "university campus; the motion passed with 31 votes in favour."
)
PROMPT = (
    "Extract the city, the year, and whether the motion passed. "
    "Answer with a JSON object only."
)


def main(argv: list[str] | None = None) -> int:
    settings = LocalLLMSettings()
    print(f"base_url={settings.base_url} model={settings.model} effort={settings.reasoning_effort}")
    try:
        with LocalLLMClient(settings) as client:
            print("served:", client.models())
            client.assert_model_served()
            reply = client.chat(
                [
                    {"role": "system", "content": "You extract structured data from text."},
                    {"role": "user", "content": f"{PROMPT}\n\n{PARAGRAPH}"},
                ],
                json_schema=SCHEMA,
                schema_name="extraction",
            )
    except LLMError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    completion = reply.usage.get("completion_tokens", 0)
    rate = completion / reply.latency_s if reply.latency_s > 0 else 0.0
    print(
        f"latency {reply.latency_s:.1f}s, completion tokens {completion}, "
        f"{rate:.1f} tok/s, reasoning tokens {reply.usage.get('reasoning_tokens', 'n/a')}"
    )
    print("parsed:", reply.parsed)
    missing = [k for k in SCHEMA["required"] if k not in (reply.parsed or {})]
    if missing:
        print(f"FAIL: missing keys {missing}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write the live test**

Create `tests/test_llm/test_live.py`:
```python
"""Live round-trip against a reachable vLLM server. Skipped unless LLM_LIVE=1.

Run on the Spark (or through the tunnel) with the repo's .env providing
LLM_MODEL:  LLM_LIVE=1 python -m pytest tests/test_llm/test_live.py -q
"""

from __future__ import annotations

import os

import pytest

from src.llm.client import LocalLLMClient
from src.llm.settings import LocalLLMSettings

pytestmark = pytest.mark.skipif(
    os.environ.get("LLM_LIVE") != "1", reason="set LLM_LIVE=1 with a reachable vLLM server"
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_live_schema_round_trip():
    settings = LocalLLMSettings()
    with LocalLLMClient(settings) as client:
        client.assert_model_served()
        reply = client.chat(
            [{"role": "user", "content": "Is water wet? Answer as JSON with key answer: yes or no."}],
            json_schema=SCHEMA,
            max_tokens=2048,
        )
    assert reply.parsed["answer"] in ("yes", "no")
    assert reply.usage.get("completion_tokens", 0) > 0
```

- [ ] **Step 3: Confirm the live test skips locally and lint**

Run:
```bash
python -m pytest tests/test_llm -q
python -m ruff check scripts/llm_smoke.py tests/test_llm
```
Expected: `10 passed, 1 skipped`, ruff reporting no findings for the two paths.

- [ ] **Step 4: Commit, push, run on the Spark**

```bash
source scripts/spark/env.sh
git add scripts/llm_smoke.py tests/test_llm/test_live.py
git commit -m "Add the local-LLM smoke check and a gated live round-trip test" -m "$COAUTHOR"
git push
"$SSH" spark 'cd ~/work/algorithmic-trade && git pull --ff-only && .venv/bin/python scripts/llm_smoke.py'
"$SSH" spark 'cd ~/work/algorithmic-trade && LLM_LIVE=1 .venv/bin/python -m pytest tests/test_llm/test_live.py -q 2>&1 | tail -2'
```
Expected: `served: [...]` containing `LLM_MODEL`, a latency/tok-s line, `parsed: {'city': 'Izmir', 'year': 2024, 'passed': True}`, `OK`; then `1 passed`. If the server is down, the smoke prints `FAIL: GET /models: ConnectError...` and exits 1: the container start is the operator's call, exactly as in Task 1 Step 6 (ask, warm the sidecar, poll through the cold start), then re-run. Paste both outputs under `## 10. Local LLM smoke (Task 10)` and commit the report with `-m "$COAUTHOR"`.

---

### Task 11: Runbook, role table, checkpoint pointer, memory, final suites, PR

**Files:**
- Create: `docs/spark_runbook.md`
- Modify: `docs/ibkr_runbook.md` (the role table, lines 65-69)
- Modify: `docs/MIGRATION_CHECKPOINT.md` (append section 9)
- Modify: memory `C:\Users\asus\.claude\projects\C--Users-asus-OneDrive-Desktop-new-work\memory\dgx-spark.md`
- Modify: `docs/spark_parity_<date>.md` (section 10)

- [ ] **Step 1: Write the runbook**

Create `docs/spark_runbook.md`:
```markdown
# DGX Spark runbook -- research mirror

Design: `docs/plans/2026-09-30-spark-research-split-design.md`. Parity evidence:
the newest `docs/spark_parity_*.md` with its `_zenbook.json` / `_spark.json`
payloads. This page is the operator's view.

## 1. What the Spark is, and is not

The Spark (`spark-8f47`, aarch64, 128 GB unified memory) runs the research
half of this repo and the local vLLM model. It is NOT a trading machine:

| Role | clientId | Machine |
| --- | --- | --- |
| `live_trading` | 1 | Zenbook (`scripts/run_ibkr_paper.py`, operator-run) |
| `data_collector` | 2 | Zenbook, Task Scheduler `\IBKR\IBKR Recorder` |
| `research` | 3 | Zenbook, ad hoc (not the Spark in this phase) |
| `paper_sync` | 4 | Zenbook, Task Scheduler `\IBKR\IBKR Paper Sync` |

Why the Spark cannot trade even by accident: its `.env` and shell carry no
`IBKR_PORT` / `IBKR_TRADING_MODE`, so every Gateway-touching entrypoint
fails at startup with a `ValidationError`; and the Gateway's API is bound to
localhost on the Zenbook, so there is nothing on the LAN to connect to.

Never run on the Spark: `record_ibkr_ticks.py`, `sync_ibkr_paper.py`,
`run_ibkr_paper.py`, `backfill_ibkr_hourly.py`, `verify_ibkr_entitlements.py`,
`collect_climate_labels.py` (the NOAA ledger is canonical on the Zenbook),
`fetch_climate_forecasts.py` (no `~/.cdsapirc` on the Spark).

## 2. Layout on the Spark

- Repo: `~/work/algorithmic-trade` (tracks `main` after the merge), venv
  `.venv` (uv-managed Python 3.14, pinned by the newest `constraints/zenbook-*.txt`).
- Data: `~/quant_data` -- a MIRROR pushed from the Zenbook (section 3). Same
  default root as the Zenbook on purpose; no `IBKR_DATA_ROOT` /
  `CLIMATE_DATA_ROOT` overrides anywhere.
- `.env`: exactly one line, `LLM_MODEL=<served id>`. No API keys.
- LLM stack: `~/stack/docker-compose.yml` (repo `curasinan/spark-llm-stack`);
  vLLM on `127.0.0.1:8000`, Open WebUI 8080, Portainer 9000, Phoenix 6006/4317,
  all loopback-only behind ufw (22/tcp is the only open port).

## 3. Pushing data (Zenbook -> Spark, never back)

On the Zenbook, outside 17:25-17:40 local (the paper-sync task writes the
SQLite DB then). PowerShell:

    wsl -d Ubuntu -- bash /mnt/c/Users/asus/OneDrive/Desktop/new_work/scripts/spark/push_snapshot.sh

Git Bash (MSYS would rewrite a bare `/mnt/...` argument into a Windows path;
`env.sh` defines `REPO_WSL` with the `//mnt` prefix that survives):

    source scripts/spark/env.sh
    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh"

It freezes `~/quant_data` into `~/quant_data_snapshot` (minus
`climate_data/raw/`, 7.6 GB of re-downloadable CDS files; add `--include-raw`
to ship it once; the recorder's `state/`, `*.tmp` and SQLite journals are
skipped), rsyncs the snapshot to `spark:quant_data`, and runs
`scripts/spark/verify_mirror.py` on the Spark, which exits 1 naming every
unreadable file. A push that lands inside the recorder's sub-second parquet
write copies a torn sweep: just run it again. To prove a push left the live
root alone, bracket it with `scripts/spark/live_manifest.py --root ~/quant_data`
before and after and `diff` the two files (parity report, section 3).

Rules: nothing is ever copied from the Spark into `~/quant_data` on the
Zenbook. Spark-side outputs (refits, parity payloads, experiments) are
ephemeral; the next push overwrites any canonical file they touched. The
operational `models/gtm_emos.json` and `paper/gtm_signals.json` are produced
on the Zenbook only.

## 4. Parity check (repeat after any numerical-stack change on either side)

    # Zenbook, Git Bash, from the repo; both machines at the same commit
    source scripts/spark/env.sh
    wsl.exe -d Ubuntu -- bash "$REPO_WSL/scripts/spark/push_snapshot.sh"
    python scripts/spark/parity_score.py --root ~/quant_data_snapshot --out ~/quant_data_snapshot/parity/zenbook.json
    "$SSH" spark 'cd ~/work/algorithmic-trade && .venv/bin/python scripts/spark/parity_score.py --root ~/quant_data --out ~/parity_spark.json'
    "$SCP" spark:parity_spark.json ~/quant_data_snapshot/parity/spark.json
    python scripts/spark/compare_json.py ~/quant_data_snapshot/parity/zenbook.json ~/quant_data_snapshot/parity/spark.json --ignore identity.machine --rtol 1e-8 --atol 1e-10

`PARITY OK` at the registered tolerance is the gate; the `identity.snapshot`
block proves both machines scored the same bytes at the same commit. The
tolerance is not 1e-15 because the frame's ERA5->NOAA bridge is refit with
LAPACK on each machine and reduction order differs by CPU kernel (measured
x86 kernel-to-kernel noise: 3e-10 relative); 1e-8 is still six orders below
the smallest price step that matters. A refit comparison
(`scripts/fit_gtm_emos.py --trend --root <root>/climate_data` on both, then
`compare_json.py --rtol 1e-6 --atol 1e-9 --ignore fingerprint`) is
report-only: the optimiser amplifies the same kernel differences, and fresh
artifacts carry a fingerprint string that changes with any last bit.

## 5. Using the local model

- From the Zenbook: `ssh -L 8000:127.0.0.1:8000 spark`, then the default
  `LLM_BASE_URL=http://127.0.0.1:8000/v1` works unchanged.
- Request rules (measured on the Spark): `max_tokens >= 8192`,
  `reasoning_effort=low` (default effort burns ~1.6k reasoning tokens),
  MTP off. Single-stream ~15.7 tok/s.
- Cold start is 11-14 min. Warm the sidecar first:
  `cat /opt/models/Qwen3.8-Flash-Next-NVFP4/ple-sidecar/ples_nvfp4/* > /dev/null`
- `python scripts/llm_smoke.py` checks that `LLM_MODEL` is what the server
  serves and that a schema-constrained extraction round-trips.
- Boundary: `src.llm` never appears under `climate/`, `src/trading/`,
  `src/venues/`, `src/edge/`, `src/domain/`, `monitoring/` or the ordering
  scripts (`tests/test_architecture/test_llm_boundary.py`). The LLM turns
  text into data; the numeric model prices.

## 6. When the network changes

The Spark's LAN address (192.168.1.31) is a DHCP reservation on its Wi-Fi
MAC; switching to Ethernet needs a second reservation. The move to the US
changes the address entirely: mDNS (`spark-8f47.local`) or Tailscale keep
`ssh spark` working. If the Gateway is ever opened to the LAN for a
Spark-side `research` role, do it on Tailscale addresses from day one.
```

- [ ] **Step 2: Add the machine column to the IBKR runbook's role table**

With the Edit tool, replace in `docs/ibkr_runbook.md`:
```
  | Role | clientId | Notes |
  | --- | --- | --- |
  | `live_trading` | 1 | order-submission role.
```
with
```
  | Role | clientId | Machine | Notes |
  | --- | --- | --- | --- |
  | `live_trading` | 1 | Zenbook | order-submission role.
```
and each of the three remaining rows gains `| Zenbook ` after the clientId cell (`| 2 | Zenbook | the recorder...`, `| 3 | Zenbook | ad-hoc...`, `| 4 | Zenbook | \IBKR\IBKR Paper Sync...`). Add below the table: `The DGX Spark holds no client id (docs/spark_runbook.md section 1).`

- [ ] **Step 3: Point the checkpoint at the runbook**

Append to `docs/MIGRATION_CHECKPOINT.md`:
```markdown

## 9. Spark research mirror (2026-10)

The research half of the repo also runs on the DGX Spark against a one-way
mirror of `~/quant_data`; the trading runtime, every client id and every
scheduled task stay on the Zenbook. Parity is evidenced in the newest
`docs/spark_parity_*.md`; the operator's view is `docs/spark_runbook.md`.
The local vLLM model is reachable through `src/llm/` (config-driven model
name) and is fenced out of the pricing/ordering path by
`tests/test_architecture/test_llm_boundary.py`.
```

- [ ] **Step 4: Update memory**

With the Edit tool, replace in the memory file `dgx-spark.md` the paragraph starting `- Decision on which option: pending` with:
```
- Option B implemented 2026-10 on branch spark-research-split: Spark clone
  at ~/work/algorithmic-trade, uv Python 3.14 venv, mirror pushed by
  `wsl -d Ubuntu -- bash <repo>/scripts/spark/push_snapshot.sh` (Git Bash
  needs the //mnt form from scripts/spark/env.sh), parity gate =
  scripts/spark/parity_score.py on both machines + compare_json.py
  --ignore identity.machine --rtol 1e-8 --atol 1e-10 (the bridge is refit
  with LAPACK per machine, so 1e-12 was never achievable; the newest
  docs/spark_parity_*.md holds the registered numbers), LLM client src/llm
  with LLM_MODEL required. The Spark holds no client id.
```

- [ ] **Step 5: Final suites on both machines at the final commit**

```bash
source scripts/spark/env.sh
python -m pytest -q 2>&1 | tail -3; echo "pytest_exit=${PIPESTATUS[0]}"
python -m ruff check . --statistics --no-cache 2>&1 | tail -3
git diff --name-only origin/main...HEAD -- '*.py' | xargs python -m ruff check && echo TOUCHED_FILES_LINT_OK
git add docs/spark_runbook.md docs/ibkr_runbook.md docs/MIGRATION_CHECKPOINT.md
git commit -m "Document the Spark research mirror and the machine each role lives on" -m "$COAUTHOR"
git push
"$SSH" spark 'cd ~/work/algorithmic-trade && git pull --ff-only && git log --oneline -1 && HOME=$(mktemp -d) .venv/bin/python -m pytest -q 2>&1 | tail -3; echo "pytest_exit=${PIPESTATUS[0]}"; .venv/bin/python -m ruff check . --statistics --no-cache 2>&1 | tail -3; git diff --name-only origin/main...HEAD -- "*.py" | xargs -r .venv/bin/python -m ruff check && echo TOUCHED_FILES_LINT_OK'
```
Expected: equal `passed` counts on both, `pytest_exit=0` twice, the same ruff baseline total twice, `TOUCHED_FILES_LINT_OK` twice, same SHA. Paste under `## 11. Final suites (Task 11)`, commit the report with `-m "$COAUTHOR"`, push.

- [ ] **Step 6: Open the PR**

```bash
gh pr create --base main --head spark-research-split --title "Spark research mirror: parity tooling, data push, local LLM client" --body-file - <<'EOF'
## Summary
- Research half of the repo runs on the DGX Spark against a one-way mirror of ~/quant_data; every Gateway role and scheduled task stays on the Zenbook.
- analysis/spark_parity.py + scripts/spark/{verify_mirror,live_manifest,parity_score,compare_json}.py + push_snapshot.sh (WSL rsync): scoring parity proven at a registered tolerance with an independently computed snapshot identity (input hashes + code SHA), and the live root proven untouched by before/after manifests.
- src/llm: httpx client for the local vLLM model, LLM_MODEL required from config; tests/test_architecture/test_llm_boundary.py keeps it out of the pricing/ordering path.

## Evidence
- The dated docs/spark_parity_*.md report and its two payload files: environment, mirror push, registered tolerance, scoring parity (gate), refit parity (report-only), suites on both machines, live-root manifests, LLM smoke.

Design: docs/plans/2026-09-30-spark-research-split-design.md. Plan: docs/plans/2026-10-01-spark-research-split.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```
After the merge: `"$SSH" spark 'cd ~/work/algorithmic-trade && git checkout main && git pull --ff-only'` so the Spark tracks `main` as the runbook says.

---

## Self-review against the spec (and its 2026-10-01 amendments)

- Section 1 non-goals: no task touches the Zenbook's Python, tasks, Gateway, or runtime code; no LLM use in the live lane (Task 9 test); pilot out of scope; no CDS on the Spark; no data back into `~/quant_data` (Global Constraints, Task 3 script, runbook section 3; the Spark's parity payloads are evidence and land only under `~/quant_data_snapshot/parity/`, spec amendment R6).
- Section 2 invariants: `.env` = one line (Task 1 Step 6); invariant 1 PROVEN by command, not asserted (Task 1 Step 7); same root (all Spark commands use `~/quant_data`); role/machine table (Task 11 Steps 1-2); never-on-Spark list (runbook section 1, with the labels script's guard honestly called documentation-only).
- Section 3: clone tracking the branch (Task 1 Step 5), uv on Spark only (Task 1 Step 4, operator), constraints file with filtered freeze (Task 1 Step 2), extras `[dev,ibkr,climate,mcp,web]`, evidence captured (Task 1 Step 9).
- Section 4: `.gitattributes` (Task 0, so no shell file is ever checked out CRLF), `-rlt`, `--chmod`, no `--delete` on the push, freeze exclusions and the vanished-file retry, evidence protected from the freeze, temp-then-move identity, remote verify, timing rule (Task 3).
- Section 5.1-5.6: identity with sha256 of the newest sweep, the ledger and every scoring input plus the code SHA, hash tests against `hashlib` and a byte flip, newest-file tests with two sweeps and two hourly files (Task 2); scorer without clock, with the probe marker, the required live-paper gate, the bridge block and no writes (Task 5); compare with NaN/None/ignore semantics and a numeric-leaf floor (Task 4); registered tolerance from a kernel-variation experiment before the cross-machine gate (Task 6 Step 0); refit report-only with `fingerprint`, `fitted_at_utc` and `artifact_paths` ignored (Task 6 Step 4); suites same day with pytest exit codes and the two-part lint gate (Task 6 Step 5); live root proven untouched (Task 6 Step 6); report with SHA, versions and the two committed payloads (Task 6 Step 7).
- Section 6: settings fields, defaults and empty-value handling (Task 7), client API and errors (Task 8), smoke and live test (Task 10), boundary test with coverage, relative-import and temp-tree self-tests (Task 9), `.env.example` block (Task 9).
- Section 7: runbook with both invocation forms, role table, checkpoint pointer, memory (Task 11), none carrying a date placeholder.
- Section 8 testing table: every row has a task; the shell script is syntax-checked, rehearsed on temp dirs, and exercised by real pushes; scoped test counts are 17/20 (Task 2), 24/28 (Task 4), 28/33 (Task 5), 5, 10, 4 (Tasks 7-9), 10 + 1 skipped (Task 10).
- Placeholder scan: `<date>` is defined in Global Constraints and appears only in file names the executor derives from `RUN_DATE`; `CLONE_URL`, `<served id>` and `<paste>` are each filled in the same step from a command's output before the file is committed; `<repo>` in `push_snapshot.sh`'s usage comment is documentation of an operator-supplied path. No committed prose keeps a placeholder.
- Type consistency: `SparkRoot` fields (`root`, `sweep`, `ledger`, `model`, `seas5`, `era5`) used identically in Tasks 2, 4, 5; `compare_json` signature and `CompareResult` fields match between the Task 4 module and CLI; `score_snapshot` keyword names match between the Task 5 module, tests and CLI; `operational_manifest` / `HEARTBEAT_REL` match between the Task 2 module and `live_manifest.py`; `LLMReply` fields and error classes match between the Task 8 module, tests, Task 10 smoke and live test; every commit uses `$COAUTHOR` from `env.sh`.
