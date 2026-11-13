"""Progress of a running simulation: a state file and an event log that viewers read.

While a run is going it keeps `<runs root>/.active/<run name>/` with
`progress.json` (the whole current state, replaced atomically, at most once a
second and at least every HEARTBEAT_S by a background thread) and
`events.jsonl` (one JSON object a line, appended). A finished cell also drops
its frames under `cells/<cell>/`, so a dashboard can show partial results.
At the end both files are copied into the run directory and the active entry
says `finished` or `failed`; a run killed outright is recognised by its stale
heartbeat and its vanished process (see `liveness`).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

SCHEMA = 1
ACTIVE = ".active"
PROGRESS_FILE = "progress.json"
EVENTS_FILE = "events.jsonl"
HEARTBEAT_S = 10.0
MIN_WRITE_S = 1.0
DEAD_AFTER_S = 120.0
EVENTS_TAIL = 20
CURVE_POINTS = 48
PRUNE_AFTER_S = 86_400.0

RUNNING, FINISHED, FAILED = "running", "finished", "failed"


def default_runs_root() -> Path:
    return Path.home() / "runs"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clean(value):
    """A JSON-safe copy: NaN and infinity as None, numpy and pandas scalars as Python ones."""
    if isinstance(value, Mapping):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).isoformat()
    if value is None or isinstance(value, (str, int)):
        return value
    return str(value)


def write_json_atomic(path: Path, obj) -> None:
    """Write beside, then rename over: a reader sees the old file or the new one, never half."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(clean(obj), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def downsample(values: Sequence[float], n: int = CURVE_POINTS) -> list[float]:
    """At most `n` evenly spaced values, always keeping the last."""
    values = list(values)
    if len(values) <= n:
        return values
    idx = np.linspace(0, len(values) - 1, n).round().astype(int)
    return [values[i] for i in idx]


def _new_cell(name: str, steps: int) -> dict:
    return {"name": name, "status": "queued", "phase": "", "step": 0, "steps": int(steps),
            "sim_time": None, "started_utc": None, "elapsed_s": 0.0, "eta_s": None,
            "orders": 0, "filled": 0, "pnl_settled": 0.0, "pnl_marked": 0.0, "drawdown": 0.0,
            "skipped": 0, "warnings": 0, "curve": [], "metrics": None}


