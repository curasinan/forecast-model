"""S3: the GTM paper book traded month by month over past years against synthetic markets.

No GTM market existed before 2026, so each past month is quoted by a
synthetic market instead: trend climatology (m0), damped persistence (m1),
the raw bridged SEAS5 ensemble (m2), the model shifted as the 2026 market
was against it (m3), each optionally blended toward the model at weight w,
and an informed market that knows the label up to an error of chosen size.
The model trades them exactly as the replay does (climate/sim/replay.py):
the EMOS refitted on the labels printed by each decision, with the bridge
refitted each year on the labels through the previous December; the edge
gate, intents at the ask, caps, executor and reconcile of the paper book.
The answer is how good a market may be before the model stops making money.

Exit codes: 0 done, 2 usage error.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time as _time
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from climate.dataset import load_era5, load_noaa_gtm, load_seas5  # noqa: E402
from climate.nn.report import git_commit  # noqa: E402
from climate.paths import root_path  # noqa: E402
from climate.sim.forecast import print_times  # noqa: E402
from climate.sim.hindcast import (  # noqa: E402
    PROXIES,
    PROXY_NAMES,
    MarketModel,
    MarketSpec,
    StrictForecaster,
    breakeven,
    build_venue,
    hindcast_decisions,
    hindcast_metrics,
    settlement_values,
    yearly_table,
)
from climate.sim.progress import ProgressWriter  # noqa: E402
from climate.sim.replay import (  # noqa: E402
    Policy,
    Replay,
    ReplayConfig,
    settlements,
    stop_times,
    target_months,
)
from climate.sim.rundir import finish_run, write_run  # noqa: E402
from climate.storage import load_ledger  # noqa: E402

GRID_COLUMNS = ["cell", "proxy", "weight", "sd", "total_pnl", "mean_year", "sd_year", "t_stat",
                "share_years_up", "orders_filled", "contracts", "capital_deployed", "roi_deployed",
                "hit_rate", "pnl_before", "pnl_from", "max_drawdown"]


class UsageError(Exception):
    """A bad argument, said in one line."""


def _floats(text: str, what: str) -> list[float]:
    try:
        return [float(x) for x in text.split(",") if x.strip()]
    except ValueError as exc:
        raise UsageError(f"{what}: {exc}") from exc


def build_specs(args) -> list[MarketSpec]:
    proxies = [p.strip() for p in args.proxies.split(",") if p.strip()]
    bad = [p for p in proxies if p not in PROXIES or p == "informed"]
    if bad:
        raise UsageError(f"--proxies takes m0, m1, m2, m3, not {', '.join(bad)} "
                         "(the informed market comes from --informed-sd)")
    weights = _floats(args.blends, "--blends")
    sds = _floats(args.informed_sd, "--informed-sd")
    if proxies and not weights:
        raise UsageError("--blends needs at least one weight (0 is the proxy alone)")
    try:
        specs = [MarketSpec(p, weight=w, bias=args.m3_bias if p == "m3" else 0.0,
                            ratio=args.m3_sigma_ratio if p == "m3" else 1.0)
                 for p in proxies for w in weights]
        specs += [MarketSpec("informed", sd=sd) for sd in sds]
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    if not specs:
        raise UsageError("nothing to run: give --proxies or --informed-sd")
    return specs


def _f(value, spec=".2f") -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "-"
    return format(value, spec)


def render_report(summary: dict, grid: pd.DataFrame, breakevens: dict, yearly: dict,
                  split_year: int) -> str:
    lines = ["# GTM hindcast against synthetic markets (S3)", ""]
    lines += [f"- {k}: {v}" for k, v in summary.items()]
    lines += ["", "## Grid", "",
              f"| cell | market | trades | contracts | deployed | total P&L | per year (sd) | t | years up "
              f"| hit rate | before {split_year} | from {split_year} | max DD |",
              "|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|"]
    for r in grid.itertuples(index=False):
        market = PROXY_NAMES[r.proxy] + (f", w {r.weight:g}" if r.proxy != "informed" else
                                         f", error sd {r.sd:g}")
        lines.append(
            f"| {r.cell} | {market} | {r.orders_filled} | {_f(r.contracts, '.0f')} | {_f(r.capital_deployed)} "
            f"| {_f(r.total_pnl, '+.2f')} | {_f(r.mean_year, '+.2f')} ({_f(r.sd_year)}) | {_f(r.t_stat)} "
            f"| {_f(r.share_years_up, '.0%')} | {_f(r.hit_rate, '.0%')} | {_f(r.pnl_before, '+.2f')} "
            f"| {_f(r.pnl_from, '+.2f')} | {_f(r.max_drawdown)} |")
    lines += ["", "## Break-even", ""]
    for name, text in breakevens.items():
        lines.append(f"- {name}: {text}")
    pure = [c for c in grid["cell"] if c.endswith("_w0") or c.startswith("informed")]
    if pure:
        years = yearly[pure[0]]["year"].tolist()
        lines += ["", "## P&L by year", "", "| year | " + " | ".join(pure) + " |",
                  "|---:|" + "---:|" * len(pure)]
        for i, y in enumerate(years):
            lines.append(f"| {y} | " + " | ".join(f"{yearly[c]['pnl'].iloc[i]:+.2f}" for c in pure) + " |")
    lines += ["", "Caveats: labels before 2026-08 are the current vintage, not first prints "
              "(--label-noise adds first-print error); SEAS5 hindcasts have 25 members before 2017 "
              "and 51 after; a synthetic market quotes the same prices for the hour after a "
              "decision, so only orders at the ask fill; the model trades where it disagrees "
              "most with the market, which is also where it is most often wrong."]
    return "\n".join(lines) + "\n"


def breakeven_lines(grid: pd.DataFrame) -> dict[str, str]:
    out = {}
    for proxy in [p for p in PROXIES if p != "informed"]:
        rows = grid[grid["proxy"] == proxy].sort_values("weight")
        if len(rows) < 2:
            continue
        x = breakeven(list(zip(rows["weight"], rows["total_pnl"])))
        first, last = rows.iloc[0], rows.iloc[-1]
        if x is not None:
            out[PROXY_NAMES[proxy]] = f"P&L reaches zero at a blend weight of {x:.2f}"
        elif first["total_pnl"] <= 0:
            out[PROXY_NAMES[proxy]] = f"P&L is already {first['total_pnl']:+.2f} against the proxy alone"
        else:
            out[PROXY_NAMES[proxy]] = (f"P&L stays positive through w = {last['weight']:g} "
                                       f"({last['total_pnl']:+.2f})")
    inf = grid[grid["proxy"] == "informed"].sort_values("sd")
    if len(inf) >= 2:
        x = breakeven(list(zip(inf["sd"], inf["total_pnl"])), rising=True)
        if x is not None:
            out["informed market"] = (f"the model makes money once the market's error sd "
                                      f"exceeds about {x:.3f} degC")
        elif inf.iloc[0]["total_pnl"] > 0:
            out["informed market"] = (f"the model makes money even against an error sd of "
                                      f"{inf.iloc[0]['sd']:g} degC")
        else:
            out["informed market"] = (f"the model loses even against an error sd of "
                                      f"{inf.iloc[-1]['sd']:g} degC")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=None, help="climate data root (SEAS5, ERA5, labels)")
    ap.add_argument("--first-year", type=int, default=2001)
    ap.add_argument("--last-year", type=int, default=2025)
    ap.add_argument("--proxies", default="m0,m1,m2,m3", help="synthetic markets: m0, m1, m2, m3")
    ap.add_argument("--blends", default="0,0.25,0.5,0.75",
                    help="weights toward the model, 0 = the proxy alone (default 0,0.25,0.5,0.75)")
    ap.add_argument("--informed-sd", default="0.04,0.06,0.08,0.1,0.12",
                    help="error sds of the informed market, degC; empty for none")
    ap.add_argument("--m3-bias", type=float, default=-0.13,
                    help="m3: added to the model's mu (default -0.13, the 2026-10-16 December gap)")
    ap.add_argument("--m3-sigma-ratio", type=float, default=1.56,
                    help="m3: multiplies the model's sigma (default 1.56, from the same ladder)")
    ap.add_argument("--months-ahead", type=int, default=4,
                    help="months quoted ahead of the running month (default 4)")
    ap.add_argument("--half-spread", type=float, default=0.015)
    ap.add_argument("--ceiling", type=float, default=1.0,
                    help="price of a certain YES (the live ladders show about 0.93)")
    ap.add_argument("--label-noise", type=float, default=0.0,
                    help="sd of first-print error added to the settling value, degC")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--day", type=int, default=14, help="decision day of the month (default 14)")
    ap.add_argument("--at", type=time.fromisoformat, default=time(14, 0))
    ap.add_argument("--bankroll", type=float, default=100.0)
    ap.add_argument("--max-position", type=float, default=10.0)
    ap.add_argument("--max-exposure", type=float, default=50.0)
    ap.add_argument("--max-group", type=float, default=15.0)
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--split-year", type=int, default=2013, help="first year of the second period")
    ap.add_argument("--out", default=None, help="run directory (default ~/runs/gtm_sim_hindcast_<UTC>)")
    args = ap.parse_args(argv)

    root = root_path(args.root)
    try:
        specs = build_specs(args)
        if args.first_year > args.last_year:
            raise UsageError("--first-year is after --last-year")
        if not 13 <= args.day <= 28:
            raise UsageError("--day must fall between the 13th (SEAS5 release) and the 28th")
        if not 0.5 < args.ceiling <= 1.0 or args.half_spread <= 0 or args.months_ahead < 0:
            raise UsageError("--ceiling must be in (0.5, 1], --half-spread positive, "
                             "--months-ahead at least 0")
        if args.out is not None:
            run_dir = Path(args.out).expanduser()
        else:
            stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
            run_dir = Path.home() / "runs" / f"gtm_sim_hindcast_{stamp}"
        if run_dir.exists():
            raise UsageError(f"the run directory exists already: {run_dir}")
        try:
            seas5, era5, labels = load_seas5(root), load_era5(root), load_noaa_gtm(root)
        except (FileNotFoundError, OSError) as exc:
            raise UsageError(f"cannot read the climate root {root}: {exc}") from exc
        if labels.empty:
            raise UsageError(f"no GTM labels under {root}")
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return 2

    writer = ProgressWriter(run_dir, "gtm_sim_hindcast", [s.name for s in specs],
                            args=vars(args), git_commit=git_commit())
    with writer:
        ledger = load_ledger(root)
        printed = print_times(labels.index, ledger)
        last_month = min(f"{args.last_year}-12", str(labels.index.max()))
        model = StrictForecaster(seas5, era5, labels, ledger)
        decisions = hindcast_decisions(args.first_year, args.last_year, day=args.day, at=args.at)
        writer.set_steps(len(decisions))
        writer.run_phase("trading the cells")
        years = list(range(args.first_year, args.last_year + 1))
        cfg = ReplayConfig(policy=Policy.parse("ask"), bankroll=args.bankroll,
                           max_position=args.max_position, max_exposure=args.max_exposure,
                           max_group=args.max_group, min_edge=args.min_edge)
        work_dir = run_dir.with_name(f".{run_dir.name}.work")
        shutil.rmtree(work_dir, ignore_errors=True)
        rows, cells, yearly, extra = [], {}, {}, {}
        try:
            for i, spec in enumerate(specs, start=1):
                started = _time.monotonic()
                writer.cell_started(spec.name, phase="building quotes")
                market = MarketModel(spec, labels, printed, model, seed=args.seed)
                venue = build_venue(market, decisions, last_month=last_month,
                                    months_ahead=args.months_ahead, half_spread=args.half_spread,
                                    ceiling=args.ceiling)
                contracts = venue.panel.contracts()
                target_of = target_months(contracts, None)
                months = sorted(set(target_of.values()))
                settle = settlements(target_of, settlement_values(labels, months,
                                                                  noise=args.label_noise,
                                                                  seed=args.seed),
                                     print_times(months, ledger))
                writer.cell_phase(spec.name, "")
                cell_dir = work_dir / spec.name
                result = Replay(venue.panel, contracts, None, model, settle,
                                stop_times(contracts, target_of, ledger), cfg,
                                cell_dir).run(decisions, on_step=writer.stepper(spec.name))
                table = yearly_table(result, years)
                metrics = hindcast_metrics(table, result, split_year=args.split_year)
                row = {"cell": spec.name, "proxy": spec.kind, "weight": spec.weight,
                       "sd": spec.sd, **{c: metrics.get(c) for c in GRID_COLUMNS[4:]}}
                rows.append(row)
                cells[spec.name] = (result, cell_dir / f"{cfg.name}.db")
                yearly[spec.name] = table
                extra[spec.name] = {"yearly": table}
                writer.cell_finished(spec.name, row,
                                     frames={"orders": result.orders, "equity": result.equity,
                                             "months": result.months, "yearly": table})
                print(f"[{i}/{len(specs)}] {spec.name}: P&L {metrics['total_pnl']:+.2f} on "
                      f"{metrics['orders_filled']} trades ({_time.monotonic() - started:.0f} s)",
                      flush=True)

            writer.run_phase("writing results")
            grid = pd.DataFrame(rows, columns=GRID_COLUMNS)
            breakevens = breakeven_lines(grid)
            summary = {
                "years": f"{args.first_year} to {args.last_year}, {len(decisions)} decisions "
                         f"(the {args.day}th at {args.at:%H:%M} UTC)",
                "model": "EMOS refitted on the labels printed by each decision; bridge refitted "
                         "each year on the labels through the previous December",
                "markets": ", ".join(PROXY_NAMES[k] for k in dict.fromkeys(s.kind for s in specs)),
                "quotes": f"{args.months_ahead + 1} months open, half-spread {args.half_spread:g}, "
                          f"ceiling {args.ceiling:g}, label noise {args.label_noise:g}",
                "caps": f"bankroll {args.bankroll:g}, position {args.max_position:g}, month "
                        f"{args.max_group:g}, exposure {args.max_exposure:g}, min edge {args.min_edge:g}",
            }
            report = render_report(summary, grid, breakevens, yearly, args.split_year)
            config = {"kind": "gtm_sim_hindcast", "root": str(root), "args": vars(args),
                      "cells": [s.name for s in specs], "git_commit": git_commit(),
                      "run_dir": str(run_dir)}
            metrics_out = {"summary": summary, "breakeven": breakevens,
                           "cells": {r["cell"]: r for r in rows}}
            write_run(run_dir, config=config, metrics=metrics_out, grid=grid, report=report,
                      cells=cells, extra=extra)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        ranked = grid.sort_values("total_pnl")
        headline = {"cells": len(rows), **{f"breakeven {k}": v for k, v in breakevens.items()}}
        if len(ranked):
            headline.update(best_cell=ranked.iloc[-1]["cell"], best_pnl=ranked.iloc[-1]["total_pnl"],
                            worst_cell=ranked.iloc[0]["cell"], worst_pnl=ranked.iloc[0]["total_pnl"])
        finish_run(writer, run_dir, headline)
    print(report)
    print(f"run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
