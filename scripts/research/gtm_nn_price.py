"""Price a strike ladder with the operational EMOS and the seasonal network side by side."""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.dataset import (  # noqa: E402
    build_frame_from_inputs,
    load_era5,
    load_noaa_gtm,
    load_seas5,
)
from climate.emos import EmosModel  # noqa: E402
from climate.ladder import p_yes  # noqa: E402
from climate.nn.features import seasonal_frame  # noqa: E402
from climate.nn.seasonal_model import GtmNnModel  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.storage import ledger_path  # noqa: E402
from scripts.research import nn_cli  # noqa: E402
from scripts.research.gtm_nowcast import (  # noqa: E402
    ISO_UTC,
    bridge_of,
    load_model,
    month_text,
    parse_as_of,
    read_provenance,
    resolve_as_of,
    training_note,
)

TABLE_HEADER = "strike | p_yes_emos | p_yes_nn"
TABLE_SEPARATOR = "|---|---|---|"


def _strike(text: str) -> float:
    """An argparse type: a finite number, a strike in degrees C of anomaly."""
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"a finite number, got {text!r}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Price strikes for one GTM target month with the fitted EMOS and a saved "
        "seasonal network side by side, each from the freshest SEAS5 init public at --as-of."
    )
    parser.add_argument("--emos", required=True, help="the fitted EMOS model, gtm_emos.json")
    parser.add_argument(
        "--nn-dir",
        required=True,
        help="the weights/ directory of a seasonal benchmark run (manifest and provenance)",
    )
    parser.add_argument(
        "--root", default=None, help="climate data root (default: CLIMATE_DATA_ROOT)"
    )
    parser.add_argument("--target-month", required=True, type=month_text, help="YYYY-MM")
    parser.add_argument(
        "--as-of",
        type=parse_as_of,
        default=None,
        help="the pricing time as 2026-10-27T12:00:00Z, with an offset or Z (default: now)",
    )
    parser.add_argument(
        "--strikes",
        required=True,
        nargs="+",
        type=_strike,
        help="strikes in degrees C of anomaly, like 1.0 1.1 1.2",
    )
    return parser


def _read_inputs(root: Path):
    """The SEAS5 members, the monthly ERA5 means and the labels under root."""
    try:
        seas5, era5_K, noaa_C = load_seas5(root), load_era5(root), load_noaa_gtm(root)
    except (OSError, ValueError, KeyError) as exc:
        raise nn_cli.UsageError(f"cannot read the research inputs under {root}: {exc}") from exc
    if noaa_C.empty:
        raise nn_cli.UsageError(f"no gtm labels in {ledger_path(root)}")
    return seas5, era5_K, noaa_C


def _load_emos(path: Path) -> EmosModel:
    """The fitted EMOS model at path; UsageError if it is not one."""
    try:
        model = EmosModel.from_json(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise nn_cli.UsageError(f"cannot load the EMOS model {path}: {exc}") from exc
    if not model.params:
        raise nn_cli.UsageError(f"the EMOS model {path} has no fitted lead")
    return model


def _freshest_init(nn_frame: pd.DataFrame, month: str, as_of: datetime) -> tuple[str, int]:
    """(init month, lead) of the freshest SEAS5 init of the target month public at as_of."""
    public = nn_frame[(nn_frame["target_month"] == month) & (nn_frame["decision_utc"] <= as_of)]
    if public.empty:
        raise nn_cli.UsageError(f"no SEAS5 init of {month} was public at {as_of.strftime(ISO_UTC)}")
    best = public.loc[public["decision_utc"].idxmax()]
    return str(best["init_month"]), int(best["lead_month"])


def _gaussian(prediction: pd.DataFrame) -> tuple[float, float]:
    """(mu, sigma) of the one predicted row; NaN for both when there is no row."""
    if prediction.empty:
        return math.nan, math.nan
    row = prediction.iloc[0]
    return float(row["mu"]), float(row["sigma"])


def _cell(gaussian: tuple[float, float], strike: float) -> str:
    """P(anomaly > strike) to 4 decimals, or - when the Gaussian is missing."""
    mu, sigma = gaussian
    if not (math.isfinite(mu) and math.isfinite(sigma) and sigma > 0):
        return "-"
    return f"{p_yes(strike, mu, sigma):.4f}"


def _number(value: float) -> str:
    return f"{value:.4f}" if math.isfinite(value) else "-"


def _price(args: argparse.Namespace) -> int:
    nn_cli.require_torch(("nn",))
    as_of = resolve_as_of(args.as_of)
    nn_dir = Path(args.nn_dir).expanduser()
    provenance = read_provenance(nn_dir, "seasonal")
    bridge = bridge_of(provenance, nn_dir)
    net = load_model(GtmNnModel, nn_dir)
    emos = _load_emos(Path(args.emos).expanduser())
    seas5, era5_K, noaa_C = _read_inputs(root_path(args.root))
    month = args.target_month
    try:
        nn_frame = seasonal_frame(seas5, era5_K, noaa_C, bridge=bridge)
        init, lead = _freshest_init(nn_frame, month, as_of)
        emos_frame = build_frame_from_inputs(seas5, era5_K, noaa_C)  # the bridge fitted on all
    except ValueError as exc:
        raise nn_cli.UsageError(f"cannot build the frames from the research inputs: {exc}") from exc
    nn_pick = nn_frame[(nn_frame["target_month"] == month) & (nn_frame["init_month"] == init)]
    in_init = emos_frame["init_date"].dt.strftime("%Y-%m") == init
    emos_pick = emos_frame[(emos_frame["target_month"] == month) & in_init]
    emos_gaussian = _gaussian(emos.predict(emos_pick))
    nn_gaussian = _gaussian(net.predict(nn_pick))
    shown = ", ".join(
        f"{name} mu={_number(mu)} sigma={_number(sigma)}"
        for name, (mu, sigma) in (("emos", emos_gaussian), ("nn", nn_gaussian))
    )
    print(
        f"target {month} as of {as_of.strftime(ISO_UTC)}, init {init} (lead {lead}), {shown}; "
        f"nn {training_note(provenance)}",
        file=sys.stderr,
    )
    print(TABLE_HEADER)
    print(TABLE_SEPARATOR)
    for strike in args.strikes:
        emos_p, nn_p = _cell(emos_gaussian, strike), _cell(nn_gaussian, strike)
        print(f"| {strike!r} | {emos_p} | {nn_p} |")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits with 2 on a usage error and 0 after --help
        return int(exc.code or 0)
    try:
        return _price(args)
    except nn_cli.UsageError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
