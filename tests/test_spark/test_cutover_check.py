"""scripts/spark/cutover_check.py: the four lines of the cutover gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import cutover_check  # noqa: E402

SINCE = "2026-10-20T16:00:00+00:00"
BEFORE = "2026-10-19T14:30:00+00:00"
AFTER = "2026-10-20T16:20:00+00:00"
COUNTS = {"examined": 3, "filled": 0, "partially_filled": 0, "cancelled": 0, "unchanged": 3,
          "unmatched_venue_orders": 0}


def _sync(ts: str, ok: bool = True, **changes) -> str:
    return json.dumps({"ts_utc": ts, "ok": ok, **COUNTS, **changes})


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).timestamp()


def _systemd(status: int, exited: str | None):
    """A stand-in for subprocess.run that answers `systemctl show ibkr-health.service ...`."""

    def run(cmd, **_kwargs):
        assert cmd[:3] == ["systemctl", "show", "ibkr-health.service"], cmd
        stamp = "" if exited is None else f"@{int(_epoch(exited))}"
        text = f"ExecMainExitTimestamp={stamp}\nExecMainStatus={status}\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

    return run


@pytest.fixture
def site(tmp_path):
    """A live root and a repo directory in the state a good cutover leaves them in."""
    root = tmp_path / "quant_data"
    sweep = root / "ibkr_ticks" / "quotes" / "date=2026-10-20" / "quotes-161500Z.parquet"
    sweep.parent.mkdir(parents=True)
    pd.DataFrame({"con_id": range(2400), "bid": [0.4] * 2400}).to_parquet(sweep, index=False)
    os.utime(sweep, (_epoch(AFTER), _epoch(AFTER)))
    paper = root / "ibkr_ticks" / "paper"
    paper.mkdir(parents=True)
    (paper / "ibkr_sync_runs.jsonl").write_text(
        _sync(BEFORE) + "\n" + _sync(AFTER) + "\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "logs").mkdir(parents=True)
    # A healthy run writes nothing here; the DOWN line is from before the cutover.
    (repo / "logs" / "ibkr_health.log").write_text(
        f"{BEFORE} DOWN ibkr recorder DOWN: unexpected disconnect\n"
        f"{AFTER} ALERT_SENT test - telegram\n", encoding="utf-8")
    same = {"x": [float(i) for i in range(1200)], "identity": {"machine": {"python": "3.14.2"}}}
    left = tmp_path / "zenbook.json"
    right = tmp_path / "spark.json"
    left.write_text(json.dumps(same), encoding="utf-8")
    other = dict(same, identity={"machine": {"python": "3.14.2", "machine": "aarch64"}})
    right.write_text(json.dumps(other), encoding="utf-8")
    return {"root": root, "repo": repo, "sweep": sweep, "paper": paper,
            "health": repo / "logs" / "ibkr_health.log", "left": left, "right": right}


def _run(site, capsys, systemd=None, parity: bool = True) -> tuple[int, str]:
    argv = ["--root", str(site["root"]), "--repo", str(site["repo"]), "--since", SINCE]
    if parity:
        argv += ["--parity", str(site["left"]), str(site["right"])]
    rc = cutover_check.main(argv, run=systemd or _systemd(0, AFTER))
    return rc, capsys.readouterr().out


def test_a_good_cutover_passes_every_line(site, capsys):
    rc, out = _run(site, capsys)
    assert rc == 0, out
    lines = out.splitlines()
    assert [line.split()[0] for line in lines[:4]] == ["PASS"] * 4
    assert [line.split()[1] for line in lines[:4]] == ["sweep:", "health:", "sync:", "parity:"]
    assert lines[4] == "CUTOVER GATE: PASS"


def test_a_sweep_from_before_the_cutover_does_not_count(site, capsys):
    os.utime(site["sweep"], (_epoch(BEFORE), _epoch(BEFORE)))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep" in out and "CUTOVER GATE: FAIL" in out


def test_a_thin_sweep_does_not_count(site, capsys):
    pd.DataFrame({"con_id": range(1999)}).to_parquet(site["sweep"], index=False)
    os.utime(site["sweep"], (_epoch(AFTER), _epoch(AFTER)))
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sweep" in out and "1999" in out


def test_a_probe_that_found_a_problem_fails_the_gate_and_says_which(site, capsys):
    with site["health"].open("a", encoding="utf-8") as log:
        log.write(f"{AFTER} STALE ibkr recorder STALE: heartbeat is 70 min old\n")
    rc, out = _run(site, capsys, systemd=_systemd(1, AFTER))
    assert rc == 1
    assert "FAIL health" in out and "exit status is 1" in out and "STALE" in out


def test_a_probe_that_has_not_run_since_the_cutover_does_not_count(site, capsys):
    rc, out = _run(site, capsys, systemd=_systemd(0, BEFORE))
    assert rc == 1
    assert "FAIL health" in out and "has not finished a run since the cutover" in out

    rc, out = _run(site, capsys, systemd=_systemd(0, None))  # the unit never ran at all
    assert rc == 1
    assert "has not finished a run since the cutover" in out


def test_systemd_that_cannot_be_asked_is_not_read_as_healthy(site, capsys):
    def no_systemctl(cmd, **_kwargs):
        raise FileNotFoundError("systemctl")

    rc, out = _run(site, capsys, systemd=no_systemctl)
    assert rc == 1
    assert "FAIL health" in out and "could not ask systemd" in out


def test_a_test_alert_that_only_reached_the_log_does_not_count(site, capsys):
    site["health"].write_text(f"{AFTER} ALERT_SENT test - log_only\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL health" in out and "no delivered test alert" in out


def test_the_sync_must_have_run_since_and_report_the_same_book(site, capsys):
    ledger = site["paper"] / "ibkr_sync_runs.jsonl"
    ledger.write_text(_sync(BEFORE) + "\n" + _sync(AFTER, unchanged=2, cancelled=1) + "\n",
                      encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and "cancelled" in out

    ledger.write_text(_sync(BEFORE) + "\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and "no sync run since" in out

    ledger.write_text(_sync(BEFORE) + "\n" + _sync(AFTER, ok=False) + "\n", encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL sync" in out and "ok is false" in out


def test_a_parity_mismatch_fails_the_gate(site, capsys):
    payload = json.loads(site["right"].read_text(encoding="utf-8"))
    payload["x"][0] += 1.0
    site["right"].write_text(json.dumps(payload), encoding="utf-8")
    rc, out = _run(site, capsys)
    assert rc == 1
    assert "FAIL parity" in out


def test_without_the_parity_payloads_the_gate_cannot_pass(site, capsys):
    rc, out = _run(site, capsys, parity=False)
    assert rc == 1
    assert "FAIL parity" in out and "not checked" in out
