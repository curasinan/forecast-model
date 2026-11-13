"""S2: replay the recorded GTM ladders through the paper executor on a simulated venue.

At each decision time the paper loop runs as it does live, on the quotes and
the forecast of that moment: the latest sweep (hourly bars fill the recorder's
gaps), the EMOS refitted on the labels printed by then and the SEAS5 inits
released by then, the edge gate, the intents at the chosen limit rule, the
dedup and caps of the paper book, the executor, and the reconcile. Orders fill
against later snapshots, settle on the month's first print, and are marked to
the bid when the month has not printed. One run covers a grid of limit rules,
latencies and fill models over the same decisions.

Exit codes: 0 done, 2 usage error or nothing to replay.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import date, datetime, time
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.dataset import build_frame_from_inputs, load_era5, load_noaa_gtm, load_seas5  # noqa: E402
from climate.emos import EmosModel  # noqa: E402
from climate.ladder import load_contract_labels  # noqa: E402
from climate.nn.report import git_commit  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.sim.compare import first_prints  # noqa: E402
from climate.sim.forecast import PointInTimeForecaster, print_times  # noqa: E402
from climate.sim.panel import HOURLY, SWEEP, load_panel  # noqa: E402
from climate.sim.replay import (  # noqa: E402
    CADENCES,
    Policy,
    Replay,
    ReplayConfig,
    d1_verdict,
    decision_times,
    settlements,
    stop_times,
    target_months,
)
from climate.sim.venue import FILL_MODELS  # noqa: E402
from climate.storage import load_ledger  # noqa: E402

GRID_COLUMNS = [
    "cell", "orders", "orders_filled", "fill_rate", "contracts", "capital_deployed",
    "median_hours_to_fill", "paid_over_mid", "markout_1d", "markout_7d", "settled_pnl",
    "open_cost", "open_value_bid", "total_pnl_bid", "roi_bankroll", "max_drawdown",
]


class UsageError(Exception):
    """A bad argument, said in one line."""


def default_ticks_root() -> Path:
    """IBKR_DATA_ROOT, else ~/quant_data/ibkr_ticks (the research mirror for scura)."""
    override = os.environ.get("IBKR_DATA_ROOT", "").strip()
    return Path(override).expanduser() if override else Path.home() / "quant_data" / "ibkr_ticks"


def _csv(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _switch(text: str) -> list[bool]:
    return {"off": [False], "on": [True], "both": [False, True]}[text]


def build_grid(args) -> list[ReplayConfig]:
    try:
        policies = [Policy.parse(p) for p in _csv(args.policies)]
        latencies = [int(x) for x in _csv(args.latency)]
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    fills = _csv(args.fill)
    if not policies or not latencies or not fills:
        raise UsageError("--policies, --latency and --fill each need at least one value")
    if any(x < 1 for x in latencies):
        raise UsageError("--latency values must be at least 1 snapshot")
    bad = [f for f in fills if f not in FILL_MODELS]
    if bad:
        raise UsageError(f"--fill takes {', '.join(FILL_MODELS)}, not {', '.join(bad)}")
    common = dict(bankroll=args.bankroll, max_position=args.max_position,
                  max_exposure=args.max_exposure, max_group=args.max_group,
                  min_edge=args.min_edge, top=args.top,
                  max_age=pd.Timedelta(minutes=args.max_age_min))
    return [
        ReplayConfig(policy=p, latency=lat, fill_model=f, reprice=rep, skip_running_month=skip,
                     **common)
        for p, lat, f, rep, skip in product(policies, latencies, fills, _switch(args.reprice),
                                            _switch(args.skip_running_month))
    ]


def build_forecaster(root: Path, model_arg: str, ledger: pd.DataFrame,
                     fitted_at: datetime | None) -> PointInTimeForecaster:
    """The point-in-time EMOS over the root's SEAS5, ERA5 and labels."""
    frame = build_frame_from_inputs(load_seas5(root), load_era5(root), load_noaa_gtm(root))
    printed = print_times(frame["target_month"].astype(str), ledger)
    if model_arg == "pit":
        return PointInTimeForecaster(frame, printed, trend=True)
    path = Path(model_arg).expanduser()
    if not path.exists():
        raise UsageError(f"--model {path}: no such file (or pass 'pit')")
    return PointInTimeForecaster(frame, printed, model=EmosModel.from_json(path),
                                 model_fitted_at=fitted_at)


def _fmt(value, spec: str = ".2f") -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "-"
    return format(value, spec)


