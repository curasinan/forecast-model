"""The two research scripts: exit codes, run directories and the torch-free paths."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analysis.gpu_budget import GpuBudgetError
from climate.nn import benchmark
from climate.nn.benchmark import NowcastRun, SeasonalRun, run_nowcast, run_seasonal
from climate.nn.report import compare_runs, read_run, write_run
from climate.nn.seasonal_model import GtmNnModel
from scripts.research import gtm_nn_benchmark, gtm_nowcast_benchmark, nn_cli
from scripts.research.gtm_nn_benchmark import main
from scripts.research.gtm_nn_compare import main as compare_main
from scripts.research.gtm_nowcast_benchmark import main as nowcast_main
from tests.test_climate import nn_fixtures

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "research"


class _BlockTorch:
    """A meta-path finder that makes `import torch` fail as on a machine without it."""

    def find_spec(self, name, path=None, target=None):
        if name == "torch" or name.startswith("torch."):
            raise ModuleNotFoundError("torch is blocked", name=name)


@pytest.fixture
def no_torch(monkeypatch):
    """A machine without torch: the import machinery refuses it, whatever was imported before."""
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockTorch(), *sys.meta_path])


def _args(root, run_dir, *more):
    return ["--root", str(root), "--run-dir", str(run_dir), *more]


FAST_YEARS = ["--first-test-year", "2012", "--last-test-year", "2013"]


def _spy_on_run_seasonal(monkeypatch):
    """Record every SeasonalRun the script runs; the run itself is a cheap frozen one."""
    seen = []

    def spy(inputs, ledger, cfg, *, model_dir=None):
        seen.append(cfg)
        return run_seasonal(inputs, ledger, replace(cfg, protocol="frozen"), model_dir=model_dir)

    monkeypatch.setattr(gtm_nn_benchmark, "run_seasonal", spy)
    return seen


def _device_must_not_be_configured(budget):
    raise AssertionError("configure_device was called without a GPU budget")


# the benchmark script


def test_benchmark_cli_refuses_without_torch(tmp_path, no_torch, capsys):
    run_dir = tmp_path / "r"
    argv = ["--protocol", "strict", "--models", "nn,emos"]
    rc = main(_args(tmp_path / "missing", run_dir, *argv))  # a root that would fail if it were read
    captured = capsys.readouterr()
    assert rc == 2 and not run_dir.exists()
    assert captured.err.strip() == (
        'research extra not installed: uv pip install -e ".[climate,research]" '
        "into ~/work/wx-dl/.venv"
    )
    assert captured.out == ""


def test_benchmark_cli_frozen_run_writes_a_run_dir(tmp_path, synthetic_root):
    argv = ["--protocol", "frozen", "--first-test-year", "2006", "--last-test-year", "2008"]
    rc = main(_args(synthetic_root, tmp_path / "r", *argv))
    assert rc == 0 and "frozen" in (tmp_path / "r" / "report.md").read_text(encoding="utf-8")


def test_the_frozen_cli_run_needs_no_torch_and_records_its_options_and_the_library_numbers(
    tmp_path, synthetic_root, synthetic_inputs, no_torch, capsys
):
    run_dir = tmp_path / "r"
    argv = ["--protocol", "frozen", "--first-test-year", "2006", "--last-test-year", "2008"]
    assert main(_args(synthetic_root, run_dir, *argv)) == 0
    run = read_run(run_dir)
    config = run["config"]
    assert config["kind"] == "seasonal" and config["protocol"] == "frozen"
    assert config["models"] == ["emos", "trend", "clim"]
    assert (config["first_test_year"], config["last_test_year"]) == (2006, 2008)
    assert config["seeds"] == [0, 1, 2, 3, 4] and config["hidden"] == 32
    assert config["gpu_budget"] is None and config["device"] == "cpu"
    assert Path(config["root"]) == synthetic_root.resolve() and config["run_dir"] == str(run_dir)
    assert re.fullmatch(r"[0-9a-f]{7,40}|unknown", config["git_commit"])
    assert run["metrics"]["win"] is None
    cfg = SeasonalRun("frozen", ("emos",), 2006, 2008, (), 32, "cpu")
    expected = run_seasonal(synthetic_inputs, None, cfg)[1]
    got = pd.DataFrame(run["metrics"]["summary"])
    np.testing.assert_allclose(got["crps"], expected["crps"], rtol=0, atol=1e-12)
    assert len(pd.read_parquet(run_dir / "rows.parquet")) == 3 * 72
    assert not (run_dir / "weights").exists()
    out = capsys.readouterr().out
    assert out.startswith("# GTM seasonal benchmark: frozen protocol") and str(run_dir) in out


def test_benchmark_cli_exits_3_on_leakage(tmp_path, synthetic_root, monkeypatch, capsys):
    monkeypatch.setattr(
        "climate.nn.benchmark.strict_windows", nn_fixtures.windows_with_a_late_input
    )
    argv = ["--protocol", "strict", "--models", "emos"]
    argv += ["--first-test-year", "2006", "--last-test-year", "2006"]
    rc = main(_args(synthetic_root, tmp_path / "r", *argv))
    err = capsys.readouterr().err
    assert rc == 3 and not (tmp_path / "r").exists()
    assert "2006" in err and "row 0" in err and "avail_seas5_utc" in err


def test_a_strict_baseline_run_needs_no_torch_and_records_no_verdict(
    tmp_path, synthetic_root, no_torch
):
    run_dir = tmp_path / "r"
    argv = ["--protocol", "strict", "--models", "emos,raw,trend"]
    argv += ["--first-test-year", "2006", "--last-test-year", "2007"]
    assert main(_args(synthetic_root, run_dir, *argv)) == 0
    run = read_run(run_dir)
    assert run["config"]["models"] == ["emos", "raw", "trend"]
    assert len(run["metrics"]["summary"]) == 18 and run["metrics"]["win"] is None
    assert run["metrics"]["extras"]["excluded_rows"] == 30
    assert run["metrics"]["extras"]["skipped_windows"] == []
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# GTM seasonal benchmark: strict protocol")
    assert "Verdict: none" in report and not (run_dir / "weights").exists()


def test_a_strict_network_run_records_the_verdict_and_saves_the_weights(
    tmp_path, synthetic_root, monkeypatch
):
    pytest.importorskip("torch")
    monkeypatch.setattr(nn_cli, "configure_device", _device_must_not_be_configured)
    run_dir = tmp_path / "r"
    argv = ["--protocol", "strict", "--models", "nn,emos", "--seeds", "0", "--hidden", "16"]
    argv += ["--first-test-year", "2010", "--last-test-year", "2011"]
    assert main(_args(synthetic_root, run_dir, *argv)) == 0
    run = read_run(run_dir)
    assert run["config"]["device"] == "cpu" and run["config"]["gpu_budget"] is None
    assert run["config"]["seeds"] == [0] and run["config"]["hidden"] == 16
    ok, why = run["metrics"]["win"]
    assert isinstance(ok, bool) and isinstance(why, str)
    assert {r["model"] for r in run["metrics"]["summary"]} == {"nn", "emos"}
    saved = GtmNnModel.load(run_dir / "weights")
    assert (
        saved.seeds == (0,) and saved.hidden == 16 and saved.validation_years == [2008, 2009, 2010]
    )
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    verdict = "Verdict: win\n" if ok else f"Verdict: no win ({why})"
    assert verdict in report


def test_the_options_default_as_documented(tmp_path, synthetic_root, monkeypatch):
    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    monkeypatch.setattr(nn_cli, "configure_device", _device_must_not_be_configured)
    seen = _spy_on_run_seasonal(monkeypatch)
    assert main(_args(synthetic_root, tmp_path / "r")) == 0
    last_year = datetime.now(timezone.utc).year
    models = ("nn", "emos", "raw", "trend")
    assert seen == [SeasonalRun("strict", models, 2001, last_year, (0, 1, 2, 3, 4), 32, "cpu")]


@pytest.mark.parametrize("models", ["nn,raw", "nn,trend", "emos,raw,trend"])
def test_a_strict_run_without_both_the_network_and_emos_records_no_verdict(
    models, tmp_path, synthetic_root, monkeypatch
):
    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    _spy_on_run_seasonal(monkeypatch)  # the run is a cheap frozen one, whatever the models say
    assert main(_args(synthetic_root, tmp_path / "r", "--models", models, *FAST_YEARS)) == 0
    assert read_run(tmp_path / "r")["metrics"]["win"] is None
    assert "Verdict: none" in (tmp_path / "r" / "report.md").read_text(encoding="utf-8")


def test_a_strict_run_with_the_network_and_emos_records_a_verdict(
    tmp_path, synthetic_root, monkeypatch
):
    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    _spy_on_run_seasonal(monkeypatch)
    assert main(_args(synthetic_root, tmp_path / "r", "--models", "emos,nn", *FAST_YEARS)) == 0
    ok, why = read_run(tmp_path / "r")["metrics"]["win"]
    assert ok is False and "needs both" in why  # the frozen stand-in has no network rows


@pytest.mark.parametrize(
    "flags, device, budgets",
    [([], "cpu", []), (["--gpu-budget", "4"], "cuda:test", [4.0])],
    ids=["no-budget", "budget"],
)
def test_the_device_comes_from_configure_device_only_when_a_budget_is_given(
    flags, device, budgets, tmp_path, synthetic_root, monkeypatch
):
    asked = []

    def configure(budget):
        asked.append(budget)
        return "cuda:test"

    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    monkeypatch.setattr(nn_cli, "configure_device", configure)
    seen = _spy_on_run_seasonal(monkeypatch)
    argv = ["--models", "nn,emos", *FAST_YEARS, *flags]
    assert main(_args(synthetic_root, tmp_path / "r", *argv)) == 0
    assert asked == budgets and seen[0].device == device
    config = read_run(tmp_path / "r")["config"]
    assert config["device"] == device and config["gpu_budget"] == (budgets or [None])[0]


def test_the_budget_is_not_applied_to_a_run_without_the_network(
    tmp_path, synthetic_root, monkeypatch
):
    monkeypatch.setattr(nn_cli, "configure_device", _device_must_not_be_configured)
    argv = ["--protocol", "frozen", "--first-test-year", "2006", "--last-test-year", "2007"]
    argv += ["--gpu-budget", "4"]
    assert main(_args(synthetic_root, tmp_path / "r", *argv)) == 0
    assert read_run(tmp_path / "r")["config"]["device"] == "cpu"


def test_a_refused_gpu_budget_exits_2_before_anything_is_written(
    tmp_path, synthetic_root, monkeypatch, capsys
):
    def refuse(budget):
        raise GpuBudgetError("budget_gib=64 asks for more than the launcher's 8")

    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    monkeypatch.setattr(nn_cli, "configure_device", refuse)
    argv = ["--models", "nn,emos", "--gpu-budget", "64", *FAST_YEARS]
    rc = main(_args(synthetic_root, tmp_path / "r", *argv))
    assert rc == 2 and not (tmp_path / "r").exists()
    assert "launcher's 8" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--models", "emos,gbm"],
        ["--models", ""],
        ["--models", "emos,emos"],
        ["--protocol", "loose"],
        ["--seeds", "0,a"],
        ["--seeds", ""],
        ["--hidden", "0"],
        ["--gpu-budget", "0"],
        ["--models", "emos", "--first-test-year", "2009", "--last-test-year", "2008"],
        ["--no-such-option"],
    ],
    ids=lambda extra: " ".join(extra) or "empty",
)
def test_bad_options_exit_2_and_write_nothing(extra, tmp_path, synthetic_root, capsys):
    assert main(_args(synthetic_root, tmp_path / "r", *extra)) == 2
    assert not (tmp_path / "r").exists() and capsys.readouterr().err != ""


def test_a_first_test_year_after_the_last_names_both(tmp_path, synthetic_root, capsys):
    argv = ["--models", "emos", "--first-test-year", "2009", "--last-test-year", "2008"]
    assert main(_args(synthetic_root, tmp_path / "r", *argv)) == 2
    err = capsys.readouterr().err
    assert "--first-test-year 2009" in err and "2008" in err


def test_help_returns_zero(capsys):
    assert main(["--help"]) == 0
    assert "--protocol" in capsys.readouterr().out


def test_a_root_without_the_research_files_exits_2_and_names_it(tmp_path, capsys):
    argv = ["--protocol", "frozen", "--first-test-year", "2006", "--last-test-year", "2007"]
    rc = main(_args(tmp_path / "nothing", tmp_path / "r", *argv))
    assert rc == 2 and not (tmp_path / "r").exists()
    assert "nothing" in capsys.readouterr().err


def test_a_root_without_labels_exits_2(tmp_path, synthetic_root, capsys):
    (synthetic_root / "labels" / "labels_ledger.parquet").unlink()
    argv = ["--protocol", "frozen", "--first-test-year", "2006", "--last-test-year", "2007"]
    assert main(_args(synthetic_root, tmp_path / "r", *argv)) == 2
    assert "labels" in capsys.readouterr().err and not (tmp_path / "r").exists()


def test_an_existing_run_directory_is_refused_and_left_alone(tmp_path, synthetic_root, capsys):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "keep.txt").write_text("kept", encoding="utf-8")
    argv = ["--protocol", "frozen", "--first-test-year", "2006", "--last-test-year", "2007"]
    assert main(_args(synthetic_root, tmp_path / "r", *argv)) == 2
    assert [p.name for p in (tmp_path / "r").iterdir()] == ["keep.txt"]
    assert "exists" in capsys.readouterr().err


def test_nothing_to_score_exits_2_and_writes_nothing(tmp_path, synthetic_root, capsys):
    argv = ["--protocol", "strict", "--models", "emos", "--first-test-year", "2030"]
    argv += ["--last-test-year", "2031"]
    assert main(_args(synthetic_root, tmp_path / "r", *argv)) == 2
    assert not (tmp_path / "r").exists() and "no test row" in capsys.readouterr().err


def test_the_default_run_directory_is_under_runs_in_the_home_directory(
    tmp_path, synthetic_root, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    argv = ["--root", str(synthetic_root), "--protocol", "frozen", "--first-test-year", "2006"]
    assert main([*argv, "--last-test-year", "2007"]) == 0
    (made,) = (home / "runs").iterdir()
    assert re.fullmatch(r"gtm_nn_frozen_\d{8}T\d{6}Z", made.name)
    assert (made / "metrics.json").exists()


def test_the_root_defaults_to_the_climate_data_root_variable(tmp_path, synthetic_root):
    argv = ["--protocol", "frozen", "--first-test-year", "2006", "--last-test-year", "2007"]
    assert main(["--run-dir", str(tmp_path / "r"), *argv]) == 0  # synthetic_root set the variable
    assert Path(read_run(tmp_path / "r")["config"]["root"]) == synthetic_root.resolve()


# the nowcast benchmark script

NOWCAST_YEARS = ["--first-test-year", "2010", "--last-test-year", "2011"]


def _spy_on_run_nowcast(monkeypatch):
    """Record every NowcastRun and bridge month the script runs; the run itself is a cheap one."""
    seen = []

    def spy(daily_K, noaa_C, seas5, ledger, cfg, *, bridge_through=None, model_dir=None):
        seen.append((cfg, bridge_through))
        cheap = replace(cfg, models=("bridge",), first_test_year=2011, last_test_year=2011)
        return run_nowcast(
            daily_K,
            noaa_C,
            seas5,
            ledger,
            cheap,
            bridge_through=bridge_through,
            model_dir=model_dir,
        )

    monkeypatch.setattr(gtm_nowcast_benchmark, "run_nowcast", spy)
    return seen


def test_nowcast_cli_refuses_without_torch(tmp_path, no_torch, capsys):
    run_dir = tmp_path / "r"
    rc = nowcast_main(_args(tmp_path / "missing", run_dir))  # nn is in the default models
    captured = capsys.readouterr()
    assert rc == 2 and not run_dir.exists() and captured.out == ""
    assert captured.err.strip() == nn_cli.TORCH_MISSING


def test_a_nowcast_baseline_run_needs_no_torch_records_its_options_and_no_verdict(
    tmp_path, nowcast_root, no_torch, capsys
):
    run_dir = tmp_path / "r"
    argv = ["--models", "bridge,persistence", *NOWCAST_YEARS]
    assert nowcast_main(_args(nowcast_root, run_dir, *argv)) == 0
    run = read_run(run_dir)
    config = run["config"]
    assert config["kind"] == "nowcast" and config["protocol"] == "strict"
    assert config["models"] == ["bridge", "persistence"] and config["bridge_through"] is None
    assert (config["first_test_year"], config["last_test_year"]) == (2010, 2011)
    assert config["seeds"] == [0, 1, 2, 3, 4] and config["hidden"] == 32
    assert config["gpu_budget"] is None and config["device"] == "cpu"
    assert Path(config["root"]) == nowcast_root.resolve() and config["run_dir"] == str(run_dir)
    assert re.fullmatch(r"[0-9a-f]{7,40}|unknown", config["git_commit"])
    metrics = run["metrics"]
    assert metrics["win"] is None and len(metrics["summary"]) == 18
    assert metrics["extras"]["protocol"] == "strict" and metrics["extras"]["kind"] == "nowcast"
    assert len(metrics["extras"]["first_print_summary"]) == 18
    assert len(pd.read_parquet(run_dir / "rows.parquet")) == 2 * 12 * 9
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# GTM nowcast benchmark: strict protocol")
    assert "Verdict: none" in report and not (run_dir / "weights").exists()
    printed = capsys.readouterr()
    assert printed.out.startswith("# GTM nowcast benchmark") and str(run_dir) in printed.out
    assert "test year 2010: n_train=" in printed.err and "test year 2011" in printed.err


def test_a_nowcast_network_run_records_the_verdict_and_saves_the_weights(
    tmp_path, nowcast_root, monkeypatch
):
    pytest.importorskip("torch")
    from climate.nn.nowcast_model import GtmNowcastModel

    monkeypatch.setattr(nn_cli, "configure_device", _device_must_not_be_configured)
    run_dir = tmp_path / "r"
    argv = ["--models", "nn,bridge", "--seeds", "0", "--hidden", "8", *NOWCAST_YEARS]
    assert nowcast_main(_args(nowcast_root, run_dir, *argv)) == 0
    run = read_run(run_dir)
    ok, why = run["metrics"]["win"]
    assert isinstance(ok, bool) and isinstance(why, str)
    assert run["config"]["device"] == "cpu" and run["config"]["models"] == ["nn", "bridge"]
    assert {r["model"] for r in run["metrics"]["summary"]} == {"nn", "bridge"}
    saved = GtmNowcastModel.load(run_dir / "weights")
    assert saved.seeds == (0,) and saved.hidden == 8
    assert saved.validation_years == [2008, 2009, 2010]
    verdict = "Verdict: win\n" if ok else f"Verdict: no win ({why})"
    assert verdict in (run_dir / "report.md").read_text(encoding="utf-8")


def test_a_nowcast_run_with_a_bridge_month_is_a_frozen_run_without_a_verdict(
    tmp_path, nowcast_root, monkeypatch
):
    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    seen = _spy_on_run_nowcast(monkeypatch)
    argv = ["--models", "nn,bridge", "--bridge-through", "2011-11", *NOWCAST_YEARS]
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *argv)) == 0
    run = read_run(tmp_path / "r")
    assert run["config"]["protocol"] == "frozen" and run["config"]["bridge_through"] == "2011-11"
    assert run["metrics"]["win"] is None and seen[0][1] == "2011-11"
    report = (tmp_path / "r" / "report.md").read_text(encoding="utf-8")
    assert "frozen protocol" in report and "Verdict: none" in report


@pytest.mark.parametrize("models", ["nn", "bridge,persistence", "nn,persistence"])
def test_a_nowcast_run_without_both_the_network_and_the_bridge_records_no_verdict(
    models, tmp_path, nowcast_root, monkeypatch
):
    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    _spy_on_run_nowcast(monkeypatch)
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", "--models", models)) == 0
    assert read_run(tmp_path / "r")["metrics"]["win"] is None


def test_a_nowcast_run_with_the_network_and_the_bridge_records_a_verdict(
    tmp_path, nowcast_root, monkeypatch
):
    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    _spy_on_run_nowcast(monkeypatch)
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", "--models", "bridge,nn")) == 0
    ok, why = read_run(tmp_path / "r")["metrics"]["win"]
    assert ok is False and "needs both" in why  # the cheap stand-in has no network rows


def test_the_nowcast_options_default_as_documented(tmp_path, nowcast_root, monkeypatch):
    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    monkeypatch.setattr(nn_cli, "configure_device", _device_must_not_be_configured)
    seen = _spy_on_run_nowcast(monkeypatch)
    assert nowcast_main(_args(nowcast_root, tmp_path / "r")) == 0
    last_year = datetime.now(timezone.utc).year
    models = ("nn", "bridge", "persistence")
    assert seen == [(NowcastRun(models, 2001, last_year, (0, 1, 2, 3, 4), 32, "cpu"), None)]


@pytest.mark.parametrize(
    "flags, device, budgets",
    [([], "cpu", []), (["--gpu-budget", "4"], "cuda:test", [4.0])],
    ids=["no-budget", "budget"],
)
def test_the_nowcast_device_comes_from_configure_device_only_when_a_budget_is_given(
    flags, device, budgets, tmp_path, nowcast_root, monkeypatch
):
    asked = []

    def configure(budget):
        asked.append(budget)
        return "cuda:test"

    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    monkeypatch.setattr(nn_cli, "configure_device", configure)
    seen = _spy_on_run_nowcast(monkeypatch)
    argv = ["--models", "nn,bridge", *NOWCAST_YEARS, *flags]
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *argv)) == 0
    assert asked == budgets and seen[0][0].device == device
    config = read_run(tmp_path / "r")["config"]
    assert config["device"] == device and config["gpu_budget"] == (budgets or [None])[0]


def test_the_budget_is_not_applied_to_a_nowcast_run_without_the_network(
    tmp_path, nowcast_root, monkeypatch
):
    monkeypatch.setattr(nn_cli, "configure_device", _device_must_not_be_configured)
    argv = ["--models", "bridge", "--gpu-budget", "4", *NOWCAST_YEARS]
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *argv)) == 0
    assert read_run(tmp_path / "r")["config"]["device"] == "cpu"


def test_a_refused_gpu_budget_exits_2_before_the_nowcast_script_writes_anything(
    tmp_path, nowcast_root, monkeypatch, capsys
):
    def refuse(budget):
        raise GpuBudgetError("budget_gib=64 asks for more than the launcher's 8")

    monkeypatch.setattr(nn_cli, "torch_available", lambda: True)
    monkeypatch.setattr(nn_cli, "configure_device", refuse)
    argv = ["--models", "nn,bridge", "--gpu-budget", "64", *NOWCAST_YEARS]
    rc = nowcast_main(_args(nowcast_root, tmp_path / "r", *argv))
    assert rc == 2 and not (tmp_path / "r").exists()
    assert "launcher's 8" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--models", "bridge,gbm"],
        ["--models", "emos"],
        ["--models", ""],
        ["--models", "bridge,bridge"],
        ["--seeds", "0,a"],
        ["--seeds", ""],
        ["--hidden", "0"],
        ["--gpu-budget", "0"],
        ["--bridge-through", "2011-13"],
        ["--bridge-through", "2011"],
        ["--models", "bridge", "--first-test-year", "2011", "--last-test-year", "2010"],
        ["--protocol", "frozen"],
        ["--no-such-option"],
    ],
    ids=lambda extra: " ".join(extra) or "empty",
)
def test_bad_nowcast_options_exit_2_and_write_nothing(extra, tmp_path, nowcast_root, capsys):
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *extra)) == 2
    assert not (tmp_path / "r").exists() and capsys.readouterr().err != ""


def test_a_first_test_year_after_the_last_names_both_in_the_nowcast_script(
    tmp_path, nowcast_root, capsys
):
    argv = ["--models", "bridge", "--first-test-year", "2011", "--last-test-year", "2010"]
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *argv)) == 2
    err = capsys.readouterr().err
    assert "--first-test-year 2011" in err and "2010" in err


def test_nowcast_help_returns_zero(capsys):
    assert nowcast_main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "--bridge-through" in out and "nn,bridge,persistence" in out


def test_a_nowcast_root_without_the_research_files_exits_2_and_names_it(tmp_path, capsys):
    argv = ["--models", "bridge", *NOWCAST_YEARS]
    rc = nowcast_main(_args(tmp_path / "nothing", tmp_path / "r", *argv))
    assert rc == 2 and not (tmp_path / "r").exists()
    assert "nothing" in capsys.readouterr().err


def test_a_nowcast_root_without_the_daily_series_exits_2_and_names_the_file(
    tmp_path, nowcast_root, capsys
):
    (nowcast_root / "reference" / "era5_daily_global_mean.parquet").unlink()
    argv = ["--models", "bridge", *NOWCAST_YEARS]
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *argv)) == 2
    assert "era5_daily_global_mean" in capsys.readouterr().err and not (tmp_path / "r").exists()


def test_a_nowcast_root_without_labels_exits_2(tmp_path, nowcast_root, capsys):
    (nowcast_root / "labels" / "labels_ledger.parquet").unlink()
    argv = ["--models", "bridge", *NOWCAST_YEARS]
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *argv)) == 2
    assert "labels" in capsys.readouterr().err and not (tmp_path / "r").exists()


def test_an_existing_run_directory_is_refused_and_left_alone_by_the_nowcast_script(
    tmp_path, nowcast_root, capsys
):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "keep.txt").write_text("kept", encoding="utf-8")
    argv = ["--models", "bridge", *NOWCAST_YEARS]
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *argv)) == 2
    assert [p.name for p in (tmp_path / "r").iterdir()] == ["keep.txt"]
    assert "exists" in capsys.readouterr().err


def test_nothing_to_score_exits_2_and_writes_nothing_in_the_nowcast_script(
    tmp_path, nowcast_root, capsys
):
    argv = ["--models", "bridge", "--first-test-year", "2030", "--last-test-year", "2031"]
    assert nowcast_main(_args(nowcast_root, tmp_path / "r", *argv)) == 2
    assert not (tmp_path / "r").exists() and "no test row" in capsys.readouterr().err


def test_a_late_input_makes_the_nowcast_script_exit_3_with_the_year_and_the_row(
    tmp_path, nowcast_root, monkeypatch, capsys
):
    real = benchmark.nowcast_windows

    def with_a_late_input(*args, **kwargs):
        windows = real(*args, **kwargs)
        test = windows[-1].test
        test.loc[0, "avail_era5_utc"] = test.loc[0, "decision_utc"] + pd.Timedelta(days=1)
        return windows

    monkeypatch.setattr(benchmark, "nowcast_windows", with_a_late_input)
    argv = ["--models", "bridge", *NOWCAST_YEARS]
    rc = nowcast_main(_args(nowcast_root, tmp_path / "r", *argv))
    err = capsys.readouterr().err
    assert rc == 3 and not (tmp_path / "r").exists()
    assert "2011" in err and "row 0" in err and "avail_era5_utc" in err


def test_the_default_nowcast_run_directory_is_under_runs_in_the_home_directory(
    tmp_path, nowcast_root, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    argv = ["--root", str(nowcast_root), "--models", "bridge", "--first-test-year", "2011"]
    assert nowcast_main([*argv, "--last-test-year", "2011"]) == 0
    (made,) = (home / "runs").iterdir()
    assert re.fullmatch(r"gtm_nowcast_\d{8}T\d{6}Z", made.name)
    assert (made / "metrics.json").exists()


def test_the_nowcast_root_defaults_to_the_climate_data_root_variable(tmp_path, nowcast_root):
    argv = ["--models", "bridge", "--first-test-year", "2011", "--last-test-year", "2011"]
    assert nowcast_main(["--run-dir", str(tmp_path / "r"), *argv]) == 0  # the fixture set it
    assert Path(read_run(tmp_path / "r")["config"]["root"]) == nowcast_root.resolve()


# the compare script


def _run_with(run_dir, nn, emos):
    summary = [
        {"model": model, "lead_month": lead, "crps": crps}
        for model, crps in (("nn", nn), ("emos", emos))
        for lead in range(1, 7)
    ]
    metrics = {"summary": summary, "win": [nn <= emos, ""]}
    write_run(
        run_dir, config={"protocol": "strict"}, metrics=metrics, rows=pd.DataFrame(), report_md=""
    )


def test_the_compare_script_prints_the_table_of_the_runs(tmp_path, capsys):
    _run_with(tmp_path / "a", 0.04, 0.05)
    _run_with(tmp_path / "b", 0.06, 0.05)
    runs = [tmp_path / "a", tmp_path / "b"]
    assert compare_main([str(run) for run in runs]) == 0
    assert capsys.readouterr().out == compare_runs(runs)


def test_the_compare_script_exits_2_for_a_directory_that_is_not_a_run_and_for_no_directory(
    tmp_path, capsys
):
    assert compare_main([str(tmp_path / "nothing")]) == 2
    assert "nothing" in capsys.readouterr().err
    assert compare_main([]) == 2


def _nowcast_run_with(run_dir, nn, bridge):
    summary = [
        {"model": model, "checkpoint": checkpoint, "crps": crps}
        for model, crps in (("nn", nn), ("bridge", bridge))
        for checkpoint in ("10", "25", "+3")
    ]
    metrics = {"summary": summary, "win": [nn < bridge, ""]}
    config = {"kind": "nowcast", "protocol": "strict"}
    write_run(run_dir, config=config, metrics=metrics, rows=pd.DataFrame(), report_md="")


def test_the_compare_script_prints_the_nowcast_table_and_refuses_to_mix_the_kinds(tmp_path, capsys):
    _nowcast_run_with(tmp_path / "a", 0.04, 0.05)
    _nowcast_run_with(tmp_path / "b", 0.06, 0.05)
    _run_with(tmp_path / "seasonal", 0.04, 0.05)
    runs = [tmp_path / "a", tmp_path / "b"]
    assert compare_main([str(run) for run in runs]) == 0
    printed = capsys.readouterr().out
    assert printed == compare_runs(runs) and printed.startswith("| run | cp10_nn | cp10_bridge |")
    assert compare_main([str(tmp_path / "a"), str(tmp_path / "seasonal")]) == 2
    assert "different kinds" in capsys.readouterr().err


# process level


BLOCK_TORCH = """
import sys
sys.path.insert(0, sys.argv[1])


class BlockTorch:
    def find_spec(self, name, path=None, target=None):
        if name == "torch" or name.startswith("torch."):
            raise ModuleNotFoundError("torch is blocked", name=name)


sys.meta_path.insert(0, BlockTorch())
import climate.nn.benchmark
import climate.nn.nowcast_model
import climate.nn.nowcast_windows
import climate.nn.report
import climate.nn.scoring
import scripts.research.gtm_nn_benchmark
import scripts.research.gtm_nn_compare
import scripts.research.gtm_nowcast_benchmark
import scripts.research.nn_cli
"""


def test_the_modules_and_the_scripts_import_without_torch():
    done = subprocess.run(
        [sys.executable, "-c", BLOCK_TORCH, str(REPO_ROOT)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "script", ["gtm_nn_benchmark.py", "gtm_nn_compare.py", "gtm_nowcast_benchmark.py"]
)
def test_the_scripts_run_as_programs(script):
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--help"], capture_output=True, text=True
    )
    assert done.returncode == 0 and "usage" in done.stdout.lower(), done.stderr
