"""analysis/gpu_budget.py, analysis/cgroup_memory.py and scripts/spark/cgroup_cuda_check.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from analysis.cgroup_memory import (
    EXIT_INCONSISTENT,
    EXIT_OUTSIDE,
    EXIT_UNFIT,
    EXIT_USAGE,
    GIB,
    CgroupError,
    judge,
    own_cgroup_dir,
    parse_meminfo,
    read_limit,
    read_peak,
)
from analysis.gpu_budget import ENV_VAR, GpuBudgetError, apply_gpu_budget, budget_from_env

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import cgroup_cuda_check  # noqa: E402

TOTAL = 119 * GIB  # what a GB10 reports as its device total, roughly


class FakeCuda:
    def __init__(self, *, available=True, reserved=0, total=TOTAL, on_alloc=None):
        self.available = available
        self.reserved = reserved
        self.total = total
        self.fractions: list[tuple[float, int]] = []
        self.on_alloc = on_alloc
        self.synced = 0

    def is_available(self):
        return self.available

    def memory_reserved(self, device=0):
        return self.reserved

    def get_device_properties(self, device=0):
        return SimpleNamespace(total_memory=self.total)

    def set_per_process_memory_fraction(self, fraction, device=0):
        self.fractions.append((fraction, device))

    def synchronize(self):
        self.synced += 1

    def get_device_name(self, device=0):
        return "NVIDIA GB10 (fake)"

    def empty_cache(self):
        pass


class FakeTorch:
    uint8 = "uint8"
    __version__ = "0.0-fake"

    def __init__(self, **cuda_kwargs):
        self.cuda = FakeCuda(**cuda_kwargs)
        self.allocations: list[tuple[int, str, str]] = []

    def ones(self, size, dtype=None, device=None):
        self.allocations.append((size, dtype, device))
        if self.cuda.on_alloc is not None:
            self.cuda.on_alloc(size)
        return object()


def test_the_budget_becomes_a_fraction_of_the_device_total():
    torch = FakeTorch()
    budget = apply_gpu_budget(20, environ={}, torch_module=torch)
    assert torch.cuda.fractions == [(20 * GIB / TOTAL, 0)]
    assert budget.budget_gib == 20 and budget.source == "argument"
    assert budget.device_total_gib == pytest.approx(119)


def test_the_launchers_budget_is_used_when_the_job_names_none():
    torch = FakeTorch()
    budget = apply_gpu_budget(environ={ENV_VAR: "12"}, torch_module=torch)
    assert budget.budget_gib == 12 and budget.source == ENV_VAR
    assert torch.cuda.fractions == [(12 * GIB / TOTAL, 0)]


def test_no_budget_anywhere_is_refused_before_cuda_is_touched():
    torch = FakeTorch()
    with pytest.raises(GpuBudgetError, match="research_run.sh --gpu-budget"):
        apply_gpu_budget(environ={}, torch_module=torch)
    assert torch.cuda.fractions == []


def test_a_job_may_ask_for_less_than_the_launcher_but_never_for_more():
    assert apply_gpu_budget(8, environ={ENV_VAR: "12"}, torch_module=FakeTorch()).budget_gib == 8
    torch = FakeTorch()
    with pytest.raises(GpuBudgetError, match="ceiling"):
        apply_gpu_budget(16, environ={ENV_VAR: "12"}, torch_module=torch)
    assert torch.cuda.fractions == []


def test_memory_already_held_means_the_call_came_too_late():
    torch = FakeTorch(reserved=512 * 2**20)
    with pytest.raises(GpuBudgetError, match="before the first CUDA allocation"):
        apply_gpu_budget(4, environ={}, torch_module=torch)
    assert torch.cuda.fractions == []


def test_no_cuda_and_a_budget_past_the_device_are_refused():
    with pytest.raises(GpuBudgetError, match="not available"):
        apply_gpu_budget(4, environ={}, torch_module=FakeTorch(available=False))
    with pytest.raises(GpuBudgetError, match="not below"):
        apply_gpu_budget(119, environ={}, torch_module=FakeTorch())


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "4"])
def test_a_budget_that_is_not_a_positive_finite_number_is_refused(bad):
    with pytest.raises(GpuBudgetError):
        apply_gpu_budget(bad, environ={}, torch_module=FakeTorch())


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "nan", "inf"])
def test_a_launcher_value_that_is_not_a_positive_number_is_refused(raw):
    with pytest.raises(GpuBudgetError, match=ENV_VAR):
        budget_from_env({ENV_VAR: raw})


def test_an_empty_launcher_value_is_no_budget():
    assert budget_from_env({ENV_VAR: ""}) is None
    assert budget_from_env({}) is None


def _cgroup(root: Path, rel: str, *, limit: str, current: int, peak: int | None = None) -> Path:
    directory = root / rel.lstrip("/")
    directory.mkdir(parents=True)
    (directory / "memory.max").write_text(f"{limit}\n", encoding="ascii")
    (directory / "memory.current").write_text(f"{current}\n", encoding="ascii")
    if peak is not None:
        (directory / "memory.peak").write_text(f"{peak}\n", encoding="ascii")
    return directory


SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/run-r1.scope"


def test_the_v2_line_names_the_directory(tmp_path):
    directory = _cgroup(tmp_path, SCOPE, limit=str(GIB), current=0)
    assert own_cgroup_dir(f"0::{SCOPE}\n", tmp_path) == directory
    # a hybrid host lists v1 controllers too; only the 0:: line counts
    assert own_cgroup_dir(f"12:memory:/v1/path\n0::{SCOPE}\n", tmp_path) == directory


def test_no_v2_line_or_no_memory_controller_is_an_error(tmp_path):
    with pytest.raises(CgroupError, match="no cgroup v2"):
        own_cgroup_dir("12:memory:/v1/path\n", tmp_path)
    (tmp_path / "bare.scope").mkdir()
    with pytest.raises(CgroupError, match="memory.current"):
        own_cgroup_dir("0::/bare.scope\n", tmp_path)


def test_max_reads_as_no_ceiling_and_a_missing_peak_as_none(tmp_path):
    directory = _cgroup(tmp_path, "/a.scope", limit="max", current=5)
    assert read_limit(directory) is None
    assert read_peak(directory) is None


def test_meminfo_is_read_in_bytes():
    info = parse_meminfo("MemTotal:       131072 kB\nMemAvailable:    2048 kB\nHugePages_Total: 0\n")
    assert info == {"MemTotal": 131072 * 1024, "MemAvailable": 2048 * 1024, "HugePages_Total": 0}


def test_a_small_charge_is_the_outside_verdict_and_a_large_one_is_no_verdict():
    outside = judge(limit=GIB, allocated=2 * GIB, before=10 * 2**20, after=40 * 2**20)
    assert outside.exit_code == EXIT_OUTSIDE and outside.verdict.startswith("cuda_outside_cgroup")
    inconsistent = judge(limit=GIB, allocated=2 * GIB, before=0, after=int(1.5 * GIB))
    assert inconsistent.exit_code == EXIT_INCONSISTENT
    assert inconsistent.verdict.startswith("inconsistent")


def _machine(tmp_path, *, limit=str(GIB), available_gib=40, current=8 * 2**20):
    directory = _cgroup(tmp_path / "cg", SCOPE, limit=limit, current=current, peak=current)
    proc = tmp_path / "self_cgroup"
    proc.write_text(f"0::{SCOPE}\n", encoding="ascii")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemAvailable: {available_gib * 1024 * 1024} kB\n", encoding="ascii")
    return directory, dict(proc_self_cgroup=proc, cgroup_root=tmp_path / "cg", meminfo=meminfo)


def test_a_job_that_lives_past_its_ceiling_uncharged_gives_the_outside_verdict(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv(ENV_VAR, raising=False)
    _, paths = _machine(tmp_path)
    torch = FakeTorch()
    out = tmp_path / "evidence.json"
    assert cgroup_cuda_check.main(["--alloc-gib", "2", "--out", str(out)],
                                  torch_module=torch, **paths) == EXIT_OUTSIDE
    assert torch.allocations == [(2 * GIB, "uint8", "cuda")]
    assert torch.cuda.fractions == [(3 * GIB / TOTAL, 0)]  # the check obeys the budget rule
    text = capsys.readouterr().out
    assert "launcher exit 137" in text and "VERDICT: cuda_outside_cgroup" in text
    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert evidence["exit_code"] == EXIT_OUTSIDE
    assert evidence["memory_max_bytes"] == GIB and evidence["allocated_bytes"] == 2 * GIB
    assert evidence["charged_bytes"] == 0 and evidence["gpu_budget_gib"] == 3


def test_a_charge_that_does_not_add_up_is_reported_and_not_judged(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(ENV_VAR, raising=False)
    directory, paths = _machine(tmp_path)

    def charge(size):
        (directory / "memory.current").write_text(f"{size}\n", encoding="ascii")

    rc = cgroup_cuda_check.main(["--alloc-gib", "2"], torch_module=FakeTorch(on_alloc=charge),
                                **paths)
    assert rc == EXIT_INCONSISTENT
    assert "VERDICT: inconsistent" in capsys.readouterr().out


def test_an_allocation_inside_the_ceiling_proves_nothing_and_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    _, paths = _machine(tmp_path, limit=str(4 * GIB))
    torch = FakeTorch()
    assert cgroup_cuda_check.main(["--alloc-gib", "2"], torch_module=torch, **paths) == EXIT_USAGE
    assert torch.allocations == [] and torch.cuda.fractions == []


@pytest.mark.parametrize("case", ["no_ceiling", "short_of_memory", "no_cuda", "no_v2"])
def test_a_machine_that_cannot_run_the_check_is_unfit_and_nothing_is_allocated(
    tmp_path, monkeypatch, case
):
    monkeypatch.delenv(ENV_VAR, raising=False)
    _, paths = _machine(tmp_path, limit="max" if case == "no_ceiling" else str(GIB),
                        available_gib=3 if case == "short_of_memory" else 40)
    if case == "no_v2":
        paths["proc_self_cgroup"].write_text("12:memory:/v1\n", encoding="ascii")
    torch = FakeTorch(available=case != "no_cuda")
    assert cgroup_cuda_check.main(["--alloc-gib", "2"], torch_module=torch, **paths) == EXIT_UNFIT
    assert torch.allocations == []


def test_a_launcher_budget_below_the_allocation_is_unfit(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "2")  # the check needs alloc + 1 = 3 GiB
    _, paths = _machine(tmp_path)
    torch = FakeTorch()
    assert cgroup_cuda_check.main(["--alloc-gib", "2"], torch_module=torch, **paths) == EXIT_UNFIT
    assert torch.allocations == []
