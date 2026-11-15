"""The terminal watcher: its renderables and gtm_sim_watch.py."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pandas as pd
import pytest
from rich.console import Console

from climate.sim.progress import ACTIVE, PROGRESS_FILE, ProgressWriter, list_runs, write_json_atomic
from climate.sim.watch import cells_table, fmt_duration, overall, render_list, render_run, sparkline
from scripts.research import gtm_sim_hindcast, gtm_sim_watch


def _text(renderable, width=160) -> str:
    console = Console(width=width, record=True, color_system=None, file=open("/dev/null", "w"))
    console.print(renderable)
    return console.export_text()


@pytest.fixture(autouse=True)
def _wide(monkeypatch):
    monkeypatch.setenv("COLUMNS", "160")


def _live_run(root, name="gtm_sim_hindcast_20261113T101500Z", steps=40):
    w = ProgressWriter(root / name, "gtm_sim_hindcast", ["m0_w0", "informed_sd0.08"], steps,
                       git_commit="fb330ec", start_thread=False, min_write_s=0)
    w.cell_started("m0_w0")
    for i in range(1, 11):
        w.step("m0_w0", i, steps, pd.Timestamp(2001, i, 14, 14, tz="UTC"), orders=i, filled=i,
               pnl_settled=float(i), pnl_marked=float(i) + 0.5)
    return w


def test_helpers():
    assert sparkline([1, 2, 3, 4, 5, 6, 7, 8]) == "▁▂▃▄▅▆▇█"
    assert sparkline([2, 2, 2]) == "▄▄▄"
    assert sparkline([float("nan"), 1.0, 2.0]) == "▁█"
    assert len(sparkline(list(range(100)), width=12)) == 12
    assert fmt_duration(42) == "42s" and fmt_duration(185) == "3m 05s" and fmt_duration(3725) == "1h 02m"
    assert fmt_duration(None) == "-"
    state = {"cells": [{"status": "finished", "steps": 10, "step": 10, "elapsed_s": 20.0},
                       {"status": "running", "steps": 10, "step": 5, "elapsed_s": 10.0},
                       {"status": "queued", "steps": 10, "step": 0, "elapsed_s": 0.0}]}
    done, total, eta = overall(state)
    assert (done, total) == (15, 30) and eta == pytest.approx(30.0)


@pytest.mark.parametrize("width", [160, 120, 90, 70])
def test_the_cell_table_drops_columns_instead_of_cutting_numbers(tmp_path, width):
    w = _live_run(tmp_path / "runs")
    table = cells_table(w.state["cells"], width)
    lines = _text(table, width=width).splitlines()
    assert max(len(line.rstrip()) for line in lines) <= width
    text = "\n".join(lines)
    assert "m0_w0" in text and "informed_sd0.08" in text and "10/40" in text
    assert "+10.50" in text  # the marked P&L survives every width


def test_render_run_says_what_a_dead_run_is(tmp_path):
    w = _live_run(tmp_path / "runs")
    text = _text(render_run(w.state))
    assert "gtm_sim_hindcast_20261113T101500Z" in text and "S3 hindcast" in text and "running" in text
    corpse = dict(w.state, pid=_dead_pid(), heartbeat_utc="2026-01-01T00:00:00Z")
    text = _text(render_run(corpse))
    assert "dead" in text and "memory ceiling" in text


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_render_list_puts_active_runs_first(tmp_path):
    root = tmp_path / "runs"
    done = ProgressWriter(root / "gtm_sim_score_20261114T090000Z", "gtm_sim_score", ["score"], 1,
                          start_thread=False)
    done.run_dir.mkdir(parents=True)
    done.finished({"rows": 3})
    _live_run(root)
    text = _text(render_list(list_runs(root)))
    assert text.index("gtm_sim_hindcast_20261113T101500Z") < text.index("gtm_sim_score_20261114T090000Z")
    assert "S1 score" in text and "50%" not in text.split("gtm_sim_score")[0][-40:]


def test_cli_lists_and_snapshots(tmp_path, capsys):
    root = tmp_path / "runs"
    w = _live_run(root)
    assert gtm_sim_watch.main(["--runs-root", str(root)]) == 0
    assert "gtm_sim_hindcast_20261113T101500Z" in capsys.readouterr().out
    assert gtm_sim_watch.main(["--runs-root", str(root), "--once"]) == 0
    out = capsys.readouterr().out
    assert "m0_w0" in out and "10/40" in out
    w.failed("RuntimeError: boom")
    assert gtm_sim_watch.main(["--runs-root", str(root), "--once", "--follow", "gtm_sim_h"]) == 1
    assert "boom" in capsys.readouterr().out
    assert gtm_sim_watch.main(["--runs-root", str(root), "--follow", "nope"]) == 2


def test_follow_tracks_a_run_to_its_end(tmp_path, capsys):
    root = tmp_path / "runs"
    w = _live_run(root, steps=12)

    def finish():
        time.sleep(0.4)
        w.step("m0_w0", 12, 12, pd.Timestamp(2001, 12, 14, tz="UTC"), pnl_marked=3.0)
        w.cell_finished("m0_w0", {"total_pnl": 3.0})
        w.run_dir.mkdir(parents=True)
        w.finished({"best_cell": "m0_w0", "best_pnl": 3.0})

    thread = threading.Thread(target=finish)
    thread.start()
    code = gtm_sim_watch.main(["--runs-root", str(root), "--follow", "--interval", "0.1"])
    thread.join()
    assert code == 0
    assert "finished" in capsys.readouterr().out


def test_show_prints_a_finished_hindcast(tmp_path, synthetic_root, capsys):
    root = tmp_path / "runs"
    out = root / "gtm_sim_hindcast_20261113T120000Z"
    assert gtm_sim_hindcast.main(["--root", str(synthetic_root), "--first-year", "2006",
                                  "--last-year", "2006", "--proxies", "m0", "--blends", "0,1",
                                  "--informed-sd", "0.4", "--out", str(out)]) == 0
    capsys.readouterr()
    assert gtm_sim_watch.main(["--runs-root", str(root), "--show", "latest"]) == 0
    text = capsys.readouterr().out
    assert "results: S3 hindcast" in text and "m0_w1" in text and "break-even" in text
    assert "dashboard.html" in text
    progress = json.loads((out / PROGRESS_FILE).read_text())
    assert progress["status"] == "finished" and len(progress["cells"]) == 3


def test_clear_dead_removes_the_dead_and_keeps_the_living(tmp_path, capsys):
    root = tmp_path / "runs"
    _live_run(root)
    dead_dir = root / ACTIVE / "gtm_sim_replay_20261112T080000Z"
    dead_dir.mkdir(parents=True)
    write_json_atomic(dead_dir / PROGRESS_FILE, {
        "schema": 1, "run": dead_dir.name, "kind": "gtm_sim_replay", "status": "running",
        "pid": _dead_pid(), "host": __import__("socket").gethostname(),
        "started_utc": "2026-11-12T08:00:00Z", "heartbeat_utc": "2026-11-12T08:05:00Z",
        "cells": [], "events_tail": []})
    assert gtm_sim_watch.main(["--runs-root", str(root), "--clear-dead"]) == 0
    assert "gtm_sim_replay_20261112T080000Z" in capsys.readouterr().out
    assert not dead_dir.exists()
    assert (root / ACTIVE / "gtm_sim_hindcast_20261113T101500Z").exists()
