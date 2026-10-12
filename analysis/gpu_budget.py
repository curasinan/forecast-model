"""The GPU memory budget of a research job on the Spark: the first call of every torch job."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ENV_VAR = "RESEARCH_GPU_BUDGET_GIB"
GIB = 2**30


class GpuBudgetError(RuntimeError):
    """The budget cannot be applied; the job must not touch the GPU."""


@dataclass(frozen=True)
class GpuBudget:
    budget_gib: float
    device_total_gib: float
    fraction: float
    device: int
    source: str  # "argument" or the environment variable's name


def _positive_gib(value: Any, what: str) -> float:
    # bool is an int: True must not read as a 1 GiB budget.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GpuBudgetError(f"{what} must be a number of GiB, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise GpuBudgetError(f"{what} must be a positive, finite number of GiB, got {value!r}")
    return number


def budget_from_env(environ: Mapping[str, str] | None = None) -> float | None:
    """The launcher's budget in GiB, or None when it set none."""
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_VAR)
    if raw is None or raw.strip() == "":
        return None
    try:
        number = float(raw)
    except ValueError:
        raise GpuBudgetError(f"{ENV_VAR}={raw!r} is not a number of GiB") from None
    return _positive_gib(number, ENV_VAR)


def apply_gpu_budget(
    budget_gib: float | None = None,
    *,
    device: int = 0,
    environ: Mapping[str, str] | None = None,
    torch_module: Any | None = None,
) -> GpuBudget:
    """Cap this process's PyTorch allocations on ``device`` at ``budget_gib`` GiB."""
    from_env = budget_from_env(environ)
    if budget_gib is None:
        if from_env is None:
            raise GpuBudgetError(
                "no GPU budget: start the job with scripts/spark/research_run.sh --gpu-budget NG "
                "or pass apply_gpu_budget(budget_gib)"
            )
        budget, source = from_env, ENV_VAR
    else:
        budget, source = _positive_gib(budget_gib, "budget_gib"), "argument"
        if from_env is not None and budget > from_env:
            raise GpuBudgetError(
                f"budget_gib={budget:g} asks for more than the launcher's "
                f"{ENV_VAR}={from_env:g}; the launcher's number is the ceiling"
            )

    if torch_module is None:
        import torch as torch_module  # the one torch import the guard test exempts

    cuda = torch_module.cuda
    if not cuda.is_available():
        raise GpuBudgetError("CUDA is not available to this process")
    held = cuda.memory_reserved(device)
    if held:
        raise GpuBudgetError(
            f"the allocator already holds {held} bytes on device {device}: apply_gpu_budget "
            "must run before the first CUDA allocation"
        )
    total = cuda.get_device_properties(device).total_memory
    fraction = budget * GIB / total
    if fraction >= 1:
        raise GpuBudgetError(
            f"a budget of {budget:g} GiB is not below the device's {total / GIB:.1f} GiB"
        )
    cuda.set_per_process_memory_fraction(fraction, device)
    return GpuBudget(
        budget_gib=budget,
        device_total_gib=total / GIB,
        fraction=fraction,
        device=device,
        source=source,
    )
