"""S1: the model's GTM distributions against the market's, from recorded paper payloads.

Reads gtm_signals.json payloads (a .json file each, or the history .jsonl),
fits the market-implied Gaussian of every ladder, and sets it beside the
model's forecast for the same month. Where the ledger holds the month's
settling value, both sides are scored (CRPS, and Brier on the quoted strikes).
Writes a run directory; nothing is written under the data root.

Exit codes: 0 done, 2 usage error or no usable ladder.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.nn.report import git_commit, write_run  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.sim.compare import compare_snapshots, first_prints, print_level_summary  # noqa: E402
from climate.sim.ladders import ladder_frame, read_payloads  # noqa: E402
from climate.sim.progress import ProgressWriter  # noqa: E402
from climate.sim.rundir import finish_run  # noqa: E402
from climate.storage import load_ledger  # noqa: E402

PAPER_FILES = ("gtm_signals_history.jsonl", "gtm_signals.json")


def default_payloads(root: Path) -> list[Path]:
    """The history and the latest payload under <root>/paper, those that exist."""
    return [root / "paper" / name for name in PAPER_FILES if (root / "paper" / name).exists()]


def new_run_dir(given: str | None, name: str) -> Path:
    if given is not None:
        run_dir = Path(given).expanduser()
    else:
        stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
        run_dir = Path.home() / "runs" / f"{name}_{stamp}"
    if run_dir.exists():
        raise SystemExit(f"the run directory exists already: {run_dir}")
    return run_dir


def _fmt(value, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "-"
    if isinstance(value, (bool, np.bool_)):
        return "yes" if value else "no"
    if isinstance(value, (float, np.floating)):
        return f"{value:.{digits}f}"
    return str(value)


def render_report(compared: pd.DataFrame, by_print: pd.DataFrame, sources: list[str]) -> str:
    lines = ["# GTM model against market (S1)", "", f"Payloads: {', '.join(sources)}", ""]
    lines += [
        "| snapshot (UTC) | target | lead | running | model mu, sd | market mu, sd | ceiling "
        "| identified | mu gap | P(>pivot) model / market | label | CRPS model / market |",
        "|---|---|---:|---|---|---|---:|---|---:|---|---:|---|",
    ]
    for r in compared.itertuples(index=False):
        lines.append(
            f"| {r.snapshot_utc:%Y-%m-%d %H:%M} | {r.target_month} | {r.lead_months} "
            f"| {_fmt(r.running_month)} | {_fmt(r.model_mu)}, {_fmt(r.model_sigma)} "
            f"| {_fmt(r.market_mu)}, {_fmt(r.market_sigma)} | {_fmt(r.market_ceiling)} "
            f"| {_fmt(r.identified)} | {_fmt(r.mu_gap)} "
            f"| >{r.pivot_strike:g}: {_fmt(r.p_model_pivot)} / {_fmt(r.p_market_pivot)} "
            f"| {_fmt(r.label, 2)} | {_fmt(r.crps_model, 4)} / {_fmt(r.crps_market, 4)} |"
        )
    lines.append("")
    if by_print.empty:
        lines.append("No settled month with an identified market fit: nothing is scored yet.")
    else:
        lines += ["## Settled months", "",
                  "| target | snapshots | CRPS model | CRPS market | model better |",
                  "|---|---:|---:|---:|---|"]
        for r in by_print.itertuples(index=False):
            lines.append(f"| {r.target_month} | {r.n_snapshots} | {r.crps_model:.4f} "
                         f"| {r.crps_market:.4f} | {_fmt(r.model_better)} |")
        wins = int(by_print["model_better"].sum())
        lines += ["", f"Model better on {wins} of {len(by_print)} settled months."]
    lines += ["", "A market fit is identified when at least two quotes sit inside the "
              "informative band; unidentified rows are shown, never scored."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--payload", action="append", default=None,
                    help="a payload .json or a history .jsonl (repeatable); default: "
                         "<root>/paper/gtm_signals_history.jsonl and gtm_signals.json")
    ap.add_argument("--root", default=None, help="climate data root (labels, default payloads)")
    ap.add_argument("--no-labels", action="store_true", help="compare only, score nothing")
    ap.add_argument("--out", default=None, help="run directory (default ~/runs/gtm_sim_score_<UTC>)")
    args = ap.parse_args(argv)

    root = root_path(args.root)
    sources = [Path(p) for p in args.payload] if args.payload else default_payloads(root)
    if not sources:
        print(f"no payloads given and none under {root / 'paper'}", file=sys.stderr)
        return 2
    missing = [str(p) for p in sources if not p.exists()]
    if missing:
        print(f"payload not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    ladders = ladder_frame(read_payloads(sources))
    labels = {} if args.no_labels else first_prints(load_ledger(root))
    compared = compare_snapshots(ladders, labels)
    if compared.empty:
        print("no snapshot holds a usable ladder with a model forecast", file=sys.stderr)
        return 2
    by_print = print_level_summary(compared)

    run_dir = new_run_dir(args.out, "gtm_sim_score")
    report = render_report(compared, by_print, [str(p) for p in sources])
    config = {"kind": "gtm_sim_score", "payloads": [str(p) for p in sources], "root": str(root),
              "labels": not args.no_labels, "git_commit": git_commit(), "run_dir": str(run_dir)}
    metrics = {
        "n_rows": int(len(compared)),
        "n_identified": int(compared["identified"].sum()),
        "n_scored": int(np.isfinite(compared["crps_diff"]).sum()),
        "settled_months": by_print.to_dict(orient="records"),
    }
    with ProgressWriter(run_dir, "gtm_sim_score", ["score"], steps=1, args=vars(args),
                        git_commit=git_commit(), start_thread=False) as writer:
        writer.cell_started("score")
        write_run(run_dir, config=config, metrics=metrics, rows=compared, report_md=report)
        headline = {"rows": metrics["n_rows"], "identified": metrics["n_identified"],
                    "scored": metrics["n_scored"]}
        writer.cell_finished("score", headline)
        finish_run(writer, run_dir, headline)
    print(report)
    print(f"run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
