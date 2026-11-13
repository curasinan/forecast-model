"""S4a: the P&L of GTM books for one print, under the model, the market and stresses.

A book is a list of positions STRIKE:LEG:QTY:PRICE. The market scenario is
the ceiling-removed implied Gaussian of the target month's ladder in the
chosen payload; the model scenarios are the payload's forecast and any given
with --model. Each (book, scenario) gets its exact settlement distribution:
expected P&L and ROI, chance of a loss and of losing everything, quantiles.
Writes a run directory.

Exit codes: 0 done, 2 usage error.
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
from climate.sim.book import (  # noqa: E402
    PRINT_RESOLUTION,
    Position,
    Scenario,
    book_cost,
    breakeven_mu,
    build_scenarios,
    mark_to_market,
    scenario_table,
)
from climate.sim.implied import fit_implied  # noqa: E402
from climate.sim.ladders import ladder_frame, read_payloads  # noqa: E402
from climate.sim.progress import ProgressWriter  # noqa: E402
from climate.sim.rundir import finish_run  # noqa: E402
from scripts.research.gtm_sim_score import new_run_dir  # noqa: E402


class UsageError(Exception):
    """A bad argument, said in one line."""


def parse_book(text: str) -> tuple[str, list[Position]]:
    """'NAME=STRIKE:LEG:QTY:PRICE[,...]'."""
    name, sep, body = text.partition("=")
    if not sep or not name.strip() or not body.strip():
        raise UsageError(f"--book {text!r} is not NAME=STRIKE:LEG:QTY:PRICE[,...]")
    try:
        return name.strip(), [Position.parse(part.strip()) for part in body.split(",")]
    except ValueError as exc:
        raise UsageError(f"--book {name}: {exc}") from exc


def parse_model(text: str) -> Scenario:
    """'NAME=MU:SIGMA'."""
    name, sep, body = text.partition("=")
    try:
        mu, sigma = (float(x) for x in body.split(":"))
    except ValueError as exc:
        raise UsageError(f"--model {text!r} is not NAME=MU:SIGMA") from exc
    if not sep or sigma <= 0:
        raise UsageError(f"--model {text!r} is not NAME=MU:SIGMA with SIGMA > 0")
    return Scenario(name.strip(), mu, sigma, "given on the command line")


def pick_snapshot(ladders: pd.DataFrame, target: str, snapshot: str | None) -> pd.DataFrame:
    rows = ladders[ladders["target_month"] == target]
    if rows.empty:
        raise UsageError(f"no ladder for target month {target} in the payloads")
    if snapshot is None:
        when = rows["snapshot_utc"].max()
    else:
        when = pd.Timestamp(snapshot)
        when = when.tz_localize("UTC") if when.tzinfo is None else when.tz_convert("UTC")
        if when not in set(rows["snapshot_utc"]):
            raise UsageError(f"no {target} ladder at snapshot {when}")
    return rows[rows["snapshot_utc"] == when]


def render_report(target: str, ladder: pd.DataFrame, fit, books: dict, table: pd.DataFrame,
                  marks: dict, breakevens: dict, fee: float) -> str:
    snap = ladder["snapshot_utc"].iloc[0]
    lines = [f"# GTM {target}: book P&L at settlement (S4a)", "",
             f"Ladder snapshot {snap:%Y-%m-%d %H:%M} UTC. "
             f"Prints resolve at {PRINT_RESOLUTION:g} degC; YES at k pays iff the print > k.", ""]
    if fit is None:
        lines.append("Market fit: none (fewer than three usable quotes).")
    else:
        lines.append(f"Market fit: mu {fit.mu:.3f}, sigma {fit.sigma:.3f}, ceiling {fit.ceiling:.3f}, "
                     f"{fit.n_quotes} quotes, {fit.n_interior} informative, "
                     f"identified {'yes' if fit.identified else 'no'}.")
    lines += ["", "| book | positions | cost | value at mid / bid now | breakeven mu (model sd, market sd) |",
              "|---|---|---:|---|---|"]
    for name, positions in books.items():
        pos = ", ".join(f"{p.qty:g} {p.leg} >{p.strike:g} @ {p.price:g}" for p in positions)
        m = marks[name]
        be = breakevens[name]
        lines.append(f"| {name} | {pos} | {book_cost(positions, fee):.2f} "
                     f"| {m['value_at_mid']:.2f} / {m['value_at_bid']:.2f} "
                     f"| {_f(be.get('model'))}, {_f(be.get('market'))} |")
    lines += ["", "| book | scenario | mu | sd | E[P&L] | E[ROI] | P(loss) | P(lose all) | 5% P&L |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in table.itertuples(index=False):
        lines.append(f"| {r.book} | {r.scenario} | {r.mu:.3f} | {r.sigma:.3f} | {r.expected_pnl:+.2f} "
                     f"| {r.expected_roi:+.0%} | {r.p_loss:.0%} | {r.p_total_loss:.0%} | {r.pnl_q05:+.2f} |")
    return "\n".join(lines) + "\n"


def _f(value) -> str:
    return "-" if value is None or not np.isfinite(value) else f"{value:.3f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--payload", action="append", required=True,
                    help="a payload .json or a history .jsonl (repeatable)")
    ap.add_argument("--target", required=True, help="target month, YYYY-MM")
    ap.add_argument("--snapshot", default=None, help="snapshot time (UTC); default the latest")
    ap.add_argument("--book", action="append", required=True,
                    help="NAME=STRIKE:LEG:QTY:PRICE[,...] (repeatable)")
    ap.add_argument("--model", action="append", default=[],
                    help="model scenario NAME=MU:SIGMA (repeatable); the first one given is the "
                         "base of the blends and stresses, else the payload's forecast is")
    ap.add_argument("--fee", type=float, default=0.01, help="commission per contract (default 0.01)")
    ap.add_argument("--out", default=None, help="run directory (default ~/runs/gtm_sim_book_<UTC>)")
    args = ap.parse_args(argv)

    try:
        books = dict(parse_book(b) for b in args.book)
        extra = [parse_model(m) for m in args.model]
        missing = [p for p in args.payload if not Path(p).exists()]
        if missing:
            raise UsageError(f"payload not found: {', '.join(missing)}")
        ladder = pick_snapshot(ladder_frame(read_payloads(args.payload)), args.target, args.snapshot)
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return 2

    mu0, sd0 = float(ladder["model_mu"].iloc[0]), float(ladder["model_sigma"].iloc[0])
    models = list(extra)  # a given model is the base of the blends and stresses
    if np.isfinite(mu0) and np.isfinite(sd0) and sd0 > 0:
        init = ladder["init"].iloc[0]
        models.append(Scenario(f"model_init_{init}" if init else "model", mu0, sd0, "payload forecast"))
    if not models:
        print("no model forecast in the payload and none given with --model", file=sys.stderr)
        return 2

    fit = fit_implied(ladder["strike"], ladder["bid"], ladder["ask"])
    scenarios = build_scenarios(models, fit)
    table = scenario_table(books, scenarios, fee_per_contract=args.fee)
    marks = {name: mark_to_market(pos, ladder[["strike", "bid", "ask"]]) for name, pos in books.items()}
    breakevens = {
        name: {"model": breakeven_mu(pos, models[0].sigma, fee_per_contract=args.fee),
               "market": (breakeven_mu(pos, fit.sigma, fee_per_contract=args.fee)
                          if fit is not None and fit.identified else None)}
        for name, pos in books.items()
    }

    run_dir = new_run_dir(args.out, "gtm_sim_book")
    report = render_report(args.target, ladder, fit, books, table, marks, breakevens, args.fee)
    config = {
        "kind": "gtm_sim_book", "payloads": args.payload, "target": args.target,
        "snapshot_utc": str(ladder["snapshot_utc"].iloc[0]), "fee": args.fee,
        "resolution": PRINT_RESOLUTION,
        "books": {n: [vars(p) for p in pos] for n, pos in books.items()},
        "models": [vars(m) for m in models], "git_commit": git_commit(), "run_dir": str(run_dir),
    }
    metrics = {"market_fit": fit.to_dict() if fit is not None else None,
               "marks": marks, "breakeven_mu": breakevens}
    with ProgressWriter(run_dir, "gtm_sim_book", ["book"], steps=1, args=vars(args),
                        git_commit=git_commit(), start_thread=False) as writer:
        writer.cell_started("book")
        write_run(run_dir, config=config, metrics=metrics, rows=table, report_md=report)
        headline = {"books": len(books), "scenarios": int(table["scenario"].nunique()),
                    "target": args.target}
        writer.cell_finished("book", headline)
        finish_run(writer, run_dir, headline)
    print(report)
    print(f"run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
