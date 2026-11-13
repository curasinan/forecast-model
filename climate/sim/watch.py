"""Terminal views of the simulations' progress files, built with rich."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd
from rich import box
from rich.console import Console, Group
from rich.measure import Measurement
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from climate.sim.progress import RunEntry, _parse_iso, liveness

SPARK = "▁▂▃▄▅▆▇█"
STATUS_STYLE = {"running": "cyan", "finished": "green", "failed": "bold red", "dead": "bold red",
                "stalled": "yellow", "silent": "yellow", "queued": "dim"}
KIND_LABEL = {"gtm_sim_replay": "S2 replay", "gtm_sim_hindcast": "S3 hindcast",
              "gtm_sim_score": "S1 score", "gtm_sim_book": "S4a book"}


def fmt_duration(seconds) -> str:
    if seconds is None or not np.isfinite(seconds) or seconds < 0:
        return "-"
    seconds = int(round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def money(value, digits: int = 2) -> Text:
    """A signed amount, green when up and red when down."""
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return Text("-", style="dim")
    style = "green" if value > 0 else "red" if value < 0 else ""
    return Text(f"{value:+,.{digits}f}", style=style)


def sparkline(values: Sequence[float], width: int = 16) -> str:
    """The shape of a series in block characters; flat series sit in the middle."""
    vals = [float(v) for v in values if v is not None and np.isfinite(v)]
    if not vals:
        return ""
    if len(vals) > width:
        idx = np.linspace(0, len(vals) - 1, width).round().astype(int)
        vals = [vals[i] for i in idx]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return SPARK[3] * len(vals)
    return "".join(SPARK[int(round((v - lo) / (hi - lo) * (len(SPARK) - 1)))] for v in vals)


def status_text(status: str) -> Text:
    return Text(status, style=STATUS_STYLE.get(status, ""))


def overall(state: Mapping) -> tuple[int, int, float | None]:
    """(steps done, steps in all, seconds left) over every cell of a run."""
    cells = state.get("cells") or []
    total = sum(int(c.get("steps") or 0) for c in cells)
    done = sum(int(c.get("steps") or 0) if c.get("status") == "finished" else int(c.get("step") or 0)
               for c in cells)
    elapsed = sum(float(c.get("elapsed_s") or 0.0) for c in cells)
    eta = elapsed / done * (total - done) if done and total else None
    return done, total, eta


def _elapsed(state: Mapping, now: float) -> float | None:
    start = _parse_iso(state.get("started_utc"))
    end = _parse_iso(state.get("finished_utc")) or now
    return end - start if start else None


def _sim_date(value) -> str:
    if not value:
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except ValueError:
        return str(value)[:10]


# columns of the cell table, most important first; the tail is dropped to fit the terminal
CELL_COLUMNS = ("cell", "progress", "status", "marked", "settled", "ord/fill", "left", "skip/warn",
                "sim date", "equity", "DD")
_RIGHT = {"left", "ord/fill", "settled", "marked", "DD", "skip/warn"}
BAR_WIDTH = 8
SPARK_WIDTH = 12


_MEASURE = Console(width=10_000, color_system=None, file=open(os.devnull, "w"))  # noqa: SIM115


def _measure(renderable) -> int:
    """The width rich needs to draw `renderable` without wrapping or cutting."""
    return Measurement.get(_MEASURE, _MEASURE.options, renderable).maximum


def _cell_values(c: Mapping) -> dict:
    steps = int(c.get("steps") or 0)
    step = steps if c.get("status") == "finished" else int(c.get("step") or 0)
    label = c.get("phase") or c.get("status", "?")
    metrics = c.get("metrics") or {}
    warn = int(c.get("warnings") or 0)
    return {
        "cell": Text(str(c.get("name", "?"))),
        "status": status_text(label) if label in STATUS_STYLE else Text(label, style="cyan"),
        "progress": (step, steps),
        "sim date": Text(_sim_date(c.get("sim_time"))),
        "left": Text(fmt_duration(c.get("eta_s")) if c.get("status") == "running" else ""),
        "ord/fill": Text(f"{c.get('orders', 0)}/{c.get('filled', 0)}"),
        "settled": money(metrics.get("total_pnl", c.get("pnl_settled"))),
        "marked": money(metrics.get("total_pnl_bid", c.get("pnl_marked"))),
        "DD": Text(f"{float(c.get('drawdown') or 0):.2f}"),
        "skip/warn": Text(f"{c.get('skipped', 0)}/{warn}", style="yellow" if warn else ""),
        "equity": Text(sparkline(c.get("curve") or [], width=SPARK_WIDTH), style="cyan"),
    }


def cells_table(cells: Sequence[Mapping], width: int = 160) -> Table:
    """A row per cell; columns that would not fit `width` are dropped, never cut short."""
    rows = [_cell_values(c) for c in cells]

    def need(col: str) -> int:
        if col == "progress":
            counts = [len(f"{a}/{b}") for a, b in (r["progress"] for r in rows)] or [3]
            return BAR_WIDTH + 1 + max(counts)
        return max([len(col)] + [r[col].cell_len for r in rows])

    widths = {col: need(col) for col in CELL_COLUMNS}

    def build(shown: list[str]) -> Table:
        table = Table(expand=False, pad_edge=False, box=box.SIMPLE_HEAD)
        for col in shown:
            table.add_column(col, justify="right" if col in _RIGHT else "left", no_wrap=True,
                             min_width=widths[col], max_width=widths[col] if col != "cell" else None)
        for r in rows:
            values = []
            for col in shown:
                if col == "progress":
                    step, steps = r["progress"]
                    bar = Table.grid(padding=(0, 1))
                    bar.add_row(ProgressBar(total=max(steps, 1), completed=step, width=BAR_WIDTH),
                                Text(f"{step}/{steps}", style="dim"))
                    values.append(bar)
                else:
                    values.append(r[col])
            table.add_row(*values)
        return table

    shown = list(CELL_COLUMNS)
    table = build(shown)
    while len(shown) > 3 and _measure(table) > width:
        shown.pop()
        table = build(shown)
    return table



def render_list(entries: Sequence[RunEntry], now: float | None = None, limit: int = 10) -> Table:
    """Active runs first, then the latest finished ones."""
    now = time.time() if now is None else now
    table = Table(title="GTM simulation runs", title_justify="left", expand=False,
                  box=box.SIMPLE_HEAD)
    for col, justify in (("status", "left"), ("kind", "left"), ("run", "left"), ("started (UTC)", "left"),
                         ("cells", "right"), ("progress", "right"), ("elapsed", "right"),
                         ("headline", "left")):
        table.add_column(col, justify=justify, no_wrap=col != "headline")
    active = [e for e in entries if e.active and liveness(e.state, now) not in ("finished", "failed")]
    rest = [e for e in entries if e not in active][:limit]
    for entry in active + rest:
        state = entry.state
        cells = state.get("cells") or []
        finished = sum(1 for c in cells if c.get("status") == "finished")
        done, total, _ = overall(state)
        started = state.get("started_utc") or ""
        table.add_row(
            status_text(liveness(state, now)),
            KIND_LABEL.get(state.get("kind"), str(state.get("kind"))),
            entry.name,
            started.replace("T", " ").replace("Z", "")[:16],
            f"{finished}/{len(cells)}" if cells else "-",
            f"{done / total:.0%}" if total else "-",
            fmt_duration(_elapsed(state, now)),
            _headline_text(state),
        )
    if not active and not rest:
        table.add_row("-", "-", "no runs yet", "", "", "", "", "")
    return table


def _headline_text(state: Mapping) -> str:
    if state.get("status") == "failed":
        return f"failed: {state.get('error') or '?'}"
    head = state.get("headline") or {}
    parts = []
    if head.get("d1"):
        parts.append(str(head["d1"]))
    if head.get("best_cell") is not None:
        parts.append(f"best {head['best_cell']} {head.get('best_pnl', 0):+.2f}")
    if not parts and state.get("phase"):
        parts.append(str(state["phase"]))
    return "; ".join(parts)


def render_run(state: Mapping, now: float | None = None, *, events: int = 8,
               width: int = 160) -> Group:
    """One run: its header, a row per cell with a bar, the latest events, fitted to `width`."""
    now = time.time() if now is None else now
    status = liveness(state, now)
    done, total, eta = overall(state)
    head = Table.grid(padding=(0, 2))
    head.add_column(style="bold")
    head.add_column()
    title = Text(state.get("run", "?"), style="bold")
    title.append(f"   {KIND_LABEL.get(state.get('kind'), state.get('kind'))}", style="")
    head.add_row("run", title)
    line = status_text(status)
    if state.get("phase"):
        line.append(f"   {state['phase']}")
    if status in ("dead", "stalled"):
        beat = _parse_iso(state.get("heartbeat_utc"))
        line.append(f"   last heartbeat {fmt_duration(now - beat) if beat else '?'} ago", style="yellow")
        if status == "dead":
            line.append("   (process gone: check the launcher's memory ceiling and the journal)",
                        style="yellow")
    if state.get("error"):
        line.append(f"   {state['error']}", style="red")
    head.add_row("status", line)
    head.add_row("where", f"{state.get('host', '?')} pid {state.get('pid', '?')}   commit "
                          f"{state.get('git_commit') or '?'}")
    bar = Table.grid(padding=(0, 1))
    bar.add_row(ProgressBar(total=max(total, 1), completed=done, width=30),
                Text(f"{done}/{total} decisions" if total else "starting"),
                Text(f"elapsed {fmt_duration(_elapsed(state, now))}"),
                Text(f"left {fmt_duration(eta)}" if status == "running" else ""))
    head.add_row("progress", bar)
    if state.get("headline"):
        head.add_row("headline", _headline_text(state))

    cells = cells_table(state.get("cells") or [], width)
    log = Table(title="latest events", title_justify="left", show_header=False, box=None, pad_edge=False)
    log.add_column(style="dim", no_wrap=True)
    log.add_column(no_wrap=True)
    log.add_column()
    for e in (state.get("events_tail") or [])[-events:]:
        detail = {k: v for k, v in e.items() if k not in ("ts", "kind", "cell")}
        text = e.get("message") or e.get("error") or ", ".join(
            f"{k} {v:+.2f}" if isinstance(v, float) else f"{k} {v}" for k, v in list(detail.items())[:4])
        kind = e.get("kind", "")
        where = e.get("cell") or ""
        log.add_row(str(e.get("ts", ""))[11:19], Text(kind, style="yellow" if kind == "warning" else
                                                      "red" if kind == "run_failed" else ""),
                    f"{where}: {text}" if where and text else where or text)
    return Group(head, Text(""), cells, Text(""), log)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def render_results(run_dir: Path, top: int = 25) -> Group:
    """A finished run's results: its grid (or rows), its verdicts, where the files are."""
    run_dir = Path(run_dir)
    config = _read_json(run_dir / "config.json")
    metrics = _read_json(run_dir / "metrics.json")
    kind = config.get("kind", "?")
    parts: list = []
    table = Table(title=f"results: {KIND_LABEL.get(kind, kind)}", title_justify="left",
                  box=box.SIMPLE_HEAD)
    if kind == "gtm_sim_replay" and (run_dir / "grid.parquet").exists():
        grid = pd.read_parquet(run_dir / "grid.parquet").sort_values("total_pnl_bid", ascending=False)
        cols = [("cell", "cell"), ("orders", "orders"), ("filled", "orders_filled"),
                ("fill rate", "fill_rate"), ("deployed", "capital_deployed"),
                ("h to fill", "median_hours_to_fill"), ("P&L at bid", "total_pnl_bid"),
                ("ROI", "roi_bankroll"), ("max DD", "max_drawdown")]
        _fill(table, grid.head(top), cols, money_cols={"total_pnl_bid"},
              pct_cols={"fill_rate", "roi_bankroll"})
        parts.append(table)
        parts += [Text(line) for line in metrics.get("d1", [])]
    elif kind == "gtm_sim_hindcast" and (run_dir / "grid.parquet").exists():
        grid = pd.read_parquet(run_dir / "grid.parquet").sort_values("total_pnl", ascending=False)
        cols = [("cell", "cell"), ("trades", "orders_filled"), ("total P&L", "total_pnl"),
                ("per year", "mean_year"), ("t", "t_stat"), ("years up", "share_years_up"),
                ("hit rate", "hit_rate"), ("before", "pnl_before"), ("from", "pnl_from"),
                ("max DD", "max_drawdown")]
        _fill(table, grid.head(top), cols, money_cols={"total_pnl", "mean_year", "pnl_before", "pnl_from"},
              pct_cols={"share_years_up", "hit_rate"})
        parts.append(table)
        parts += [Text(f"break-even, {k}: {v}") for k, v in (metrics.get("breakeven") or {}).items()]
    elif (run_dir / "rows.parquet").exists():
        rows = pd.read_parquet(run_dir / "rows.parquet")
        if kind == "gtm_sim_book":
            cols = [("book", "book"), ("scenario", "scenario"), ("mu", "mu"), ("sd", "sigma"),
                    ("E[P&L]", "expected_pnl"), ("E[ROI]", "expected_roi"), ("P(loss)", "p_loss"),
                    ("P(lose all)", "p_total_loss")]
            _fill(table, rows.head(top), cols, money_cols={"expected_pnl"},
                  pct_cols={"expected_roi", "p_loss", "p_total_loss"})
        else:
            cols = [("snapshot", "snapshot_utc"), ("target", "target_month"), ("model mu", "model_mu"),
                    ("market mu", "market_mu"), ("gap", "mu_gap"), ("identified", "identified"),
                    ("CRPS model", "crps_model"), ("CRPS market", "crps_market")]
            _fill(table, rows.head(top), cols)
        parts.append(table)
    else:
        parts.append(Text("no results in this run directory yet", style="dim"))
    for name in ("report.md", "dashboard.html"):
        if (run_dir / name).exists():
            parts.append(Text(f"{name}: {run_dir / name}", style="dim"))
    return Group(*parts)


def _fill(table: Table, frame: pd.DataFrame, cols, money_cols=frozenset(), pct_cols=frozenset()) -> None:
    for label, _ in cols:
        table.add_column(label, justify="left" if label in ("cell", "book", "scenario", "target",
                                                            "snapshot") else "right", no_wrap=True)
    for _, row in frame.iterrows():
        cells = []
        for _, key in cols:
            value = row.get(key)
            if key in money_cols:
                cells.append(money(value))
            elif value is None or (isinstance(value, float) and not np.isfinite(value)):
                cells.append(Text("-", style="dim"))
            elif key in pct_cols:
                cells.append(Text(f"{value:.0%}"))
            elif isinstance(value, (float, np.floating)):
                cells.append(Text(f"{value:.3f}" if abs(value) < 10 else f"{value:,.1f}"))
            elif isinstance(value, pd.Timestamp):
                cells.append(Text(value.strftime("%Y-%m-%d %H:%M")))
            else:
                cells.append(Text(str(value)))
        table.add_row(*cells)


__all__ = ["cells_table", "fmt_duration", "money", "overall", "render_list", "render_results",
           "render_run", "sparkline"]
