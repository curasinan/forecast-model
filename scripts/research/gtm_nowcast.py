"""Price the running month: the nowcast network's Gaussian at an exact time, as a JSON payload."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.dataset import load_seas5  # noqa: E402
from climate.nn.availability import last_era5_day_for, seas5_available_on  # noqa: E402
from climate.nn.features import seasonal_frame  # noqa: E402
from climate.nn.nowcast import (  # noqa: E402
    CHECKPOINTS,
    expected_seas_init,
    load_daily,
    missing_label,
    nowcast_payload,
    nowcast_row,
)
from climate.nn.nowcast_model import GtmNowcastModel  # noqa: E402
from climate.nn.nowcast_windows import monthly_means  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.reduce import SEAS5_PARQUET  # noqa: E402
from climate.storage import ledger_path, load_ledger  # noqa: E402
from scripts.research import nn_cli  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Predict the running month of the GTM contracts with a saved nowcast "
        "network at an exact time and print a JSON payload; exit 0 when it is usable, 3 when "
        "it is not (a day or an input is missing, or checkpoint 10 is not reached)."
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
        type=nn_cli.parse_as_of,
        default=None,
        help="the decision time as 2026-10-27T12:00:00Z, with an offset or Z (default: now)",
    )
    parser.add_argument(
        "--target-month",
        type=nn_cli.month_text,
        default=None,
        help="the month to predict as YYYY-MM, one that has started by --as-of "
        "(default: the month of --as-of)",
    )
    parser.add_argument("--out", default=None, help="also write the payload to this JSON file")
    return parser


def _require_days(daily_K: pd.Series, month: str, as_of: datetime) -> None:
    """UsageError for a month not under way, or one with published days that the series lacks."""
    period = pd.Period(month, freq="M")
    first, end = period.start_time.date(), period.end_time.date()
    if as_of.date() < first:
        raise nn_cli.UsageError(
            f"target month {month} has not started at {as_of.strftime(nn_cli.ISO_UTC)}: "
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


def _reason(
    payload: dict, *, as_of: datetime, root: Path, noaa_C: pd.Series, ledger: pd.DataFrame
) -> str:
    """One line on why the payload cannot be used; a stale input names the file to copy."""
    month = payload["target_month"]
    if payload["gap_days"]:
        return (
            f"not usable: {payload['gap_days']} public day(s) of {month} "
            "are missing from the daily series"
        )
    if payload["days_used"] == 0:
        return f"not usable: no day of {month} is public yet"
    if payload["checkpoint"] == CHECKPOINTS[0]:
        return (
            f"not usable: checkpoint 10 not reached, {payload['days_used']} day(s) used; "
            "the network trained on 5 or more"
        )
    expected = expected_seas_init(month, as_of)
    if payload["seas_init"] != expected:
        since = seas5_available_on(expected).strftime(nn_cli.ISO_UTC)
        return (
            f"not usable: seas_init {payload['seas_init'] or '(none)'} is stale, init {expected} "
            f"has been public since {since}: copy the current {'/'.join(SEAS5_PARQUET)} "
            f"from the mirror into {root}"
        )
    missing = missing_label(noaa_C, ledger, target_month=month, decision=as_of)
    if missing is not None:
        period, printed = missing
        labels = ledger_path(root).relative_to(root).as_posix()
        return (
            f"not usable: the labels ledger under {root} lacks {period}, printed since "
            f"{printed.strftime(nn_cli.ISO_UTC)}: copy the current {labels} from the mirror"
        )
    return "not usable: an input of the network is missing (see seas_init and prev_label_month)"


def _write(path: Path, text: str) -> None:
    """Write text through a temporary file of its own beside path; the old one stays whole."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
            newline="\n",
        ) as handle:
            tmp = Path(handle.name)
            handle.write(text + "\n")
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)  # the mode a plain write gets, not the temporary file's 0600
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        raise nn_cli.UsageError(f"cannot write {path}: {exc}") from exc


def _nowcast(args: argparse.Namespace) -> int:
    nn_cli.require_torch(("nn",))
    as_of = nn_cli.resolve_as_of(args.as_of)
    month = args.target_month or f"{as_of.year:04d}-{as_of.month:02d}"
    model_dir = Path(args.model_dir).expanduser()
    provenance = nn_cli.read_provenance(model_dir, "nowcast")
    bridge = nn_cli.bridge_of(provenance, model_dir)
    model = nn_cli.load_model(GtmNowcastModel, model_dir)
    root = root_path(args.root)
    daily_K, seas5, ledger, noaa_C = nn_cli.read_inputs(root, load_daily, load_seas5, load_ledger)
    if daily_K.empty:
        raise nn_cli.UsageError(f"the daily ERA5 series under {root} is empty")
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
    except (ValueError, KeyError) as exc:
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
    print(f"nowcast model {fingerprint} {nn_cli.training_note(provenance)}", file=sys.stderr)
    if not payload["usable"]:
        reason = _reason(payload, as_of=as_of, root=root, noaa_C=noaa_C, ledger=ledger)
        print(reason, file=sys.stderr)
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