class ProgressWriter:
    """The writing side of the protocol, owned by one simulation run."""

    def __init__(self, run_dir: str | Path, kind: str, cells: Sequence[str], steps: int = 0, *,
                 args: Mapping | None = None, git_commit: str | None = None,
                 heartbeat_s: float = HEARTBEAT_S, min_write_s: float = MIN_WRITE_S,
                 clock: Callable[[], float] = time.time, start_thread: bool = True):
        self.run_dir = Path(run_dir)
        self.dir = self.run_dir.parent / ACTIVE / self.run_dir.name
        try:
            prune_active(self.run_dir.parent)
        except OSError:
            pass
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.clock = clock
        self.min_write_s = min_write_s
        now = clock()
        self.state = {
            "schema": SCHEMA, "run": self.run_dir.name, "kind": kind, "status": RUNNING, "phase": "",
            "pid": os.getpid(), "host": socket.gethostname(), "git_commit": git_commit,
            "started_utc": _iso(now), "heartbeat_utc": _iso(now), "finished_utc": None,
            "args": dict(args or {}), "run_dir": str(self.run_dir), "headline": None,
            "error": None, "cells": [_new_cell(c, steps) for c in cells], "events_tail": [],
        }
        self._index = {c["name"]: i for i, c in enumerate(self.state["cells"])}
        self._curves: dict[str, list[float]] = {}
        self._peaks: dict[str, float] = {}
        self._started: dict[str, float] = {}
        self._lock = threading.RLock()
        self._last_write = 0.0
        self._stop = threading.Event()
        self._thread = None
        self.event("run_started", cells=len(cells), run_kind=kind)
        self._write()
        if start_thread:
            self._thread = threading.Thread(target=self._beat, args=(heartbeat_s,), daemon=True)
            self._thread.start()

    # -- context ------------------------------------------------------------
    def __enter__(self) -> ProgressWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and self.state["status"] == RUNNING:
            self.failed(exc)
        self._halt()
        return False

    # -- run level ------------------------------------------------------------
    def run_phase(self, phase: str) -> None:
        """What the run as a whole is doing now (loading quotes, writing results, ...)."""
        with self._lock:
            self.state["phase"] = phase
            self.event("phase", phase=phase)
            self._write()

    def set_steps(self, steps: int) -> None:
        """Every cell has `steps` decisions (known once the inputs are loaded)."""
        with self._lock:
            for c in self.state["cells"]:
                c["steps"] = int(steps)
            self._write()

    # -- cells --------------------------------------------------------------
    def cell(self, name: str) -> dict:
        return self.state["cells"][self._index[name]]

    def cell_started(self, name: str, steps: int | None = None, phase: str = "") -> None:
        with self._lock:
            c = self.cell(name)
            c.update(status=RUNNING, phase=phase, started_utc=_iso(self.clock()))
            if steps is not None:
                c["steps"] = int(steps)
            self._started[name] = self.clock()
            self.event("cell_started", cell=name)
            self._write()

    def cell_phase(self, name: str, phase: str) -> None:
        with self._lock:
            self.cell(name)["phase"] = phase
            self._write()

    def step(self, name: str, step: int, steps: int, sim_time=None, *, orders: int = 0,
             filled: int = 0, pnl_settled: float = 0.0, pnl_marked: float = 0.0,
             skipped: str = "", warnings: Sequence[str] = ()) -> None:
        with self._lock:
            now = self.clock()
            c = self.cell(name)
            elapsed = now - self._started.get(name, now)
            peak = max(self._peaks.get(name, 0.0), float(pnl_marked))
            self._peaks[name] = peak
            curve = self._curves.setdefault(name, [])
            curve.append(float(pnl_marked))
            c.update(status=RUNNING, phase="", step=int(step), steps=int(steps),
                     sim_time=clean(sim_time), elapsed_s=elapsed,
                     eta_s=elapsed / step * (steps - step) if step else None,
                     orders=int(orders), filled=int(filled), pnl_settled=float(pnl_settled),
                     pnl_marked=float(pnl_marked), drawdown=max(c["drawdown"], peak - float(pnl_marked)),
                     curve=downsample(curve))
            if skipped:
                c["skipped"] += 1
            for message in warnings:
                c["warnings"] += 1
                self.event("warning", cell=name, message=message, sim_time=clean(sim_time))
            if now - self._last_write >= self.min_write_s or step >= steps:
                self.event("step", cell=name, step=int(step), steps=int(steps),
                           sim_time=clean(sim_time), pnl_marked=float(pnl_marked))
                self._write()

    def stepper(self, name: str) -> Callable[[Mapping], None]:
        """A Replay.run on_step callback that reports into cell `name`."""
        def on_step(info: Mapping) -> None:
            self.step(name, info["step"], info["steps"], info.get("sim_time"),
                      orders=info.get("orders", 0), filled=info.get("filled", 0),
                      pnl_settled=info.get("pnl_settled", 0.0),
                      pnl_marked=info.get("pnl_marked", 0.0),
                      skipped=info.get("skipped", ""), warnings=info.get("warnings", ()))
        return on_step

    def warning(self, name: str | None, message: str) -> None:
        with self._lock:
            if name is not None:
                self.cell(name)["warnings"] += 1
            self.event("warning", cell=name, message=message)
            self._write()

    def cell_finished(self, name: str, metrics: Mapping,
                      frames: Mapping[str, pd.DataFrame] | None = None) -> None:
        """The cell is done: its metrics go into the state, its frames under cells/<name>/."""
        if frames:
            out = self.dir / "cells" / name
            out.mkdir(parents=True, exist_ok=True)
            for frame_name, frame in frames.items():
                tmp = out / f".{frame_name}.parquet.tmp"
                frame.to_parquet(tmp, index=False)
                os.replace(tmp, out / f"{frame_name}.parquet")
        with self._lock:
            c = self.cell(name)
            started = self._started.get(name, self.clock())
            c.update(status=FINISHED, phase="", eta_s=0.0, metrics=clean(dict(metrics)),
                     elapsed_s=self.clock() - started)
            if c["steps"]:
                c["step"] = c["steps"]
            scalars = {k: v for k, v in clean(dict(metrics)).items() if not isinstance(v, (dict, list))}
            self.event("cell_finished", **{**scalars, "cell": name})
            self._write()

    # -- the end ------------------------------------------------------------
    def finished(self, headline: Mapping | None = None) -> None:
        with self._lock:
            self.state.update(status=FINISHED, phase="", finished_utc=_iso(self.clock()),
                              headline=clean(dict(headline or {})))
            self.event("run_finished", **{k: v for k, v in clean(dict(headline or {})).items()
                                          if not isinstance(v, (dict, list))})
            self._write()
        self._halt()
        if self.run_dir.is_dir():
            for name in (PROGRESS_FILE, EVENTS_FILE):
                try:
                    shutil.copy2(self.dir / name, self.run_dir / name)
                except OSError:
                    pass

    def failed(self, error: BaseException | str) -> None:
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}".rstrip(": ")
        with self._lock:
            self.state.update(status=FAILED, finished_utc=_iso(self.clock()), error=text)
            for c in self.state["cells"]:
                if c["status"] == RUNNING:
                    c["status"] = FAILED
            self.event("run_failed", error=text)
            self._write()
        self._halt()

    # -- internals ----------------------------------------------------------
    def event(self, kind: str, /, **fields) -> None:
        record = clean({"ts": _iso(self.clock()), **fields, "kind": kind})
        with self._lock:
            with open(self.dir / EVENTS_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            tail = self.state["events_tail"]
            if kind != "step":
                tail.append(record)
                del tail[:-EVENTS_TAIL]

    def _write(self) -> None:
        with self._lock:
            now = self.clock()
            self.state["heartbeat_utc"] = _iso(now)
            write_json_atomic(self.dir / PROGRESS_FILE, self.state)
            self._last_write = now

    def _beat(self, every: float) -> None:
        while not self._stop.wait(every):
            try:
                self._write()
            except OSError:
                pass

    def _halt(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None


# -- the reading side ----------------------------------------------------------

@dataclass
class RunEntry:
    """A run a viewer can show: its state, where it lives, and whether it is still active."""

    name: str
    path: Path
    state: dict
    active: bool

    @property
    def started(self) -> float:
        return _parse_iso(self.state.get("started_utc")) or 0.0


def read_progress(path: str | Path) -> dict | None:
    """The state in a progress.json, or None when it is missing or unreadable."""
    path = Path(path)
    for _ in range(2):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
    return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def liveness(state: Mapping, now: float | None = None, host: str | None = None) -> str:
    """finished, failed, running, stalled (no heartbeat, process alive), dead, or silent (other host)."""
    status = state.get("status")
    if status in (FINISHED, FAILED):
        return status
    now = time.time() if now is None else now
    beat = _parse_iso(state.get("heartbeat_utc"))
    if beat is not None and now - beat <= DEAD_AFTER_S:
        return RUNNING
    if (host or socket.gethostname()) != state.get("host"):
        return "silent"
    return "stalled" if pid_alive(state.get("pid")) else "dead"


def _finished_state(run_dir: Path) -> dict | None:
    """A run directory's state: its own progress.json, else one made up from config.json."""
    state = read_progress(run_dir / PROGRESS_FILE)
    if state is not None:
        return state
    config = run_dir / "config.json"
    if not config.exists():
        return None
    try:
        cfg = json.loads(config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    stamp = run_dir.name.rsplit("_", 1)[-1]
    try:
        started = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        started = _iso(run_dir.stat().st_mtime)
    return {"schema": SCHEMA, "run": run_dir.name, "kind": cfg.get("kind", "?"), "status": FINISHED,
            "started_utc": started, "finished_utc": None, "cells": [], "events_tail": [],
            "headline": None, "run_dir": str(run_dir), "git_commit": cfg.get("git_commit")}


def list_runs(runs_root: str | Path | None = None) -> list[RunEntry]:
    """Active runs and finished run directories, newest first; an active entry wins over its run dir."""
    root = Path(runs_root) if runs_root is not None else default_runs_root()
    entries: dict[str, RunEntry] = {}
    if root.is_dir():
        for path in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            state = _finished_state(path)
            if state is not None:
                entries[path.name] = RunEntry(path.name, path, state, False)
        active = root / ACTIVE
        if active.is_dir():
            for path in active.iterdir():
                state = read_progress(path / PROGRESS_FILE)
                if state is not None and (path.name not in entries or state.get("status") == RUNNING):
                    entries[path.name] = RunEntry(path.name, path, state, True)
    return sorted(entries.values(), key=lambda e: e.started, reverse=True)


def find_run(runs_root: str | Path | None, spec: str = "latest") -> RunEntry | None:
    """'latest' (the newest running run, else the newest), a run name, a unique prefix or a path."""
    runs = list_runs(runs_root)
    if spec == "latest":
        running = [r for r in runs if r.active and liveness(r.state) == RUNNING]
        return (running or runs or [None])[0]
    path = Path(spec).expanduser()
    if os.sep in spec or "/" in spec or path.exists():
        active = path.parent / ACTIVE / path.name
        state = read_progress(active / PROGRESS_FILE)
        if state is not None and (state.get("status") == RUNNING or not path.is_dir()):
            return RunEntry(path.name, active, state, True)
        if path.is_dir():
            state = _finished_state(path)
            return RunEntry(path.name, path, state, False) if state else None
        return None
    exact = [r for r in runs if r.name == spec]
    if exact:
        return exact[0]
    prefixed = [r for r in runs if r.name.startswith(spec)]
    return prefixed[0] if len({r.name for r in prefixed}) == 1 else None


def prune_active(runs_root: str | Path, *, older_than_s: float = PRUNE_AFTER_S,
                 include_dead: bool = False, now: float | None = None) -> list[str]:
    """Remove finished or failed active entries older than `older_than_s` (and dead ones, on request)."""
    active = Path(runs_root) / ACTIVE
    if not active.is_dir():
        return []
    now = time.time() if now is None else now
    removed = []
    for path in active.iterdir():
        state = read_progress(path / PROGRESS_FILE)
        if state is None:
            continue
        status = liveness(state, now)
        ended = _parse_iso(state.get("finished_utc")) or _parse_iso(state.get("heartbeat_utc")) or now
        old_end = status in (FINISHED, FAILED) and now - ended >= older_than_s
        if old_end or (include_dead and status == "dead"):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path.name)
    return removed


__all__ = [
    "ACTIVE",
    "DEAD_AFTER_S",
    "EVENTS_FILE",
    "FAILED",
    "FINISHED",
    "PROGRESS_FILE",
    "RUNNING",
    "SCHEMA",
    "ProgressWriter",
    "RunEntry",
    "clean",
    "default_runs_root",
    "downsample",
    "find_run",
    "list_runs",
    "liveness",
    "pid_alive",
    "prune_active",
    "read_progress",
    "write_json_atomic",
]
