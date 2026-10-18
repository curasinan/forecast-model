#!/usr/bin/env bash
# Run a research job on the Spark under a memory ceiling, as the first thing the kernel
# kills when memory runs out.
#
# Runs ON THE SPARK, as the operator (never as trading), from the research clone:
#   bash scripts/spark/research_run.sh -- .venv/bin/python scripts/fit_gtm_emos.py --trend --root ~/quant_data/climate_data
#   bash scripts/spark/research_run.sh --memory 60G -- .venv/bin/python train.py
#   bash scripts/spark/research_run.sh --memory 8G --gpu-budget 20G -- ~/work/wx-dl/.venv/bin/python job.py
#   bash scripts/spark/research_run.sh --dry-run -- COMMAND [ARG...]   # print the command, run nothing
#
# Kill order under memory pressure: research jobs +500, the LLM (qwen36) -500, the Gateway
# and the trading runtime -900. The kernel kills the highest score first.
#   * MemoryMax (default 15G) is a hard ceiling for the HOST memory of the job and everything
#     it starts, and MemorySwapMax=0 keeps it from spilling into swap instead. A job that hits
#     the ceiling is killed inside its own scope. It does NOT bound GPU memory: CUDA
#     allocations are not charged to the cgroup (docker stats showed vLLM at 6.36 GiB while
#     it held about 87 GiB, 2026-10-11; scripts/spark/cgroup_cuda_check.py is the check).
#     Ask for more only after `cd ~/stack && docker compose stop qwen36`.
#   * --gpu-budget NG is the GPU side: the job sees RESEARCH_GPU_BUDGET_GIB=N, and
#     analysis/gpu_budget.py's apply_gpu_budget(), which every torch job calls first, caps
#     PyTorch's allocator at N GiB. Without the flag the variable is removed from the job's
#     environment, so the budget can only come from here (or from the job's own argument).
#   * The score 500 is written by the job's own shell before it execs the command: a scope
#     unit has no OOMScoreAdjust= property, and raising one's own score needs no privilege.
#     Child processes inherit it.
# The job runs in the foreground of this shell; use tmux for anything long.
# Exit: 2 usage; otherwise the command's own status.
set -euo pipefail

MEMORY="15G"
GPU_BUDGET=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --memory)
      [ $# -ge 2 ] || { echo "research_run: --memory needs a value such as 15G" >&2; exit 2; }
      MEMORY="$2"; shift 2 ;;
    --gpu-budget)
      [ $# -ge 2 ] || { echo "research_run: --gpu-budget needs a value such as 20G" >&2; exit 2; }
      GPU_BUDGET="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    --) shift; break ;;
    *) echo "research_run: unknown argument: $1 (the command goes after --)" >&2; exit 2 ;;
  esac
done
if [[ ! "$MEMORY" =~ ^[1-9][0-9]*[KMGT]$ ]]; then
  echo "research_run: --memory takes a positive number with a unit K, M, G or T, such as 15G (got '$MEMORY')" >&2
  exit 2
fi
if [ -n "$GPU_BUDGET" ] && [[ ! "$GPU_BUDGET" =~ ^[1-9][0-9]*G$ ]]; then
  echo "research_run: --gpu-budget takes a positive whole number of GiB with the unit G, such as 20G (got '$GPU_BUDGET')" >&2
  exit 2
fi
if [ $# -eq 0 ]; then
  echo "research_run: no command given; put it after --" >&2
  exit 2
fi

if [ -n "$GPU_BUDGET" ]; then
  BUDGET_ENV=(env "RESEARCH_GPU_BUDGET_GIB=${GPU_BUDGET%G}")
else
  BUDGET_ENV=(env -u RESEARCH_GPU_BUDGET_GIB)
fi

LAUNCH=(systemd-run --user --scope --quiet --collect
        -p "MemoryMax=$MEMORY" -p MemorySwapMax=0 --
        "${BUDGET_ENV[@]}"
        bash -c 'echo 500 > /proc/self/oom_score_adj && exec "$@"' research_run "$@")

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: ${LAUNCH[*]}"
  exit 0
fi
exec "${LAUNCH[@]}"
