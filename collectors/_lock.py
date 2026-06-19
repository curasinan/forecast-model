"""Cross-platform PID lock for skipping overlapping collector runs.

The lock file stores the owning process's PID. A new invocation checks the
file; if the PID still names a live process, it raises ``LockHeld`` so the
caller can exit cleanly (as opposed to stacking another run on top of an
in-flight one). Stale locks (file exists, PID is dead) are reclaimed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class LockHeld(Exception):
    """Raised when another live process already holds the lock."""

    def __init__(self, pid: int, path: Path):
        super().__init__(f"Lock held by PID {pid} at {path}")
        self.pid = pid
        self.path = path


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        # ERROR_ACCESS_DENIED (5) means the process exists but we can't
        # query it — still treat it as alive for locking purposes.
        return ctypes.windll.kernel32.GetLastError() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class PidLock:
    """Context manager that exclusively acquires a PID lock file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._acquired = False

    def __enter__(self) -> "PidLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing_pid = int(self.path.read_text().strip())
            except (OSError, ValueError):
                existing_pid = 0
            if existing_pid and _pid_alive(existing_pid):
                raise LockHeld(existing_pid, self.path)
            # Stale — previous process died without cleanup. Reclaim.
            try:
                self.path.unlink()
            except OSError:
                pass

        self.path.write_text(str(os.getpid()))
        self._acquired = True
        return self

    def __exit__(self, *exc_info) -> None:
        if not self._acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self._acquired = False
