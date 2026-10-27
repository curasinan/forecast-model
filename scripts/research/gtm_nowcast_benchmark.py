"""Benchmark the GTM nowcast network against the bridge and persistence; write a run directory."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.dataset import load_noaa_gtm, load_seas5  # noqa: E402
from climate.nn.benchmark import (  # noqa: E402
    NOWCAST_MODELS,
    NowcastRun,
    check_models,
    nowcast_win,
    run_nowcast,
)
from climate.nn.leakage import LeakageError  # noqa: E402
from climate.nn.nowcast import load_daily  # noqa: E402
from climate.nn.nowcast_windows import MONTH, WindowError  # noqa: E402
from climate.nn.report import render_nowcast_report  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.storage import ledger_path, load_ledger  # noqa: E402
from scripts.research import nn_cli  # noqa: E402

DEFAULT_MODELS = "nn,bridge,persistence"


def _month(text: str) -> str:
    if MONTH.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(f"a month as YYYY-MM, got {text!r}")
    return text


def _models(models: tuple[str, ...]) -> tuple[str, ...]:
    return check_models(models, NOWCAST_MODELS)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark the GTM nowcast network against the bridge baseline and "
        "persistence at nine checkpoints per month and write a run directory."
    )
    nn_cli.add_common_options(
        parser,
        models_default=DEFAULT_MODELS,
        models_type=nn_cli.models_parser(_models),
        models_help="comma-separated from nn, bridge, persistence (default: %(default)s)",
        run_dir_help="default: ~/runs/gtm_nowcast_<UTC stamp>",
    )
    parser.add_argument(
        "--bridge-through",
        type=_month,
        default=None,
        help="fit one bridge through this month (YYYY-MM) for every window: a frozen variant "
        "that is never the headline and records no verdict",
    )
    return parser


def _read_inputs(root: Path):
    """The daily ERA5 series, the labels, the SEAS5 members and the labels ledger under root."""
    try:
        daily_K = load_daily(root)
        seas5 = load_seas5(root)
        noaa_C = load_noaa_gtm(root)
        if noaa_C.empty:
            raise FileNotFoundError(f"no gtm labels in {ledger_path(root)}")
        return daily_K, noaa_C, seas5, load_ledger(root)
    except OSError as exc:
        raise nn_cli.UsageError(f"cannot read the research inputs under {root}: {exc}") from exc


def _benchmark(args: argparse.Namespace) -> int:
    _, last_year = nn_cli.year_range(args)
    models = args.models
    nn_cli.require_torch(models)
    run_dir = nn_cli.new_run_dir(args.run_dir, "gtm_nowcast")
    device = nn_cli.device_for(models, args.gpu_budget)
    root = root_path(args.root)
    daily_K, noaa_C, seas5, ledger = _read_inputs(root)
    cfg = NowcastRun(models, args.first_test_year, last_year, args.seeds, args.hidden, device)
    with tempfile.TemporaryDirectory() as scratch:
        model_dir = Path(scratch) / "model"
        try:
            rows, summary, extras = run_nowcast(
                daily_K,
                noaa_C,
                seas5,
                ledger,
                cfg,
                bridge_through=args.bridge_through,
                model_dir=model_dir,
            )
        except WindowError as exc:
            raise nn_cli.UsageError(str(exc)) from exc
        if not (summary["n"] > 0).any():
            raise nn_cli.UsageError("no test row could be scored: check --root and the test years")
        judged = args.bridge_through is None and {"nn", "bridge"} <= set(models)
        win = nowcast_win(summary, extras["first_print_summary"]) if judged else None
        report_md = render_nowcast_report(summary, extras, win)
        config = nn_cli.base_config(
            args,
            kind="nowcast",
            protocol=extras["protocol"],
            models=models,
            root=root,
            run_dir=run_dir,
            last_year=last_year,
            device=device,
            bridge_through=args.bridge_through,
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
