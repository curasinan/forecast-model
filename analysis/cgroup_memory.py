"""Reading a process's cgroup v2 memory files and /proc/meminfo, and judging the CUDA check."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GIB = 2**30

EXIT_OUTSIDE = 0  # verdict: CUDA memory is not charged to the cgroup
EXIT_INCONSISTENT = 1
EXIT_USAGE = 2
EXIT_UNFIT = 3  # the machine or the process cannot run the check


class CgroupError(RuntimeError):
    """The process is not in a cgroup v2 hierarchy with a memory controller."""


def own_cgroup_dir(proc_self_cgroup: str, root: Path = Path("/sys/fs/cgroup")) -> Path:
    """The cgroup v2 directory named by the text of /proc/self/cgroup."""
    for line in proc_self_cgroup.splitlines():
        hierarchy, _, rest = line.partition(":")
        _controllers, _, path = rest.partition(":")
        if hierarchy == "0" and path.startswith("/"):
            directory = root / path.lstrip("/")
            if not (directory / "memory.current").is_file():
                raise CgroupError(f"{directory} has no memory.current (memory controller off?)")
            return directory
    raise CgroupError("no cgroup v2 entry (0::/...) in /proc/self/cgroup")


def read_limit(directory: Path) -> int | None:
    """memory.max in bytes; None for "max" (no ceiling)."""
    raw = (directory / "memory.max").read_text(encoding="ascii").strip()
    return None if raw == "max" else int(raw)


def read_current(directory: Path) -> int:
    return int((directory / "memory.current").read_text(encoding="ascii").strip())


def read_peak(directory: Path) -> int | None:
    """memory.peak in bytes, or None on a kernel that does not keep it."""
    path = directory / "memory.peak"
    return int(path.read_text(encoding="ascii").strip()) if path.is_file() else None


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo as bytes per field ("MemAvailable: 1024 kB" -> 1048576)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        fields = rest.split()
        if not sep or not fields:
            continue
        value = int(fields[0])
        if len(fields) > 1 and fields[1] == "kB":
            value *= 1024
        out[name.strip()] = value
    return out


@dataclass(frozen=True)
class Judgement:
    exit_code: int
    verdict: str
    charged_bytes: int


def judge(*, limit: int, allocated: int, before: int, after: int) -> Judgement:
    """The verdict of a job that allocated ``allocated`` bytes on the GPU and lived."""
    charged = after - before
    if charged < allocated / 2:
        return Judgement(
            EXIT_OUTSIDE,
            "cuda_outside_cgroup: the job held more on the GPU than its MemoryMax allows and "
            f"its cgroup was charged {charged / GIB:.2f} GiB of {allocated / GIB:.2f} GiB",
            charged,
        )
    return Judgement(
        EXIT_INCONSISTENT,
        f"inconsistent: charged {charged / GIB:.2f} GiB of {allocated / GIB:.2f} GiB above a "
        f"ceiling of {limit / GIB:.2f} GiB and still alive; record no verdict",
        charged,
    )
