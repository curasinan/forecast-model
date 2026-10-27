"""Price the running month: the nowcast network's Gaussian at an exact time, as a JSON payload."""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.bridge import Bridge  # noqa: E402
from climate.dataset import load_noaa_gtm, load_seas5  # noqa: E402
from climate.nn.availability import last_era5_day_for  # noqa: E402
from climate.nn.features import seasonal_frame  # noqa: E402
from climate.nn.nowcast import load_daily, nowcast_payload, nowcast_row  # noqa: E402
from climate.nn.nowcast_model import GtmNowcastModel  # noqa: E402
from climate.nn.nowcast_windows import MONTH, monthly_means  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.storage import ledger_path, load_ledger  # noqa: E402
from scripts.research import nn_cli  # noqa: E402

# what a model directory that is incomplete or damaged can raise while it loads
LOAD_ERRORS = (OSError, ValueError, KeyError, RuntimeError, pickle.UnpicklingError)
ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"


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
        raise nn_cli.UsageError(f"{model_dir} is not a directory")
    path = model_dir / "provenance.json"
    try:
        provenance = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise nn_cli.UsageError(
            f"{model_dir} has no provenance.json: point it at the weights/ directory "
            f"of a {kind} benchmark run"
        ) from None
    except (OSError, ValueError) as exc:
        raise nn_cli.UsageError(f"cannot read {path}: {exc}") from exc
    found = provenance.get("kind") if isinstance(provenance, dict) else None
    if found != kind:
        raise nn_cli.UsageError(f"{path} is of a {found!r} model, this script needs a {kind!r} one")
    return provenance


