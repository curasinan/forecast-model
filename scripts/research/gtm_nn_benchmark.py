"""Benchmark the seasonal GTM network against EMOS and the raw ensemble; write a run directory."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.gpu_budget import GpuBudgetError  # noqa: E402
from climate.dataset import load_era5, load_noaa_gtm, load_seas5  # noqa: E402
from climate.nn.benchmark import (  # noqa: E402
    FROZEN_MODELS,
    PROTOCOLS,
    SeasonalRun,
    check_models,
    run_seasonal,
    seasonal_win,
)
from climate.nn.leakage import LeakageError  # noqa: E402
from climate.nn.report import git_commit, render_seasonal_report, write_run  # noqa: E402
from climate.nn.seasonal_model import SEEDS  # noqa: E402
from climate.nn.torch_model import configure_device  # noqa: E402
from climate.nn.windows import FIRST_TEST_YEAR  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.storage import ledger_path, load_ledger  # noqa: E402

DEFAULT_MODELS = "nn,emos,raw,trend"
TORCH_MISSING = (
    'research extra not installed: uv pip install -e ".[climate,research]" into ~/work/wx-dl/.venv'
)


def _torch_available() -> bool:
    """Whether torch can be imported here, asked without importing it."""
    try:
        return importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        return False


def _models(text: str) -> tuple[str, ...]:
    try:
        return check_models(tuple(part.strip() for part in text.split(",") if part.strip()))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _seeds(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"integers separated by commas, got {text!r}") from None


def _positive(kind):
    def parse(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"a number, got {text!r}") from None
        if not value > 0:
            raise argparse.ArgumentTypeError(f"a positive number, got {text!r}")
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark the seasonal GTM network against EMOS and the raw ensemble "
        "per lead and write a run directory."
    )
    parser.add_argument(
        "--root", default=None, help="climate data root (default: CLIMATE_DATA_ROOT)"
    )
    parser.add_argument(
        "--protocol",
        choices=PROTOCOLS,
        default="strict",
        help="strict is the headline; frozen reproduces the model report and runs only "
        "emos, trend and clim, whatever --models says",
    )
    parser.add_argument(
        "--models",
        type=_models,
        default=DEFAULT_MODELS,
        help="comma-separated from nn, emos, raw, trend (default: %(default)s)",
    )
    parser.add_argument(
        "--first-test-year", type=int, default=FIRST_TEST_YEAR, help="default: %(default)s"
    )
    parser.add_argument(
        "--last-test-year", type=int, default=None, help="default: the current year (UTC)"
    )
    parser.add_argument(
        "--seeds",
        type=_seeds,
        default=",".join(str(seed) for seed in SEEDS),
        help="comma-separated (default: %(default)s)",
    )
    parser.add_argument(
        "--hidden", type=_positive(int), default=32, help="hidden width (default: %(default)s)"
    )
    parser.add_argument(
        "--gpu-budget",
        type=_positive(float),
        default=None,
        help="GiB for configure_device; without it the network runs on the CPU",
    )
    parser.add_argument(
        "--run-dir", default=None, help="default: ~/runs/gtm_nn_<protocol>_<UTC stamp>"
    )
    return parser


def _run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir is not None:
        return Path(args.run_dir).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / "runs" / f"gtm_nn_{args.protocol}_{stamp}"


def _read_inputs(root: Path):
    """The three research inputs and the labels ledger under root."""
    inputs = (load_seas5(root), load_era5(root), load_noaa_gtm(root))
    if inputs[2].empty:
        raise FileNotFoundError(f"no gtm labels in {ledger_path(root)}")
    return inputs, load_ledger(root)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits with 2 on a usage error and 0 after --help
        return int(exc.code or 0)
    last_year = args.last_test_year
    if last_year is None:
        last_year = datetime.now(timezone.utc).year
    if args.first_test_year > last_year:
        print(
            f"--first-test-year {args.first_test_year} is after --last-test-year {last_year}",
            file=sys.stderr,
        )
        return 2
    models = FROZEN_MODELS if args.protocol == "frozen" else args.models
    if "nn" in models and not _torch_available():
        print(TORCH_MISSING, file=sys.stderr)
        return 2
    run_dir = _run_dir(args)
    if run_dir.exists():
        print(f"the run directory exists already: {run_dir}", file=sys.stderr)
        return 2
    device = "cpu"
    if "nn" in models and args.gpu_budget is not None:
        try:
            device = configure_device(args.gpu_budget)
        except GpuBudgetError as exc:
            print(exc, file=sys.stderr)
            return 2
    root = root_path(args.root)
    try:
        inputs, ledger = _read_inputs(root)
    except OSError as exc:
        print(f"cannot read the research inputs under {root}: {exc}", file=sys.stderr)
        return 2
    cfg = SeasonalRun(
        args.protocol, models, args.first_test_year, last_year, args.seeds, args.hidden, device
    )
    with tempfile.TemporaryDirectory() as scratch:
        model_dir = Path(scratch) / "model"
        try:
            rows, summary, extras = run_seasonal(inputs, ledger, cfg, model_dir=model_dir)
        except LeakageError as exc:
            print(f"leakage: {exc}", file=sys.stderr)
            return 3
        if not (summary["n"] > 0).any():
            print("no test row could be scored: check --root and the test years", file=sys.stderr)
            return 2
        win = seasonal_win(summary) if args.protocol == "strict" and "nn" in models else None
        report_md = render_seasonal_report(summary, extras, win)
        config = {
            "root": str(root),
            "protocol": args.protocol,
            "models": list(models),
            "first_test_year": args.first_test_year,
            "last_test_year": last_year,
            "seeds": list(args.seeds),
            "hidden": args.hidden,
            "gpu_budget": args.gpu_budget,
            "device": device,
            "run_dir": str(run_dir),
            "git_commit": git_commit(),
        }
        metrics = {
            "summary": summary.to_dict(orient="records"),
            "extras": extras,
            "win": None if win is None else list(win),
        }
        write_run(
            run_dir,
            config=config,
            metrics=metrics,
            rows=rows,
            report_md=report_md,
            model_dir=model_dir if model_dir.exists() else None,
        )
    print(report_md)
    print(f"run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
