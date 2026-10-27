"""Run directories: write, read, render and compare."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from climate.nn import report
from climate.nn.nowcast import CHECKPOINTS
from climate.nn.report import (
    compare_runs,
    git_commit,
    read_run,
    render_nowcast_report,
    render_seasonal_report,
    write_run,
)

LEADS = range(1, 7)
HEADER = (
    "| run | protocol | "
    + " | ".join(f"lead{k}_nn" for k in LEADS)
    + " | "
    + " | ".join(f"lead{k}_emos" for k in LEADS)
    + " | win |"
)


def _summary_records(nn_crps, emos_crps):
    """Summary rows of an nn and an emos model (a missing model has no rows)."""
    models = (("nn", nn_crps), ("emos", emos_crps))
    return [
        {
            "model": model,
            "lead_month": lead,
            "n": 30,
            "crps": crps,
            "skill_vs_trend": 0.2,
            "skill_vs_emos": 0.0,
            "cover50": 0.5,
            "cover90": 0.9,
        }
        for model, crps in models
        if crps is not None
        for lead in LEADS
    ]


def _write_synthetic_run(run_dir, *, nn_crps, emos_crps, win, protocol="strict", kind=None):
    verdict = None if win is None else [win, "" if win else "nn beats emos at 0 of 6 leads"]
    metrics = {"summary": _summary_records(nn_crps, emos_crps), "extras": {}, "win": verdict}
    config = {"protocol": protocol} if kind is None else {"protocol": protocol, "kind": kind}
    rows = pd.DataFrame({"a": [1]})
    write_run(run_dir, config=config, metrics=metrics, rows=rows, report_md="# r")


# write_run and read_run


def test_write_and_read_run_round_trip(tmp_path):
    rows = pd.DataFrame({"a": [1]})
    write_run(
        tmp_path / "r",
        config={"protocol": "strict"},
        metrics={"win": True},
        rows=rows,
        report_md="# r",
    )
    names = {p.name for p in (tmp_path / "r").iterdir()}
    assert {"config.json", "metrics.json", "rows.parquet", "report.md"} <= names
    assert read_run(tmp_path / "r")["config"]["protocol"] == "strict"


def test_read_run_returns_the_config_and_the_metrics_under_their_names(tmp_path):
    config = {"protocol": "strict", "seeds": [0, 1], "git_commit": "abc1234"}
    metrics = {"summary": [{"model": "nn", "crps": 0.04}], "extras": {"excluded_rows": 45}}
    write_run(tmp_path / "r", config=config, metrics=metrics, rows=pd.DataFrame(), report_md="")
    assert read_run(tmp_path / "r") == {"config": config, "metrics": metrics}


def test_the_report_is_written_as_given_and_the_rows_survive_the_parquet_round_trip(tmp_path):
    rows = pd.DataFrame(
        {
            "decision_utc": pd.to_datetime(["2006-01-13T12:00", "2006-02-13T12:00"], utc=True),
            "lead_month": [1, 2],
            "mu_nn": [0.25, np.nan],
        }
    )
    write_run(tmp_path / "r", config={}, metrics={}, rows=rows, report_md="# title\n\nbody\n")
    assert (tmp_path / "r" / "report.md").read_bytes() == b"# title\n\nbody\n"
    back = pd.read_parquet(tmp_path / "r" / "rows.parquet")
    pd.testing.assert_frame_equal(back, rows)


def test_the_json_files_are_strict_json_with_nan_as_null_and_numpy_as_numbers(tmp_path):
    metrics = {
        "summary": [{"n": np.int64(0), "crps": float("nan"), "cover50": np.float64(0.5)}],
        "extras": {"pit_hist": {"nn": np.array([0.25, np.nan])}, "skipped_windows": (2001,)},
    }
    write_run(
        tmp_path / "r",
        config={"gpu_budget": None},
        metrics=metrics,
        rows=pd.DataFrame(),
        report_md="",
    )

    def refuse(constant):
        raise AssertionError(f"not strict JSON: {constant}")

    text = (tmp_path / "r" / "metrics.json").read_text(encoding="utf-8")
    parsed = json.loads(text, parse_constant=refuse)
    assert parsed["summary"] == [{"n": 0, "crps": None, "cover50": 0.5}]
    assert parsed["extras"] == {"pit_hist": {"nn": [0.25, None]}, "skipped_windows": [2001]}
    assert read_run(tmp_path / "r")["metrics"]["summary"][0]["crps"] is None


def test_a_frame_in_the_metrics_is_written_as_a_list_of_records(tmp_path):
    frame = pd.DataFrame({"model": ["nn", "bridge"], "n": [3, 0], "crps": [0.05, float("nan")]})
    write_run(
        tmp_path / "r",
        config={},
        metrics={"extras": {"first_print_summary": frame}},
        rows=pd.DataFrame(),
        report_md="",
    )
    written = read_run(tmp_path / "r")["metrics"]["extras"]["first_print_summary"]
    assert written == [
        {"model": "nn", "n": 3, "crps": 0.05},
        {"model": "bridge", "n": 0, "crps": None},
    ]


def test_a_saved_model_directory_is_copied_to_weights_and_the_original_is_left_alone(tmp_path):
    model = tmp_path / "model"
    (model / "weights").mkdir(parents=True)
    (model / "manifest.json").write_text('{"seeds": [0]}', encoding="utf-8")
    (model / "weights" / "seed_0.pt").write_bytes(b"\x00\x01")
    write_run(
        tmp_path / "r", config={}, metrics={}, rows=pd.DataFrame(), report_md="", model_dir=model
    )
    copy = tmp_path / "r" / "weights"
    assert (copy / "manifest.json").read_text(encoding="utf-8") == '{"seeds": [0]}'
    assert (copy / "weights" / "seed_0.pt").read_bytes() == b"\x00\x01"
    assert (model / "manifest.json").exists() and (model / "weights" / "seed_0.pt").exists()


def test_no_weights_directory_is_made_without_a_model_directory(tmp_path):
    write_run(tmp_path / "r", config={}, metrics={}, rows=pd.DataFrame(), report_md="")
    assert not (tmp_path / "r" / "weights").exists()


def test_a_run_directory_is_never_reused(tmp_path):
    write_run(tmp_path / "r", config={"n": 1}, metrics={}, rows=pd.DataFrame(), report_md="first")
    with pytest.raises(FileExistsError):
        write_run(
            tmp_path / "r", config={"n": 2}, metrics={}, rows=pd.DataFrame(), report_md="second"
        )
    assert (tmp_path / "r" / "report.md").read_text(encoding="utf-8") == "first"
    assert read_run(tmp_path / "r")["config"] == {"n": 1}


def test_a_refused_run_directory_leaves_no_scratch_directory_beside_it(tmp_path):
    (tmp_path / "r").mkdir()
    with pytest.raises(FileExistsError, match="exists already"):
        write_run(tmp_path / "r", config={}, metrics={}, rows=pd.DataFrame(), report_md="")
    assert [p.name for p in tmp_path.iterdir()] == ["r"]


def test_a_finished_run_leaves_only_the_run_directory_beside_the_others(tmp_path):
    for name in ("a", "b"):
        write_run(tmp_path / name, config={}, metrics={}, rows=pd.DataFrame(), report_md="")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b"]


def test_the_run_directory_does_not_exist_until_every_file_is_in_it(tmp_path, monkeypatch):
    seen = []
    real = report._write_json

    def spy(path, payload):
        seen.append(((tmp_path / "r").exists(), path.parent != tmp_path / "r"))
        real(path, payload)

    monkeypatch.setattr(report, "_write_json", spy)
    write_run(tmp_path / "r", config={}, metrics={}, rows=pd.DataFrame(), report_md="")
    assert seen == [(False, True), (False, True)]
    assert (tmp_path / "r" / "metrics.json").exists()


def test_a_failure_while_writing_leaves_no_run_directory_and_no_scratch_directory(
    tmp_path, monkeypatch
):
    def disk_full(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", disk_full)
    run_dir = tmp_path / "runs" / "r"
    with pytest.raises(OSError, match="disk full"):
        write_run(run_dir, config={"n": 1}, metrics={}, rows=pd.DataFrame(), report_md="")
    assert list((tmp_path / "runs").iterdir()) == []


def test_a_failed_copy_of_the_weights_leaves_nothing_behind(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()

    def broken_copy(*args, **kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(report.shutil, "copytree", broken_copy)
    with pytest.raises(OSError, match="copy failed"):
        write_run(
            tmp_path / "r",
            config={},
            metrics={},
            rows=pd.DataFrame(),
            report_md="",
            model_dir=model,
        )
    assert [p.name for p in tmp_path.iterdir()] == ["model"]


def test_parents_of_a_new_run_directory_are_created(tmp_path):
    run_dir = tmp_path / "runs" / "deeper" / "r"
    write_run(run_dir, config={}, metrics={}, rows=pd.DataFrame(), report_md="")
    assert (run_dir / "config.json").exists()


def test_reading_a_directory_that_is_not_a_run_raises_file_not_found(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        read_run(tmp_path / "empty")


# git_commit


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    shutil.which("git") is None or not (REPO_ROOT / ".git").exists(),
    reason="needs git and a git checkout",
)
def test_git_commit_is_the_short_hash_of_this_checkout():
    assert re.fullmatch(r"[0-9a-f]{7,40}", git_commit())


def test_git_commit_is_unknown_when_git_is_missing_or_fails(monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(report.subprocess, "run", no_git)
    assert git_commit() == "unknown"

    def not_a_repository(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0])

    monkeypatch.setattr(report.subprocess, "run", not_a_repository)
    assert git_commit() == "unknown"


def test_git_commit_is_unknown_when_git_prints_nothing(monkeypatch):
    def silent(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="\n", stderr="")

    monkeypatch.setattr(report.subprocess, "run", silent)
    assert git_commit() == "unknown"


# compare_runs


def test_compare_runs_lists_every_run_with_its_win(tmp_path):
    _write_synthetic_run(tmp_path / "a", nn_crps=0.04, emos_crps=0.05, win=True)
    _write_synthetic_run(tmp_path / "b", nn_crps=0.06, emos_crps=0.05, win=False)
    table = compare_runs([tmp_path / "a", tmp_path / "b"])
    assert table.count("\n| ") == 2 and "lead1_nn" in table and "| a |" in table


def test_the_compare_table_has_the_documented_columns_and_four_decimals(tmp_path):
    _write_synthetic_run(tmp_path / "a", nn_crps=0.04, emos_crps=0.05, win=True)
    _write_synthetic_run(tmp_path / "b", nn_crps=0.06123456, emos_crps=0.05, win=False)
    lines = compare_runs([tmp_path / "a", tmp_path / "b"]).splitlines()
    assert lines[0] == HEADER
    assert lines[1] == "|" + "---|" * 15
    assert lines[2] == "| a | strict | " + " | ".join(["0.0400"] * 6 + ["0.0500"] * 6) + " | yes |"
    expected = " | ".join(["0.0612"] * 6 + ["0.0500"] * 6)
    assert lines[3] == f"| b | strict | {expected} | no: nn beats emos at 0 of 6 leads |"
    assert len(lines) == 4


def test_a_missing_model_prints_a_dash_and_a_run_without_a_verdict_has_no_win(tmp_path):
    _write_synthetic_run(
        tmp_path / "frozen", nn_crps=None, emos_crps=0.05, win=None, protocol="frozen"
    )
    line = compare_runs([tmp_path / "frozen"]).splitlines()[2]
    assert line == "| frozen | frozen | " + " | ".join(["-"] * 6 + ["0.0500"] * 6) + " | - |"


def test_a_nan_score_prints_a_dash_too(tmp_path):
    _write_synthetic_run(tmp_path / "a", nn_crps=float("nan"), emos_crps=0.05, win=False)
    cells = compare_runs([tmp_path / "a"]).splitlines()[2].split(" | ")
    assert cells[2:8] == ["-"] * 6 and cells[8] == "0.0500"


def test_the_win_cell_accepts_a_bare_boolean_and_a_missing_verdict(tmp_path):
    write_run(
        tmp_path / "bare", config={}, metrics={"win": True}, rows=pd.DataFrame(), report_md=""
    )
    write_run(tmp_path / "none", config={}, metrics={}, rows=pd.DataFrame(), report_md="")
    lines = compare_runs([tmp_path / "bare", tmp_path / "none"]).splitlines()
    assert lines[2].startswith("| bare | - | -") and lines[2].endswith("| yes |")
    assert lines[3].startswith("| none | - | -") and lines[3].endswith("| - |")


def test_a_run_directory_with_the_seasonal_kind_is_compared_like_one_without_a_kind(tmp_path):
    _write_synthetic_run(tmp_path / "plain", nn_crps=0.04, emos_crps=0.05, win=True)
    _write_synthetic_run(tmp_path / "kind", nn_crps=0.04, emos_crps=0.05, win=True, kind="seasonal")
    lines = compare_runs([tmp_path / "plain", tmp_path / "kind"]).splitlines()
    assert lines[2].split(" | ")[1:] == lines[3].split(" | ")[1:]


def test_runs_of_different_kinds_are_refused_and_name_the_runs_and_their_kinds(tmp_path):
    _write_synthetic_run(tmp_path / "a", nn_crps=0.04, emos_crps=0.05, win=True, kind="seasonal")
    _write_synthetic_run(tmp_path / "b", nn_crps=0.04, emos_crps=0.05, win=True, kind="nowcast")
    with pytest.raises(ValueError, match=r"different kinds.*a.*seasonal.*b.*nowcast"):
        compare_runs([tmp_path / "a", tmp_path / "b"])


def test_a_run_of_an_unknown_kind_is_refused(tmp_path):
    _write_synthetic_run(tmp_path / "a", nn_crps=0.04, emos_crps=0.05, win=True, kind="gbm")
    with pytest.raises(ValueError, match="unknown run kind 'gbm'"):
        compare_runs([tmp_path / "a"])


def test_runs_are_listed_in_the_order_given(tmp_path):
    for name in ("x", "y", "z"):
        _write_synthetic_run(tmp_path / name, nn_crps=0.04, emos_crps=0.05, win=True)
    lines = compare_runs([tmp_path / "z", tmp_path / "x", tmp_path / "y"]).splitlines()
    assert [line.split(" | ")[0] for line in lines[2:]] == ["| z", "| x", "| y"]


# render_seasonal_report


def _records(nn_crps=0.04):
    """Summary of nn and emos with one NaN cell for nn at lead 6."""
    rows = _summary_records(nn_crps, 0.05)
    nan_cell = next(r for r in rows if r["model"] == "nn" and r["lead_month"] == 6)
    nan_cell.update(n=0, crps=float("nan"), skill_vs_trend=float("nan"), cover50=float("nan"))
    return pd.DataFrame(rows)


def _extras(**more):
    pit = {"nn": [0.1] * 10, "emos": [0.05] * 5 + [0.15] * 5}
    return {
        "protocol": "strict",
        "excluded_rows": 45,
        "skipped_windows": [],
        "pit_hist": pit,
        **more,
    }


def test_the_strict_report_names_the_protocol_the_verdict_and_every_cell():
    text = render_seasonal_report(_records(), _extras(), (True, ""))
    assert text.startswith("# GTM seasonal benchmark: strict protocol")
    assert "Verdict: win\n" in text
    assert (
        "| model | lead | n | CRPS | skill vs trend | skill vs emos | cover50 | cover90 |" in text
    )
    assert "| nn | 1 | 30 | 0.0400 | 0.200 | 0.000 | 0.500 | 0.900 |" in text
    assert "| emos | 6 | 30 | 0.0500 | 0.200 | 0.000 | 0.500 | 0.900 |" in text
    assert "| nn | 6 | 0 | - | - | 0.000 | - | 0.900 |" in text


def test_the_report_states_the_win_rule_it_applies():
    text = render_seasonal_report(_records(), _extras(), (True, ""))
    assert "4 or more of 6 leads" in text and "[0.35, 0.65]" in text and "[0.75, 0.98]" in text


def test_a_lost_run_says_why_and_a_run_without_a_verdict_says_so():
    lost = render_seasonal_report(
        _records(0.06), _extras(), (False, "nn beats emos at 3 of 6 leads")
    )
    assert "Verdict: no win (nn beats emos at 3 of 6 leads)" in lost
    unjudged = render_seasonal_report(_records(), _extras(protocol="frozen"), None)
    assert "Verdict: none" in unjudged and "Verdict: win" not in unjudged


def test_the_frozen_report_says_it_is_not_the_headline():
    text = render_seasonal_report(_records(), _extras(protocol="frozen"), None)
    assert text.startswith("# GTM seasonal benchmark: frozen protocol")
    assert "never the headline" in text and "Rows excluded" not in text


def test_the_strict_report_counts_the_excluded_rows_and_lists_the_skipped_years():
    clean = render_seasonal_report(_records(), _extras(), (True, ""))
    assert "Rows excluded by the strict rule: 45" in clean and "Test years skipped: none" in clean
    skipped = render_seasonal_report(_records(), _extras(skipped_windows=[2001, 2002]), (True, ""))
    assert "Test years skipped: 2001, 2002" in skipped


def test_every_table_carries_the_label_vintage_note():
    text = render_seasonal_report(_records(), _extras(), (True, ""))
    assert text.count("current vintage") == 2  # the scores table and the PIT table


def test_the_pit_table_has_a_row_of_ten_frequencies_per_model():
    text = render_seasonal_report(_records(), _extras(), (True, ""))
    assert "| model | 0.0-0.1 | 0.1-0.2 | 0.2-0.3 |" in text and "| 0.9-1.0 |" in text
    assert "| nn | " + " | ".join(["0.100"] * 10) + " |" in text
    assert "| emos | " + " | ".join(["0.050"] * 5 + ["0.150"] * 5) + " |" in text


def test_a_model_without_pit_frequencies_prints_dashes():
    extras = _extras()
    extras["pit_hist"]["nn"] = [None] * 10
    text = render_seasonal_report(_records(), extras, (True, ""))
    assert "| nn | " + " | ".join(["-"] * 10) + " |" in text


def test_the_report_needs_none_of_the_optional_extras():
    text = render_seasonal_report(_records(), {"protocol": "strict"}, (True, ""))
    assert text.startswith("# GTM seasonal benchmark: strict protocol")
    assert "Test years skipped: none" in text


def test_the_report_does_not_assume_a_protocol_the_run_did_not_name():
    with pytest.raises(KeyError, match="protocol"):
        render_seasonal_report(_records(), {}, (True, ""))


def test_the_report_is_valid_markdown_tables_with_matching_column_counts():
    text = render_seasonal_report(_records(), _extras(), (False, "cover50 at lead 2"))
    tables = [block for block in text.split("\n\n") if block.startswith("|")]
    assert len(tables) == 2
    for block in tables:
        counts = {line.count("|") for line in block.splitlines()}
        assert len(counts) == 1


# nowcast runs: compare and report

NOWCAST_HEADER = (
    "| run | cp10_nn | cp10_bridge | cp25_nn | cp25_bridge | cp+3_nn | cp+3_bridge | win |"
)


def _nowcast_records(nn_crps=0.04, bridge_crps=0.05, n=30, models=("nn", "bridge")):
    """Summary rows of the models over the nine checkpoints; nn has no score at checkpoint 5."""
    crps = {"nn": nn_crps, "bridge": bridge_crps, "persistence": 0.08}
    rows = [
        {
            "model": model,
            "checkpoint": checkpoint,
            "n": n,
            "crps": crps[model],
            "skill_vs_bridge": 0.2,
            "cover50": 0.5,
            "cover90": 0.9,
        }
        for model in models
        for checkpoint in CHECKPOINTS
        if crps[model] is not None
    ]
    for row in rows:
        if row["model"] == "nn" and row["checkpoint"] == "5":
            row.update(n=0, crps=float("nan"), skill_vs_bridge=float("nan"), cover50=float("nan"))
    return rows


def _write_nowcast_run(run_dir, *, nn_crps, bridge_crps, win, protocol="strict"):
    verdict = None if win is None else [win, "" if win else "nn does not beat bridge at 10"]
    summary = _nowcast_records(nn_crps, bridge_crps)
    metrics = {"summary": summary, "extras": {}, "win": verdict}
    config = {"kind": "nowcast", "protocol": protocol}
    write_run(run_dir, config=config, metrics=metrics, rows=pd.DataFrame(), report_md="# r")


def test_the_nowcast_compare_table_has_the_documented_columns_and_four_decimals(tmp_path):
    _write_nowcast_run(tmp_path / "a", nn_crps=0.04, bridge_crps=0.05, win=True)
    _write_nowcast_run(tmp_path / "b", nn_crps=0.06123456, bridge_crps=0.05, win=False)
    lines = compare_runs([tmp_path / "a", tmp_path / "b"]).splitlines()
    assert lines[0] == NOWCAST_HEADER
    assert lines[1] == "|" + "---|" * 8
    assert lines[2] == "| a | " + " | ".join(["0.0400", "0.0500"] * 3) + " | yes |"
    expected = " | ".join(["0.0612", "0.0500"] * 3)
    assert lines[3] == f"| b | {expected} | no: nn does not beat bridge at 10 |"
    assert len(lines) == 4


def test_a_nowcast_run_without_a_verdict_or_a_model_prints_dashes(tmp_path):
    _write_nowcast_run(
        tmp_path / "base", nn_crps=None, bridge_crps=0.05, win=None, protocol="frozen"
    )
    line = compare_runs([tmp_path / "base"]).splitlines()[2]
    assert line == "| base | " + " | ".join(["-", "0.0500"] * 3) + " | - |"


def test_runs_of_one_kind_are_compared_and_the_nowcast_kind_does_not_mix_with_the_seasonal_one(
    tmp_path,
):
    _write_nowcast_run(tmp_path / "n1", nn_crps=0.04, bridge_crps=0.05, win=True)
    _write_nowcast_run(tmp_path / "n2", nn_crps=0.04, bridge_crps=0.05, win=True)
    _write_synthetic_run(tmp_path / "s", nn_crps=0.04, emos_crps=0.05, win=True)
    assert compare_runs([tmp_path / "n1", tmp_path / "n2"]).count("\n| ") == 2
    with pytest.raises(ValueError, match=r"different kinds.*n1 \(nowcast\).*s \(seasonal\)"):
        compare_runs([tmp_path / "n1", tmp_path / "s"])


def _first_print(n=2):
    return pd.DataFrame(_nowcast_records(0.045, 0.05, n=n))


def _nowcast_extras(**more):
    pit = {"nn": [0.1] * 10, "bridge": [0.05] * 5 + [0.15] * 5}
    return {
        "protocol": "strict",
        "kind": "nowcast",
        "bridge_through": None,
        "dropped_gap_rows": 3,
        "excluded_rows": 4,
        "skipped_windows": [],
        "pit_hist": pit,
        "first_print_summary": _first_print(),
        **more,
    }


def _nowcast_summary(nn_crps=0.04):
    return pd.DataFrame(_nowcast_records(nn_crps, 0.05, models=("nn", "bridge", "persistence")))


def test_the_nowcast_report_names_the_protocol_the_verdict_and_every_cell():
    text = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    assert text.startswith("# GTM nowcast benchmark: strict protocol")
    assert "Verdict: win\n" in text
    assert "| model | checkpoint | n | CRPS | skill vs bridge | cover50 | cover90 |" in text
    assert "| nn | 10 | 30 | 0.0400 | 0.200 | 0.500 | 0.900 |" in text
    assert "| bridge | +3 | 30 | 0.0500 | 0.200 | 0.500 | 0.900 |" in text
    assert "| nn | 5 | 0 | - | - | - | 0.900 |" in text
    assert text.count("| persistence | ") == 9


def test_the_nowcast_report_states_the_win_rule_it_applies():
    text = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    assert "10, 15, 20, 25, +1, +3" in text and "at least 3 rows" in text
    assert "current-vintage" in text and "below" in text


def test_a_lost_run_says_why_and_a_run_without_a_verdict_says_so_in_the_nowcast_report():
    lost = render_nowcast_report(
        _nowcast_summary(0.06), _nowcast_extras(), (False, "nn does not beat bridge at 10")
    )
    assert "Verdict: no win (nn does not beat bridge at 10)" in lost
    unjudged = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), None)
    assert "Verdict: none" in unjudged and "Verdict: win" not in unjudged


def test_the_nowcast_report_has_a_table_of_the_genuine_first_prints_after_the_main_one():
    text = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    main, first = text.index("## CRPS and calibration"), text.index("## Genuine first prints")
    assert main < first < text.index("## PIT histogram")
    assert "| nn | 10 | 2 | 0.0450 | 0.200 | 0.500 | 0.900 |" in text[first:]
    records = render_nowcast_report(
        _nowcast_summary(),
        _nowcast_extras(first_print_summary=_first_print().to_dict("records")),
        (True, ""),
    )
    assert records == text  # a run read back from metrics.json renders the same


def test_a_nowcast_report_without_a_scored_first_print_says_so_instead_of_a_table_of_dashes():
    unscored = _first_print(n=0)
    text = render_nowcast_report(
        _nowcast_summary(), _nowcast_extras(first_print_summary=unscored), (True, "")
    )
    assert "No genuine first print has been scored yet." in text
    assert text.count("| model | checkpoint |") == 1  # the scores table only


def test_the_nowcast_report_says_in_one_line_that_persistence_is_too_tight_at_two_checkpoints():
    text = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    notes = [line for line in text.splitlines() if "Persistence" in line]
    assert len(notes) == 1
    assert "one-month changes" in notes[0] and "two months old" in notes[0]
    assert "too tight" in notes[0] and "compares against the bridge" in notes[0]


def test_the_nowcast_report_says_why_checkpoint_five_has_no_prediction():
    text = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    assert any("Checkpoint 5" in line and "no ERA5 day" in line for line in text.splitlines())


def test_the_strict_nowcast_report_describes_the_windows_and_the_frozen_one_the_bridge_month():
    strict = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    assert "Expanding windows by target year" in strict and "never the headline" not in strict
    assert "decided on or after the publication of the last training label" in strict
    frozen = render_nowcast_report(
        _nowcast_summary(),
        _nowcast_extras(protocol="frozen", bridge_through="2025-12"),
        None,
    )
    assert frozen.startswith("# GTM nowcast benchmark: frozen protocol")
    assert "2025-12" in frozen and "never the headline" in frozen
    assert "Expanding windows by target year" not in frozen
    assert "same admission rule" in frozen


def test_the_nowcast_report_counts_the_excluded_rows_in_a_strict_and_in_a_frozen_run():
    strict = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    assert "Rows excluded by the strict rule: 4" in strict
    frozen = _nowcast_extras(protocol="frozen", bridge_through="2025-12")
    assert "Rows excluded by the strict rule: 4" in render_nowcast_report(
        _nowcast_summary(), frozen, None
    )
    bare = render_nowcast_report(_nowcast_summary(), {"protocol": "strict"}, (True, ""))
    assert "Rows excluded" not in bare


def test_the_nowcast_report_counts_the_dropped_rows_and_lists_the_skipped_years():
    clean = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    assert "Test rows dropped for a gap day in the ERA5 series: 3" in clean
    assert "Test years skipped: none" in clean
    skipped = render_nowcast_report(
        _nowcast_summary(), _nowcast_extras(skipped_windows=[2002, 2003]), (True, "")
    )
    assert "Test years skipped: 2002, 2003" in skipped


def test_every_nowcast_table_carries_the_label_vintage_note_and_the_pit_table_has_ten_bins():
    text = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (True, ""))
    assert text.count("current vintage") == 2  # the scores table and the PIT table
    assert "| model | 0.0-0.1 | 0.1-0.2 | 0.2-0.3 |" in text
    assert "| nn | " + " | ".join(["0.100"] * 10) + " |" in text


def test_the_nowcast_report_is_valid_markdown_tables_with_matching_column_counts():
    text = render_nowcast_report(_nowcast_summary(), _nowcast_extras(), (False, "x"))
    tables = [block for block in text.split("\n\n") if block.startswith("|")]
    assert len(tables) == 3
    for block in tables:
        assert len({line.count("|") for line in block.splitlines()}) == 1


def test_the_nowcast_report_needs_the_protocol_but_none_of_the_other_extras():
    with pytest.raises(KeyError, match="protocol"):
        render_nowcast_report(_nowcast_summary(), {}, (True, ""))
    text = render_nowcast_report(_nowcast_summary(), {"protocol": "strict"}, (True, ""))
    assert text.startswith("# GTM nowcast benchmark: strict protocol")
    assert "Test years skipped: none" in text and "Genuine first prints" in text