def bridge_of(provenance: dict, model_dir: Path) -> Bridge:
    """The bridge that the saved network was built with, from its provenance; never refitted."""
    path = model_dir / "provenance.json"
    try:
        bridge = Bridge.from_json(json.dumps(provenance["bridge"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise nn_cli.UsageError(f"{path} has no usable bridge: {exc!r}") from exc
    if not bridge.is_fitted:
        raise nn_cli.UsageError(f"the bridge in {path} is not fitted")
    return bridge


def training_note(provenance: dict) -> str:
    """What a saved network saw, for the context line: its labels, from the provenance."""
    return f"trained on labels through {provenance.get('labels_through', 'an unknown month')}"


def load_model(model_class, model_dir: Path):
    """The saved model of that class in model_dir; UsageError if its files do not load."""
    try:
        return model_class.load(model_dir)
    except LOAD_ERRORS as exc:
        raise nn_cli.UsageError(f"cannot load the model in {model_dir}: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Predict the running month of the GTM contracts with a saved nowcast "
        "network at an exact time and print a JSON payload; exit 0 when it is usable, 3 when "
        "it is not (a day is missing, or nothing is public yet)."
    )
    parser.add_argument(
        "--root", default=None, help="climate data root (default: CLIMATE_DATA_ROOT)"
    )
    parser.add_argument(
        "--model-dir",
        required=True,
        help="the weights/ directory of a nowcast benchmark run (manifest and provenance)",
    )
    parser.add_argument(
        "--as-of",
        type=parse_as_of,
        default=None,
        help="the decision time as 2026-10-27T12:00:00Z, with an offset or Z (default: now)",
    )
    parser.add_argument(
        "--target-month",
        type=month_text,
        default=None,
        help="the month to predict as YYYY-MM, one that has started by --as-of "
        "(default: the month of --as-of)",
    )
    parser.add_argument("--out", default=None, help="also write the payload to this JSON file")
    return parser


def _read_inputs(root: Path):
    """The daily ERA5 series, the labels, the SEAS5 members and the labels ledger under root."""
    try:
        daily_K = load_daily(root)
        seas5 = load_seas5(root)
        noaa_C = load_noaa_gtm(root)
        ledger = load_ledger(root)
    except (OSError, ValueError, KeyError) as exc:
        raise nn_cli.UsageError(f"cannot read the research inputs under {root}: {exc}") from exc
    if noaa_C.empty:
        raise nn_cli.UsageError(f"no gtm labels in {ledger_path(root)}")
    if daily_K.empty:
        raise nn_cli.UsageError(f"the daily ERA5 series under {root} is empty")
    return daily_K, noaa_C, seas5, ledger


def _require_days(daily_K: pd.Series, month: str, as_of: datetime) -> None:
    """UsageError for a month not under way, or one with published days that the series lacks."""
    period = pd.Period(month, freq="M")
    first, end = period.start_time.date(), period.end_time.date()
    if as_of.date() < first:
        raise nn_cli.UsageError(
            f"target month {month} has not started at {as_of.strftime(ISO_UTC)}: "
            "a nowcast needs a month that is under way"
        )
    if last_era5_day_for(as_of) < first:
        return  # nothing is public yet: the payload says so
    if ((daily_K.index >= pd.Timestamp(first)) & (daily_K.index <= pd.Timestamp(end))).any():
        return
    low, high = daily_K.index.min().date(), daily_K.index.max().date()
    if first > high:
        raise nn_cli.UsageError(
            f"target month {month} is after the daily series, which ends {high}: "
            "refresh the recent months with gtm_nn_fetch.py --months"
        )
    if end < low:
        raise nn_cli.UsageError(
            f"target month {month} is before the daily series, which starts {low}"
        )
    raise nn_cli.UsageError(f"the daily series has no day of target month {month}")


def _reason(payload: dict) -> str:
    """One line on why the payload cannot be used."""
    if payload["gap_days"]:
        return (
            f"not usable: {payload['gap_days']} public day(s) of {payload['target_month']} "
            "are missing from the daily series"
        )
    if payload["days_used"] == 0:
        return f"not usable: no day of {payload['target_month']} is public yet"
    return "not usable: an input of the network is missing (see seas_init and prev_label_month)"


def _write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        raise nn_cli.UsageError(f"cannot write {path}: {exc}") from exc


def _nowcast(args: argparse.Namespace) -> int:
    nn_cli.require_torch(("nn",))
    as_of = resolve_as_of(args.as_of)
    month = args.target_month or f"{as_of.year:04d}-{as_of.month:02d}"
    model_dir = Path(args.model_dir).expanduser()
    provenance = read_provenance(model_dir, "nowcast")
    bridge = bridge_of(provenance, model_dir)
    model = load_model(GtmNowcastModel, model_dir)
    root = root_path(args.root)
    daily_K, noaa_C, seas5, ledger = _read_inputs(root)
    try:
        _require_days(daily_K, month, as_of)
        seasonal = seasonal_frame(seas5, monthly_means(daily_K), noaa_C, bridge=bridge)
        row = nowcast_row(
            daily_K,
            noaa_C,
            seasonal,
            bridge=bridge,
            ledger=ledger,
            target_month=month,
            decision=as_of,
        )
    except ValueError as exc:
        raise nn_cli.UsageError(f"cannot build the nowcast inputs from {root}: {exc}") from exc
    prediction = model.predict(pd.DataFrame([row])).iloc[0]
    fingerprint = model.fingerprint()
    payload = nowcast_payload(
        row, prediction["mu"], prediction["sigma"], model_fingerprint=fingerprint
    )
    text = json.dumps(payload, indent=2, allow_nan=False)
    if args.out is not None:
        _write(Path(args.out).expanduser(), text)
    print(text)
    print(f"nowcast model {fingerprint} {training_note(provenance)}", file=sys.stderr)
    if not payload["usable"]:
        print(_reason(payload), file=sys.stderr)
    return 0 if payload["usable"] else 3


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits with 2 on a usage error and 0 after --help
        return int(exc.code or 0)
    try:
        return _nowcast(args)
    except nn_cli.UsageError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