def render_report(summary: dict, grid: pd.DataFrame, verdict: list[str], focus) -> str:
    lines = ["# GTM quote replay (S2)", ""]
    lines += [f"- {k}: {v}" for k, v in summary.items()]
    lines += ["", "## Grid", "",
              "| cell | orders | filled | fill rate | contracts | deployed | h to fill | paid over mid "
              "| markout 1d / 7d | settled P&L | open P&L at bid | total P&L | ROI on bankroll | max DD |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|"]
    for r in grid.itertuples(index=False):
        open_pnl = (r.open_value_bid - r.open_cost) if np.isfinite(r.open_cost) else float("nan")
        lines.append(
            f"| {r.cell} | {r.orders} | {r.orders_filled} | {_fmt(r.fill_rate, '.0%')} "
            f"| {_fmt(r.contracts, '.0f')} | {_fmt(r.capital_deployed)} | {_fmt(r.median_hours_to_fill, '.1f')} "
            f"| {_fmt(r.paid_over_mid, '+.3f')} | {_fmt(r.markout_1d, '+.3f')} / {_fmt(r.markout_7d, '+.3f')} "
            f"| {_fmt(r.settled_pnl, '+.2f')} | {_fmt(open_pnl, '+.2f')} | {_fmt(r.total_pnl_bid, '+.2f')} "
            f"| {_fmt(r.roi_bankroll, '+.1%')} | {_fmt(r.max_drawdown)} |")
    lines += ["", "## D1, the limit rule", ""] + [f"- {v}" for v in verdict]
    if focus is not None:
        name, result = focus
        lines += ["", f"## {name}: by target month", "",
                  "| target | orders | filled | contracts | cost | settled | label | P&L | open P&L at bid |",
                  "|---|---:|---:|---:|---:|---|---:|---:|---:|"]
        for r in result.months.itertuples(index=False):
            lines.append(f"| {r.target_month} | {r.orders} | {r.filled_orders} | {r.contracts:.0f} "
                         f"| {r.cost:.2f} | {'yes' if r.settled else 'no'} | {_fmt(r.label)} "
                         f"| {_fmt(r.pnl, '+.2f') if r.settled else '-'} "
                         f"| {_fmt(r.unrealized_bid, '+.2f') if not r.settled else '-'} |")
        by_lead = result.metrics.get("pnl_by_lead") or {}
        if by_lead:
            lines += ["", "P&L by lead (0 = the running month), settled or marked to the bid: "
                      + ", ".join(f"{k}: {v:+.2f}" for k, v in sorted(by_lead.items()))]
    lines += ["", "Hourly bars stand in for the recorder where it has no sweep in the hour: their "
              "bid and ask are hour averages, and the live-like fill model fills nothing on them."]
    return "\n".join(lines) + "\n"


