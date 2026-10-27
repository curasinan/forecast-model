"""What the GTM research scripts share: options, torch and device rules, run directory, models."""

from __future__ import annotations

import argparse
import importlib.util
import json
import pickle
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from analysis.gpu_budget import GpuBudgetError
from climate.bridge import Bridge
from climate.nn.nowcast_windows import MONTH
from climate.nn.report import git_commit, write_provenance, write_run
from climate.nn.seasonal_model import SEEDS
from climate.nn.torch_model import configure_device
from climate.nn.windows import FIRST_TEST_YEAR

TORCH_MISSING = (
    'research extra not installed: uv pip install -e ".[climate,research]" into ~/work/wx-dl/.venv'
)
# what a model directory that is incomplete or damaged can raise while it loads
LOAD_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    RuntimeError,
    pickle.UnpicklingError,
)
ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"


class UsageError(Exception):
    """The script was called wrongly or pointed at something unusable: exit status 2."""


def torch_available() -> bool:
    """Whether torch can be imported here, asked without importing it."""
    try:
        return importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        return False


def positive(kind: type) -> Callable[[str], int | float]:
    """An argparse type: a number of that kind above zero."""

    def parse(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"a number, got {text!r}") from None
        if not value > 0:
            raise argparse.ArgumentTypeError(f"a positive number, got {text!r}")
        return value

    return parse


def parse_seeds(text: str) -> tuple[int, ...]:
    """An argparse type: integers separated by commas, each seed once."""
    try:
        seeds = tuple(int(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"integers separated by commas, got {text!r}") from None
    if len(set(seeds)) != len(seeds):
        raise argparse.ArgumentTypeError(f"each seed once, got {text!r}")
    return seeds


def models_parser(check: Callable[[tuple[str, ...]], tuple[str, ...]]) -> Callable[[str], tuple]:
    """An argparse type: comma-separated model names that the check accepts."""

    def parse(text: str) -> tuple[str, ...]:
        try:
            return check(tuple(part.strip() for part in text.split(",") if part.strip()))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return parse


def add_common_options(
    parser: argparse.ArgumentParser,
    *,
    models_default: str,
    models_type: Callable[[str], tuple],
    models_help: str,
    run_dir_help: str,
) -> None:
    """The options both benchmarks have."""
    parser.add_argument(
        "--root", default=None, help="climate data root (default: CLIMATE_DATA_ROOT)"
    )
    parser.add_argument("--models", type=models_type, default=models_default, help=models_help)
    parser.add_argument(
        "--first-test-year", type=int, default=FIRST_TEST_YEAR, help="default: %(default)s"
    )
    parser.add_argument(
        "--last-test-year", type=int, default=None, help="default: the current year (UTC)"
    )
    parser.add_argument(
        "--seeds",
        type=parse_seeds,
        default=",".join(str(seed) for seed in SEEDS),
        help="comma-separated (default: %(default)s)",
    )
    parser.add_argument(
        "--hidden", type=positive(int), default=32, help="hidden width (default: %(default)s)"
    )
    parser.add_argument(
        "--gpu-budget",
        type=positive(float),
        default=None,
        help="GiB for configure_device; without it the network runs on the CPU",
    )
    parser.add_argument("--run-dir", default=None, help=run_dir_help)


def year_range(args: argparse.Namespace) -> tuple[int, int]:
    """(first, last) test year; UsageError when the first is after the last."""
    last = args.last_test_year
    if last is None:
        last = datetime.now(timezone.utc).year
    if args.first_test_year > last:
        raise UsageError(
            f"--first-test-year {args.first_test_year} is after --last-test-year {last}"
        )
    return args.first_test_year, last


def require_torch(models: Sequence[str]) -> None:
    """UsageError with TORCH_MISSING when the network is asked for and torch is not there."""
    if "nn" in models and not torch_available():
        raise UsageError(TORCH_MISSING)


def new_run_dir(given: str | None, name: str) -> Path:
    """The run directory, the given path or ~/runs/<name>_<UTC stamp>; UsageError if it exists."""
    if given is not None:
        run_dir = Path(given).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = Path.home() / "runs" / f"{name}_{stamp}"
    if run_dir.exists():
        raise UsageError(f"the run directory exists already: {run_dir}")
    return run_dir


def device_for(models: Sequence[str], gpu_budget: float | None) -> str:
    """cpu, or configure_device's answer when a budget is given and the network is in the run."""
    if gpu_budget is None or "nn" not in models:
        return "cpu"
    try:
        return configure_device(gpu_budget)
    except GpuBudgetError as exc:
        raise UsageError(str(exc)) from exc


def base_config(
    args: argparse.Namespace,
    *,
    kind: str,
    protocol: str,
    models: Sequence[str],
    root: Path,
    run_dir: Path,
    last_year: int,
    device: str,
    **more,
) -> dict:
    """The config.json both benchmarks write; more adds the run's own keys at the end."""
    config = {
        "root": str(root),
        "kind": kind,
        "protocol": protocol,
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
    return {**config, **more}


def write_and_report(
    run_dir: Path,
    *,
    config: dict,
    metrics: dict,
    rows,
    report_md: str,
    model_dir: Path,
    saved_model: dict | None = None,
) -> None:
    """Write the run directory, with the saved model and its provenance if any, and print it."""
    if saved_model is not None and model_dir.exists():
        write_provenance(model_dir, saved_model)
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


def parse_as_of(text: str) -> datetime:
    """An argparse type: an ISO 8601 time that carries an offset or Z."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = None
    if moment is None or moment.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            f"a time with an offset or Z, like 2026-10-27T12:00:00Z, got {text!r}"
        )
    return moment


def resolve_as_of(given: datetime | None) -> datetime:
    """The given time, else the clock read once; in UTC and whole seconds."""
    moment = datetime.now(timezone.utc) if given is None else given
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def month_text(text: str) -> str:
    """An argparse type: a month as YYYY-MM."""
    if MONTH.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(f"a month as YYYY-MM, got {text!r}")
    return text


def read_provenance(model_dir: Path, kind: str) -> dict:
    """The provenance.json of a saved model of that kind; UsageError if it is not one."""
    if not model_dir.is_dir():
        raise UsageError(f"{model_dir} is not a directory")
    path = model_dir / "provenance.json"
    try:
        provenance = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError(
            f"{model_dir} has no provenance.json: point it at the weights/ directory "
            f"of a {kind} benchmark run"
        ) from None
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read {path}: {exc}") from exc
    found = provenance.get("kind") if isinstance(provenance, dict) else None
    if found != kind:
        raise UsageError(f"{path} is of a {found!r} model, this script needs a {kind!r} one")
    return provenance


def bridge_of(provenance: dict, model_dir: Path) -> Bridge:
    """The bridge that the saved network was built with, from its provenance; never refitted."""
    path = model_dir / "provenance.json"
    try:
        bridge = Bridge.from_json(json.dumps(provenance["bridge"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise UsageError(f"{path} has no usable bridge: {exc!r}") from exc
    if not bridge.is_fitted:
        raise UsageError(f"the bridge in {path} is not fitted")
    return bridge


def training_note(provenance: dict) -> str:
    """What a saved network saw, for the context line: its labels, from the provenance."""
    return f"trained on labels through {provenance.get('labels_through', 'an unknown month')}"


def load_model(model_class, model_dir: Path):
    """The saved model of that class in model_dir; UsageError if its files do not load."""
    try:
        return model_class.load(model_dir)
    except LOAD_ERRORS as exc:
        raise UsageError(f"cannot load the model in {model_dir}: {exc}") from exc
