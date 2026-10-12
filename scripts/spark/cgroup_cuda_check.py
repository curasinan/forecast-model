"""Does research_run.sh's MemoryMax bound what a job allocates on the GPU? Asked on the Spark."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.cgroup_memory import (  # noqa: E402
    EXIT_UNFIT,
    EXIT_USAGE,
    GIB,
    CgroupError,
    judge,
    own_cgroup_dir,
    parse_meminfo,
    read_current,
    read_limit,
    read_peak,
)
from analysis.gpu_budget import GpuBudgetError, apply_gpu_budget  # noqa: E402

FREE_MARGIN = 2 * GIB


def main(
    argv: list[str] | None = None,
    *,
    proc_self_cgroup: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    meminfo: Path = Path("/proc/meminfo"),
    torch_module: Any | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--alloc-gib", type=int, default=2,
                        help="GiB to allocate on the GPU; must exceed the job's MemoryMax")
    parser.add_argument("--out", type=Path, default=None, help="also write the evidence as JSON")
    args = parser.parse_args(argv)
    if args.alloc_gib < 1:
        print("cgroup check: --alloc-gib must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    allocated = args.alloc_gib * GIB

    try:
        directory = own_cgroup_dir(proc_self_cgroup.read_text(encoding="ascii"), cgroup_root)
        limit = read_limit(directory)
    except (OSError, CgroupError) as exc:
        print(f"cgroup check: unfit: {exc}", file=sys.stderr)
        return EXIT_UNFIT
    if limit is None:
        print("cgroup check: unfit: this process has no MemoryMax; start it through "
              "scripts/spark/research_run.sh --memory 1G", file=sys.stderr)
        return EXIT_UNFIT
    if allocated <= limit:
        print(f"cgroup check: --alloc-gib {args.alloc_gib} does not exceed the ceiling of "
              f"{limit / GIB:.2f} GiB, so surviving it would prove nothing; give the launcher "
              "a smaller --memory or allocate more", file=sys.stderr)
        return EXIT_USAGE
    available = parse_meminfo(meminfo.read_text(encoding="ascii")).get("MemAvailable", 0)
    if available < allocated + FREE_MARGIN:
        print(f"cgroup check: unfit: {available / GIB:.1f} GiB available, the check needs "
              f"{(allocated + FREE_MARGIN) / GIB:.1f}; stop the LLM first", file=sys.stderr)
        return EXIT_UNFIT

    try:
        budget = apply_gpu_budget(args.alloc_gib + 1, torch_module=torch_module)
    except GpuBudgetError as exc:
        print(f"cgroup check: unfit: {exc}", file=sys.stderr)
        return EXIT_UNFIT
    if torch_module is None:
        import torch as torch_module

    before = read_current(directory)
    print(f"cgroup {directory}: MemoryMax {limit / GIB:.2f} GiB, charged {before / GIB:.2f} GiB")
    print(f"allocating {args.alloc_gib} GiB on the GPU; if this job is killed now (launcher "
          "exit 137), the ceiling covers CUDA memory", flush=True)
    block = torch_module.ones(allocated, dtype=torch_module.uint8, device="cuda")
    torch_module.cuda.synchronize()
    after = read_current(directory)
    peak = read_peak(directory)
    device_name = torch_module.cuda.get_device_name(0)
    del block
    torch_module.cuda.empty_cache()

    judgement = judge(limit=limit, allocated=allocated, before=before, after=after)
    print(f"held {args.alloc_gib} GiB on the GPU; cgroup charged {after / GIB:.2f} GiB "
          f"(peak {'n/a' if peak is None else f'{peak / GIB:.2f} GiB'})")
    print(f"VERDICT: {judgement.verdict}")
    if args.out is not None:
        evidence = {
            "check": "cgroup_cuda",
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": platform.node(),
            "kernel": platform.release(),
            "torch": getattr(torch_module, "__version__", "unknown"),
            "device": device_name,
            "cgroup": str(directory),
            "memory_max_bytes": limit,
            "allocated_bytes": allocated,
            "current_before_bytes": before,
            "current_after_bytes": after,
            "peak_bytes": peak,
            "charged_bytes": judgement.charged_bytes,
            "gpu_budget_gib": budget.budget_gib,
            "exit_code": judgement.exit_code,
            "verdict": judgement.verdict,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"evidence: {args.out}")
    return judgement.exit_code


if __name__ == "__main__":
    sys.exit(main())