def write_run(run_dir: Path, *, config: dict, metrics: dict, grid: pd.DataFrame, report: str,
              results: list, work_dir: Path) -> None:
    """The run directory in one step: written beside, then renamed into place."""
    if run_dir.exists():
        raise UsageError(f"the run directory exists already: {run_dir}")
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    scratch = run_dir.with_name(f".{run_dir.name}.{uuid.uuid4().hex[:8]}.partial")
    scratch.mkdir()
    try:
        (scratch / "config.json").write_text(json.dumps(config, indent=2, default=str), encoding="utf-8")
        (scratch / "metrics.json").write_text(json.dumps(metrics, indent=2, default=_plain),
                                              encoding="utf-8")
        grid.to_parquet(scratch / "grid.parquet", index=False)
        (scratch / "report.md").write_text(report, encoding="utf-8", newline="\n")
        for result in results:
            cell = scratch / "cells" / result.config.name
            cell.mkdir(parents=True)
            for name in ("decisions", "orders", "months", "equity"):
                getattr(result, name).to_parquet(cell / f"{name}.parquet", index=False)
            shutil.copy2(work_dir / f"{result.config.name}.db", cell / "paper_book.db")
        os.rename(scratch, run_dir)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def _plain(value):
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    return str(value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=None, help="climate data root (SEAS5, ERA5, labels)")
    ap.add_argument("--ticks-root", default=None,
                    help="IBKR data root with quotes/ and quotes_hourly/ (default IBKR_DATA_ROOT "
                         "or ~/quant_data/ibkr_ticks)")
    ap.add_argument("--universe-json", default=str(REPO_ROOT / "data" / "ibkr_universe_live.json"))
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="first decision day")
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="last day replayed")
    ap.add_argument("--cadence", choices=CADENCES, default="daily",
                    help="daily (default), release (the 13th, after SEAS5), or sweep (every one)")
    ap.add_argument("--at", type=time.fromisoformat, default=time(14, 0),
                    help="decision time of day, UTC (default 14:00)")
    ap.add_argument("--policies", default="ask,ask-1,ask-2,mid",
                    help="limit rules, comma-separated: ask, ask-N, mid")
    ap.add_argument("--latency", default="1", help="snapshots before an order can fill, e.g. 1,2")
    ap.add_argument("--fill", default="paper,live", help="fill models: paper, live")
    ap.add_argument("--reprice", choices=["off", "on", "both"], default="off")
    ap.add_argument("--skip-running-month", choices=["off", "on", "both"], default="off")
    ap.add_argument("--model", default="pit",
                    help="'pit' refits the EMOS on the labels printed by each decision (default); "
                         "or a model JSON used as is")
    ap.add_argument("--model-fitted-at", default=None,
                    help="with a model JSON: when it was fitted (UTC); decisions before it are flagged")
    ap.add_argument("--no-hourly", action="store_true", help="sweeps only, no hourly bars")
    ap.add_argument("--max-age-min", type=float, default=40.0,
                    help="oldest snapshot a decision may use, minutes (default 40, as live)")
    ap.add_argument("--bankroll", type=float, default=100.0)
    ap.add_argument("--max-position", type=float, default=10.0)
    ap.add_argument("--max-exposure", type=float, default=50.0)
    ap.add_argument("--max-group", type=float, default=15.0)
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--out", default=None, help="run directory (default ~/runs/gtm_sim_replay_<UTC>)")
    args = ap.parse_args(argv)

    root = root_path(args.root)
    ticks_root = Path(args.ticks_root).expanduser() if args.ticks_root else default_ticks_root()
    try:
        grid_cfgs = build_grid(args)
        fitted_at = (pd.Timestamp(args.model_fitted_at, tz="UTC") if args.model_fitted_at else None)
        if args.out is not None:
            run_dir = Path(args.out).expanduser()
        else:
            stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
            run_dir = Path.home() / "runs" / f"gtm_sim_replay_{stamp}"
        if run_dir.exists():
            raise UsageError(f"the run directory exists already: {run_dir}")

        panel = load_panel(ticks_root, start=args.start, end=args.end, hourly=not args.no_hourly)
        if panel.empty:
            raise UsageError(f"no GTM quotes under {ticks_root} in the window")
        contracts = panel.contracts()
        labels = load_contract_labels(ticks_root, universe_json=args.universe_json)
        ledger = load_ledger(root)
        target_of = target_months(contracts, labels)
        months = sorted(set(target_of.values()))
        settle = settlements(target_of, first_prints(ledger), print_times(months, ledger))
        stops = stop_times(contracts, target_of, ledger)
        forecaster = build_forecaster(root, args.model, ledger, fitted_at)
        decisions = decision_times(panel, cadence=args.cadence, at=args.at,
                                   start=args.start, end=args.end)
        if not decisions:
            raise UsageError("no decision time falls inside the quotes")
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return 2

    work_dir = run_dir.with_name(f".{run_dir.name}.work")
    shutil.rmtree(work_dir, ignore_errors=True)
    results = []
    try:
        for cfg in grid_cfgs:
            print(f"replaying {cfg.name} over {len(decisions)} decisions", flush=True)
            results.append(Replay(panel, contracts, labels, forecaster, settle, stops, cfg,
                                  work_dir).run(decisions))

        sources = panel.frame.drop_duplicates("ts")["source"].value_counts().to_dict()
        summary = {
            "quotes": f"{panel.times[0]:%Y-%m-%d %H:%M} to {panel.times[-1]:%Y-%m-%d %H:%M} UTC, "
                      f"{sources.get(SWEEP, 0)} sweeps, {sources.get(HOURLY, 0)} hourly snapshots",
            "contracts": f"{len(contracts)} legs over target months {', '.join(months)}",
            "settled months": ", ".join(f"{m} = {s.value:.2f} ({s.vintage})"
                                        for m, s in settle.items() if s.value is not None) or "none",
            "decisions": f"{len(decisions)} ({args.cadence} at {args.at:%H:%M} UTC)",
            "model": ("EMOS refitted on the labels printed by each decision" if args.model == "pit"
                      else f"fixed {args.model}"),
            "caps": f"bankroll {args.bankroll:g}, position {args.max_position:g}, month "
                    f"{args.max_group:g}, exposure {args.max_exposure:g}, min edge {args.min_edge:g}",
        }
        cells = {r.config.name: (r.config, r.metrics) for r in results}
        verdict = d1_verdict(cells)
        grid = pd.DataFrame([{"cell": r.config.name, **{c: r.metrics.get(c, np.nan)
                                                        for c in GRID_COLUMNS[1:]}}
                             for r in results], columns=GRID_COLUMNS)
        focus = next(((r.config.name, r) for r in results
                      if r.config.policy.name == "ask" and r.config.latency == 1
                      and r.config.fill_model == "paper"), None)
        if focus is None and results:
            focus = (results[0].config.name, results[0])
        report = render_report(summary, grid, verdict, focus)
        config = {"kind": "gtm_sim_replay", "root": str(root), "ticks_root": str(ticks_root),
                  "args": vars(args), "cells": [r.config.name for r in results],
                  "git_commit": git_commit(), "run_dir": str(run_dir)}
        metrics = {"summary": summary, "d1": verdict,
                   "cells": {r.config.name: r.metrics for r in results},
                   "notes": {r.config.name: r.notes for r in results if r.notes}}
        write_run(run_dir, config=config, metrics=metrics, grid=grid, report=report,
                  results=results, work_dir=work_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    print(report)
    print(f"run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
