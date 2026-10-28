"""Benchmark the seasonal GTM network against EMOS and the raw ensemble; write a run directory."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
from climate.nn.report import render_seasonal_report  # noqa: E402
from climate.nn.windows import WindowError  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.storage import ledger_path, load_ledger  # noqa: E402
from scripts.research import nn_cli  # noqa: E402

DEFAULT_MODELS = "nn,emos,raw,trend"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark the seasonal GTM network against EMOS and the raw ensemble "
        "per lead and write a run directory."
    )
    parser.add_argument(
        "--protocol",
        choices=PROTOCOLS,
        default="strict",
        help="strict is the headline; frozen reproduces the model report and runs only "
        "emos, trend and clim, whatever --models says",
    )
    nn_cli.add_common_options(
        parser,
        models_default=DEFAULT_MODELS,
        models_type=nn_cli.models_parser(check_models),
        models_help="comma-separated from nn, emos, raw, trend (default: %(default)s)",
        run_dir_help="default: ~/runs/gtm_nn_<protocol>_<UTC stamp>",
    )
    return parser


def _read_inputs(root: Path):
    """The three research inputs and the labels ledger under root."""
    try:
        inputs = (load_seas5(root), load_era5(root), load_noaa_gtm(root))
        if inputs[2].empty:
            raise FileNotFoundError(f"no gtm labels in {ledger_path(root)}")
        return inputs, load_ledger(root)
    except (OSError, ValueError, KeyError) as exc:
        raise nn_cli.UsageError(f"cannot read the research inputs under {root}: {exc}") from exc


def _benchmark(args: argparse.Namespace) -> int:
    _, last_year = nn_cli.year_range(args)
    models = FROZEN_MODELS if args.protocol == "frozen" else args.models
    nn_cli.require_torch(models)
    run_dir = nn_cli.new_run_dir(args.run_dir, f"gtm_nn_{args.protocol}")
    device = nn_cli.device_for(models, args.gpu_budget)
    root = root_path(args.root)
    inputs, ledger = _read_inputs(root)
    cfg = SeasonalRun(
        args.protocol, models, args.first_test_year, last_year, args.seeds, args.hidden, device
    )
    with tempfile.TemporaryDirectory() as scratch:
        model_dir = Path(scratch) / "model"
        try:
            rows, summary, extras = run_seasonal(inputs, ledger, cfg, model_dir=model_dir)
        except WindowError as exc:
            raise nn_cli.UsageError(str(exc)) from exc
        if not (summary["n"] > 0).any():
            raise nn_cli.UsageError("no test row could be scored: check --root and the test years")
        judged = args.protocol == "strict" and {"nn", "emos"} <= set(models)
        win = seasonal_win(summary) if judged else None
        report_md = render_seasonal_report(summary, extras, win, ledger=ledger)
        config = nn_cli.base_config(
            args,
            kind="seasonal",
            protocol=args.protocol,
            models=models,
            root=root,
            run_dir=run_dir,
            last_year=last_year,
            device=device,
        )
        metrics = {
            "summary": summary.to_dict(orient="records"),
            "extras": extras,
            "win": None if win is None else list(win),
        }
        nn_cli.write_and_report(
            run_dir,
            config=config,
            metrics=metrics,
            rows=rows,
            report_md=report_md,
            model_dir=model_dir,
            saved_model=extras["saved_model"],
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits with 2 on a usage error and 0 after --help
        return int(exc.code or 0)
    try:
        return _benchmark(args)
    except nn_cli.UsageError as exc:
        print(exc, file=sys.stderr)
        return 2
    except LeakageError as exc:
        print(f"leakage: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
