"""The simulations' progress protocol (climate/sim/progress.py) and the replay's step hook."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import time as clock_time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from climate.sim.panel import load_panel
from climate.sim.progress import (
    ACTIVE,
    EVENTS_FILE,
    PROGRESS_FILE,
    ProgressWriter,
    clean,
    downsample,
    find_run,
    list_runs,
    liveness,
    prune_active,
    read_progress,
    write_json_atomic,
)
from climate.sim.replay import (
    Policy,
    Replay,
    ReplayConfig,
    decision_times,
    settlements,
    stop_times,
    target_months,
)
from tests.test_climate.sim_replay_fixtures import (
    DEC_EXPIRY,
    DEC_QUOTES,
    StubForecaster,
    times,
    utc,
    write_ladder,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _writer(tmp_path, clock=None, cells=("a", "b"), steps=10):
    return ProgressWriter(tmp_path / "runs" / "gtm_sim_test_20261113T100000Z", "gtm_sim_test",
                          list(cells), steps, args={"x": 1}, git_commit="abc1234",
                          clock=clock or Clock(), start_thread=False)


def _events(writer):
    return [json.loads(line) for line in (writer.dir / EVENTS_FILE).read_text().splitlines()]


def test_a_new_run_announces_itself(tmp_path):
    w = _writer(tmp_path)
    assert w.dir == tmp_path / "runs" / ACTIVE / "gtm_sim_test_20261113T100000Z"
    state = read_progress(w.dir / PROGRESS_FILE)
    assert state["schema"] == 1 and state["status"] == "running" and state["kind"] == "gtm_sim_test"
    assert [c["status"] for c in state["cells"]] == ["queued", "queued"]
    assert state["pid"] == os.getpid() and state["args"] == {"x": 1}
    assert _events(w)[0]["kind"] == "run_started"


def test_steps_are_throttled_but_the_last_one_always_lands(tmp_path):
    clock = Clock()
    w = _writer(tmp_path, clock)
    w.cell_started("a")
    for i in range(1, 6):  # all within the same second
        w.step("a", i, 10, utc(2026, 10, i), orders=i, pnl_marked=float(i))
    state = read_progress(w.dir / PROGRESS_FILE)
    assert state["cells"][0]["step"] in (0, 1)
    clock.t += 1.5
    w.step("a", 6, 10, utc(2026, 10, 6), orders=6, pnl_marked=6.0)
    assert read_progress(w.dir / PROGRESS_FILE)["cells"][0]["step"] == 6
    for i in range(7, 11):
        w.step("a", i, 10, utc(2026, 10, i), orders=i, pnl_marked=4.0)
    cell = read_progress(w.dir / PROGRESS_FILE)["cells"][0]
    assert cell["step"] == 10 and cell["orders"] == 10
    assert cell["drawdown"] == pytest.approx(2.0)  # peak 6, now 4
    assert cell["curve"][-1] == 4.0


def test_eta_follows_the_pace(tmp_path):
    clock = Clock()
    w = _writer(tmp_path, clock)
    w.cell_started("a")
    clock.t += 30
    w.step("a", 3, 10, None)
    assert w.cell("a")["eta_s"] == pytest.approx(70.0)


def test_skips_and_warnings_are_counted_and_logged(tmp_path):
    w = _writer(tmp_path)
    w.cell_started("a")
    w.step("a", 1, 10, None, skipped="no fresh snapshot", warnings=["reconcile aborted"])
    assert w.cell("a")["skipped"] == 1 and w.cell("a")["warnings"] == 1
    assert any(e["kind"] == "warning" and e["message"] == "reconcile aborted" for e in _events(w))


def test_a_finished_cell_keeps_its_metrics_and_frames(tmp_path):
    w = _writer(tmp_path)
    w.cell_started("a")
    w.cell_finished("a", {"total_pnl": 12.5, "cell": "a", "by_lead": {"0": 1.0}},
                    frames={"equity": pd.DataFrame({"equity_bid": [0.0, 1.0]})})
    state = read_progress(w.dir / PROGRESS_FILE)
    assert state["cells"][0]["status"] == "finished" and state["cells"][0]["metrics"]["total_pnl"] == 12.5
    assert pd.read_parquet(w.dir / "cells" / "a" / "equity.parquet")["equity_bid"].tolist() == [0.0, 1.0]
    finished = [e for e in _events(w) if e["kind"] == "cell_finished"]
    assert finished[0]["cell"] == "a" and "by_lead" not in finished[0]


def test_finishing_copies_the_record_into_the_run_directory(tmp_path):
    w = _writer(tmp_path)
    w.run_dir.mkdir(parents=True)
    w.finished({"d1": "D1 choice: ask.", "best_pnl": np.float64(3.5)})
    for name in (PROGRESS_FILE, EVENTS_FILE):
        assert (w.run_dir / name).exists()
    state = read_progress(w.run_dir / PROGRESS_FILE)
    assert state["status"] == "finished" and state["headline"]["best_pnl"] == 3.5
    assert _events(w)[-1]["kind"] == "run_finished"


def test_an_exception_marks_the_run_failed_and_propagates(tmp_path):
    w = _writer(tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        with w:
            w.cell_started("a")
            raise RuntimeError("boom")
    state = read_progress(w.dir / PROGRESS_FILE)
    assert state["status"] == "failed" and state["error"] == "RuntimeError: boom"
    assert state["cells"][0]["status"] == "failed" and state["cells"][1]["status"] == "queued"


def test_the_heartbeat_thread_keeps_the_file_fresh(tmp_path):
    w = ProgressWriter(tmp_path / "runs" / "r1", "k", ["a"], 1, heartbeat_s=0.05)
    first = read_progress(w.dir / PROGRESS_FILE)["heartbeat_utc"]
    time.sleep(1.2)
    later = read_progress(w.dir / PROGRESS_FILE)["heartbeat_utc"]
    w.finished({})
    assert later > first


def test_a_reader_never_sees_half_a_file(tmp_path):
    path = tmp_path / "p.json"
    write_json_atomic(path, {"n": 0})
    stop = threading.Event()

    def write():
        n = 0
        while not stop.is_set():
            n += 1
            write_json_atomic(path, {"n": n, "pad": "x" * 5000})

    thread = threading.Thread(target=write)
    thread.start()
    try:
        for _ in range(300):
            json.loads(path.read_text())
    finally:
        stop.set()
        thread.join()


def test_liveness(tmp_path):
    now = 1_800_000_000.0
    fresh = {"status": "running", "heartbeat_utc": "2027-01-15T08:00:00Z", "host": "here", "pid": os.getpid()}
    t_fresh = pd.Timestamp("2027-01-15T08:00:30Z").timestamp()
    assert liveness(fresh, t_fresh, host="here") == "running"
    stale_now = pd.Timestamp("2027-01-15T08:10:00Z").timestamp()
    assert liveness(fresh, stale_now, host="here") == "stalled"  # this very process is alive
    assert liveness(fresh, stale_now, host="elsewhere") == "silent"
    assert liveness({"status": "finished"}, now) == "finished"
    assert liveness({"status": "failed"}, now) == "failed"


def test_a_killed_run_shows_as_dead(tmp_path):
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from climate.sim.progress import ProgressWriter\n"
        f"w = ProgressWriter({str(tmp_path / 'runs' / 'victim')!r}, 'k', ['a'], 5, start_thread=False)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    state = read_progress(tmp_path / "runs" / ACTIVE / "victim" / PROGRESS_FILE)
    assert state["status"] == "running"
    assert liveness(state, time.time() + 600) == "dead"
    assert prune_active(tmp_path / "runs", include_dead=True, now=time.time() + 600) == ["victim"]


def test_runs_are_listed_and_found(tmp_path):
    runs = tmp_path / "runs"
    old = runs / "gtm_sim_replay_20261101T090000Z"
    old.mkdir(parents=True)
    (old / "config.json").write_text(json.dumps({"kind": "gtm_sim_replay", "git_commit": "f64ab32"}))
    done = ProgressWriter(runs / "gtm_sim_score_20261110T090000Z", "gtm_sim_score", ["score"], 1,
                          start_thread=False)
    done.run_dir.mkdir()
    done.finished({})
    live = ProgressWriter(runs / "gtm_sim_hindcast_20261113T090000Z", "gtm_sim_hindcast", ["m0_w0"],
                          10, start_thread=False)
    names = [r.name for r in list_runs(runs)]
    assert set(names) == {old.name, done.run_dir.name, live.run_dir.name}
    assert find_run(runs, "latest").name == live.run_dir.name
    assert find_run(runs, "gtm_sim_score").name == done.run_dir.name  # a unique prefix
    assert find_run(runs, "gtm_sim_") is None  # ambiguous
    assert find_run(runs, old.name).state["kind"] == "gtm_sim_replay"
    assert find_run(runs, str(live.run_dir)).active
    live.finished({})


def test_old_finished_entries_are_pruned(tmp_path):
    w = _writer(tmp_path)
    w.finished({})
    root = tmp_path / "runs"
    assert prune_active(root) == []
    assert prune_active(root, now=Clock().t + 2 * 86_400) == [w.dir.name]


def test_helpers():
    assert downsample(list(range(100)), 5) == [0, 25, 50, 74, 99]
    assert downsample([1, 2], 5) == [1, 2]
    assert clean({"a": float("nan"), "b": np.int64(3), "c": [np.float32(1.5)], "d": pd.Timestamp("2026-01-01")}) \
        == {"a": None, "b": 3, "c": [1.5], "d": "2026-01-01T00:00:00"}


def test_the_replay_reports_every_decision(tmp_path):
    root = tmp_path / "ticks"
    write_ladder(root, times(utc(2026, 10, 14, 13, 0), utc(2026, 10, 16, 16, 0)),
                 lambda ts: DEC_QUOTES, DEC_EXPIRY)
    panel = load_panel(root)
    contracts = panel.contracts()
    target_of = target_months(contracts, None)
    replay = Replay(panel, contracts, None, StubForecaster({"2026-12": (1.425, 0.115)}),
                    settlements(target_of, {}, {}), stop_times(contracts, target_of, None),
                    ReplayConfig(policy=Policy.parse("ask")), tmp_path / "work")
    seen = []
    decisions = decision_times(panel, cadence="daily", at=clock_time(14, 0))
    replay.run(decisions, on_step=seen.append)
    assert [s["step"] for s in seen] == list(range(1, len(decisions) + 1))
    assert all(s["steps"] == len(decisions) for s in seen)
    assert [s["sim_time"] for s in seen] == decisions
    assert seen[-1]["filled"] == 2 and seen[-1]["orders"] == 2
