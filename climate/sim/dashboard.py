"""One self-contained HTML page per simulation run: tiles, verdicts, charts and tables.

`write_dashboard(path)` renders a finished run directory, or the active entry
of a run still going (from the cells already finished, marked partial and
refreshing itself every ten seconds). The page holds its own CSS, SVG and a
small script for the hover layer; nothing is fetched from the network.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from climate.sim.progress import PROGRESS_FILE, read_progress
from climate.sim.svg import (
    MAX_SERIES,
    Series,
    bar_chart,
    diverging_css,
    esc,
    fmt_num,
    heatmap,
    line_chart,
)

TITLES = {"gtm_sim_replay": "S2 quote replay", "gtm_sim_hindcast": "S3 hindcast",
          "gtm_sim_score": "S1 model against market", "gtm_sim_book": "S4a book at settlement"}
SHORT = {"gtm_sim_replay": "Replay run", "gtm_sim_hindcast": "Hindcast run",
         "gtm_sim_score": "Score run", "gtm_sim_book": "Book run"}


@dataclass
class RunData:
    path: Path
    name: str
    kind: str
    config: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    state: dict | None = None
    grid: pd.DataFrame | None = None
    rows: pd.DataFrame | None = None
    cells: dict[str, dict[str, pd.DataFrame]] = field(default_factory=dict)
    partial: bool = False


def _json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _cells(base: Path) -> dict[str, dict[str, pd.DataFrame]]:
    out: dict[str, dict[str, pd.DataFrame]] = {}
    root = base / "cells"
    if root.is_dir():
        for cell in sorted(p for p in root.iterdir() if p.is_dir()):
            out[cell.name] = {f.stem: pd.read_parquet(f) for f in sorted(cell.glob("*.parquet"))}
    return out


def load_run(path: str | Path) -> RunData:
    """A finished run directory, or an active entry rendered from its finished cells."""
    path = Path(path)
    state = read_progress(path / PROGRESS_FILE)
    if (path / "config.json").exists():
        config = _json_file(path / "config.json")
        return RunData(
            path=path, name=path.name, kind=config.get("kind", "?"), config=config,
            metrics=_json_file(path / "metrics.json"), state=state,
            grid=pd.read_parquet(path / "grid.parquet") if (path / "grid.parquet").exists() else None,
            rows=pd.read_parquet(path / "rows.parquet") if (path / "rows.parquet").exists() else None,
            cells=_cells(path), partial=False)
    if state is None:
        raise FileNotFoundError(f"{path} holds neither a finished run nor a progress file")
    done = [dict(c.get("metrics") or {}, cell=c["name"]) for c in state.get("cells", [])
            if c.get("status") == "finished" and c.get("metrics")]
    grid = pd.DataFrame(done) if done else None
    return RunData(path=path, name=state.get("run", path.name), kind=state.get("kind", "?"),
                   state=state, grid=grid, cells=_cells(path), partial=True)


# -- page parts ----------------------------------------------------------------

def _tiles(items: list[tuple[str, str]]) -> str:
    cards = "".join(f"<div class='tile'><div class='tile-label'>{esc(label)}</div>"
                    f"<div class='tile-value'>{esc(value)}</div></div>" for label, value in items)
    return f"<section class='tiles'>{cards}</section>" if cards else ""


def _callout(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    items = "".join(f"<li>{esc(line)}</li>" for line in lines)
    return f"<section class='callout'><h2>{esc(title)}</h2><ul>{items}</ul></section>"


def _frame_table(frame: pd.DataFrame | None, columns: list[tuple[str, str, str]], title: str) -> str:
    """columns: (header, column, format) with format one of money, pct, num, int, text."""
    if frame is None or frame.empty:
        return ""
    cols = [(h, c, f) for h, c, f in columns if c in frame.columns]
    head = "".join(f"<th scope='col' class='{'num' if f != 'text' else ''}'>{esc(h)}</th>"
                   for h, _, f in cols)
    body = []
    for _, row in frame.iterrows():
        tds = []
        for _, col, fmt in cols:
            v = row[col]
            if fmt == "text":
                tds.append(f"<td>{esc(v)}</td>")
                continue
            if v is None or (isinstance(v, float) and not math.isfinite(v)):
                text = "-"
            elif fmt == "money":
                text = fmt_num(float(v), 2, signed=True)
            elif fmt == "pct":
                text = f"{float(v):.0%}"
            elif fmt == "int":
                text = f"{int(v):,}"
            else:
                text = fmt_num(float(v), 2)
            tds.append(f"<td class='num'>{esc(text)}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (f"<section class='table-section'><h2>{esc(title)}</h2><div class='scroll'><table>"
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div></section>")


def _best_worst(grid: pd.DataFrame | None, col: str) -> list[tuple[str, str]]:
    if grid is None or grid.empty or col not in grid.columns:
        return []
    ranked = grid.dropna(subset=[col]).sort_values(col)
    if ranked.empty:
        return []
    best, worst = ranked.iloc[-1], ranked.iloc[0]
    return [("Best cell", f"{best['cell']}  {best[col]:+,.2f}"),
            ("Worst cell", f"{worst['cell']}  {worst[col]:+,.2f}")]


def _series_from_cells(run: RunData, names: list[str], frame: str, x: str, y: str) -> list[Series]:
    out = []
    for name in names[:MAX_SERIES]:
        df = run.cells.get(name, {}).get(frame)
        if df is not None and len(df) and x in df.columns and y in df.columns:
            out.append(Series(name, list(df[x]), list(df[y].astype(float))))
    return out


# -- sections by kind ------------------------------------------------------------

def _replay(run: RunData) -> str:
    grid = run.grid
    parts = []
    tiles = [("Cells", _cells_done(run))] + _best_worst(grid, "total_pnl_bid")
    d1 = run.metrics.get("d1") or []
    if d1:
        tiles.append(("Limit rule (D1)", d1[-1].replace("D1 choice: ", "").rstrip(".")))
    parts.append(_tiles(tiles))
    parts.append(_callout("D1, the limit rule", d1))
    if grid is not None and len(grid):
        names = list(grid["cell"])
        ref = [n for n in names if n.endswith("_lat1_paper")] or names
        parts.append(line_chart(
            _series_from_cells(run, ref, "equity", "ts", "equity_bid"),
            title="Equity over the replay, marked to the bid", y_label="P&L ($)", x_label="decision",
            note=("Settled P&L plus open positions at the bid, by limit rule, paper fills, one "
                  "snapshot of latency." if ref is not names else "")
            + (f" {len(ref) - MAX_SERIES} more cells are in the table." if len(ref) > MAX_SERIES else "")))
        parts.append(bar_chart(names, grid["total_pnl_bid"], title="Total P&L by cell",
                               value_label="P&L at the bid ($)"))
        if "fill_rate" in grid.columns:
            parts.append(bar_chart(names, grid["fill_rate"] * 100, title="Orders filled",
                                   value_label="share filled (%)", digits=0, signed=False))
        if "median_hours_to_fill" in grid.columns:
            parts.append(bar_chart(names, grid["median_hours_to_fill"], title="Hours to the first fill",
                                   value_label="median hours", digits=1, signed=False))
        focus = next((n for n in names if n.startswith("ask_lat1_paper")), names[0])
        months = run.cells.get(focus, {}).get("months")
        if months is not None and len(months):
            value = months["pnl"].where(months["settled"], months["unrealized_bid"])
            parts.append(bar_chart(list(months["target_month"]), value,
                                   title=f"P&L by target month: {focus}",
                                   value_label="P&L ($), settled or at the bid",
                                   note="Months not yet printed are marked to the bid."))
        parts.append(_frame_table(grid, [
            ("cell", "cell", "text"), ("orders", "orders", "int"), ("filled", "orders_filled", "int"),
            ("fill rate", "fill_rate", "pct"), ("contracts", "contracts", "num"),
            ("deployed", "capital_deployed", "num"), ("h to fill", "median_hours_to_fill", "num"),
            ("paid over mid", "paid_over_mid", "num"), ("markout 1d", "markout_1d", "num"),
            ("markout 7d", "markout_7d", "num"), ("settled P&L", "settled_pnl", "money"),
            ("total P&L", "total_pnl_bid", "money"), ("ROI", "roi_bankroll", "pct"),
            ("max DD", "max_drawdown", "num")], "All cells"))
    return "".join(parts)


def _hindcast(run: RunData) -> str:
    grid = run.grid
    parts = []
    breakevens = run.metrics.get("breakeven") or {}
    parts.append(_tiles([("Cells", _cells_done(run))] + _best_worst(grid, "total_pnl")))
    parts.append(_callout("Break-even", [f"{k}: {v}" for k, v in breakevens.items()]))
    if grid is not None and len(grid) and "proxy" in grid.columns:
        proxies = [p for p in ("m0", "m1", "m2", "m3") if p in set(grid["proxy"])]
        blend = [Series(p, list(g.sort_values("weight")["weight"]), list(g.sort_values("weight")["total_pnl"]))
                 for p in proxies for g in [grid[grid["proxy"] == p]] if len(g) >= 2]
        if blend:
            parts.append(line_chart(blend, title="P&L against market quality: the blend toward the model",
                                    y_label="total P&L ($)", x_kind="number", x_label="blend weight w",
                                    note="w = 0 is the proxy alone; w = 1 would be the model itself."))
        inf = grid[grid["proxy"] == "informed"].sort_values("sd")
        if len(inf) >= 2:
            parts.append(line_chart([Series("informed market", list(inf["sd"]), list(inf["total_pnl"]))],
                                    title="P&L against an informed market's error",
                                    y_label="total P&L ($)", x_kind="number",
                                    x_label="market error sd (degC)", digits=2))
        yearly = {n: run.cells[n]["yearly"] for n in grid["cell"] if "yearly" in run.cells.get(n, {})}
        if yearly:
            years = sorted({int(y) for t in yearly.values() for y in t["year"]})
            names = list(yearly)
            matrix = np.array([[float(yearly[n].set_index("year")["pnl"].get(y, np.nan)) for y in years]
                               for n in names])
            parts.append(heatmap(names, [str(y) for y in years], matrix, title="P&L by year and cell",
                                 value_label="P&L ($)"))
            pure = [n for n in names if n.endswith("_w0")][:MAX_SERIES]
            if pure:
                cum = [Series(n, [float(y) for y in yearly[n]["year"]],
                              list(yearly[n]["pnl"].cumsum())) for n in pure]
                parts.append(line_chart(cum, title="Cumulative P&L against each proxy alone",
                                        y_label="cumulative P&L ($)", x_kind="year", x_label="year"))
        parts.append(bar_chart(list(grid["cell"]), grid["total_pnl"], title="Total P&L by cell",
                               value_label="P&L ($)"))
        parts.append(_frame_table(grid, [
            ("cell", "cell", "text"), ("trades", "orders_filled", "int"), ("contracts", "contracts", "num"),
            ("deployed", "capital_deployed", "num"), ("total P&L", "total_pnl", "money"),
            ("per year", "mean_year", "money"), ("sd per year", "sd_year", "num"), ("t", "t_stat", "num"),
            ("years up", "share_years_up", "pct"), ("hit rate", "hit_rate", "pct"),
            ("first period", "pnl_before", "money"), ("second period", "pnl_from", "money"),
            ("max DD", "max_drawdown", "num")], "All cells"))
    return "".join(parts)


def _score(run: RunData) -> str:
    rows = run.rows
    m = run.metrics
    parts = [_tiles([("Snapshots x months", str(m.get("n_rows", "-"))),
                     ("Market fits identified", str(m.get("n_identified", "-"))),
                     ("Scored against a print", str(m.get("n_scored", "-")))])]
    if rows is not None and len(rows):
        ident = rows[rows["identified"].astype(bool)]
        if len(ident):
            labels = [f"{pd.Timestamp(s):%Y-%m-%d} {t}" for s, t in zip(ident["snapshot_utc"], ident["target_month"])]
            parts.append(bar_chart(labels, ident["mu_gap"], title="Model mean minus market mean",
                                   value_label="mu gap (degC)", digits=3,
                                   note="Identified market fits only; positive means the model is warmer."))
        parts.append(_frame_table(rows, [
            ("snapshot", "snapshot_utc", "text"), ("target", "target_month", "text"),
            ("lead", "lead_months", "int"), ("model mu", "model_mu", "num"),
            ("model sd", "model_sigma", "num"), ("market mu", "market_mu", "num"),
            ("market sd", "market_sigma", "num"), ("ceiling", "market_ceiling", "num"),
            ("gap", "mu_gap", "num"), ("label", "label", "num"), ("CRPS model", "crps_model", "num"),
            ("CRPS market", "crps_market", "num")], "Model against market"))
    return "".join(parts)


def _book(run: RunData) -> str:
    rows = run.rows
    parts = [_tiles([("Target", str(run.config.get("target", "-"))),
                     ("Books", str(len(run.config.get("books", {})) or "-"))])]
    if rows is not None and len(rows):
        for book in list(dict.fromkeys(rows["book"]))[:4]:
            sub = rows[rows["book"] == book]
            parts.append(bar_chart(list(sub["scenario"]), sub["expected_pnl"],
                                   title=f"Expected P&L at settlement: {book}", value_label="E[P&L] ($)"))
        parts.append(_frame_table(rows, [
            ("book", "book", "text"), ("scenario", "scenario", "text"), ("mu", "mu", "num"),
            ("sd", "sigma", "num"), ("cost", "cost", "num"), ("E[P&L]", "expected_pnl", "money"),
            ("E[ROI]", "expected_roi", "pct"), ("P(loss)", "p_loss", "pct"),
            ("P(lose all)", "p_total_loss", "pct"), ("5% P&L", "pnl_q05", "money")], "Every scenario"))
    return "".join(parts)


def _cells_done(run: RunData) -> str:
    cells = (run.state or {}).get("cells") or []
    if not cells:
        return str(len(run.grid)) if run.grid is not None else "-"
    done = sum(1 for c in cells if c.get("status") == "finished")
    return f"{done} of {len(cells)}"


SECTIONS = {"gtm_sim_replay": _replay, "gtm_sim_hindcast": _hindcast, "gtm_sim_score": _score,
            "gtm_sim_book": _book}


# -- the page --------------------------------------------------------------------

CSS = """
.viz-root { color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --surface-2: #f0efec; --text-primary: #0b0b0b;
  --text-secondary: #52514e; --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7;
  --border: rgba(11,11,11,0.10); --critical: #d03b3b;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
  --series-5: #e87ba4; --series-6: #008300; --series-7: #6250d6; --series-8: #e34948;
  %(div_light)s }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) .viz-root { color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --surface-2: #262624; --text-primary: #f0efec;
  --text-secondary: #c3c2b7; --muted: #898781; --grid: #2c2c2a; --axis: #383835;
  --border: rgba(255,255,255,0.10); --critical: #d03b3b;
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
  --series-5: #d55181; --series-6: #008300; --series-7: #9085e9; --series-8: #e66767;
  %(div_dark)s } }
:root[data-theme="dark"] .viz-root { color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --surface-2: #262624; --text-primary: #f0efec;
  --text-secondary: #c3c2b7; --muted: #898781; --grid: #2c2c2a; --axis: #383835;
  --border: rgba(255,255,255,0.10); --critical: #d03b3b;
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
  --series-5: #d55181; --series-6: #008300; --series-7: #9085e9; --series-8: #e66767;
  %(div_dark)s }
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: #f9f9f7; }
@media (prefers-color-scheme: dark) { body { background: #0d0d0d; } }
.viz-root { background: var(--page); color: var(--text-primary); min-height: 100vh;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 24px 16px 48px; }
.wrap { max-width: 1040px; margin: 0 auto; }
header h1 { font-size: 22px; margin: 0 0 4px; font-weight: 600; }
.meta { color: var(--text-secondary); margin: 0; font-size: 13px; }
.banner { margin: 12px 0 0; padding: 8px 12px; border-radius: 8px; background: var(--surface-2);
  color: var(--text-secondary); font-size: 13px; }
.banner.failed { border-left: 3px solid var(--critical); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile-label { color: var(--text-secondary); font-size: 13px; }
.tile-value { font-size: 20px; font-weight: 600; margin-top: 2px; overflow-wrap: anywhere; }
.callout, .chart, .table-section { background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; margin: 16px 0; }
.callout h2, .table-section h2 { font-size: 15px; margin: 0 0 6px; }
.callout ul { margin: 0; padding-left: 18px; color: var(--text-secondary); }
figure.chart { margin: 16px 0; }
figcaption { font-weight: 600; margin-bottom: 4px; }
.note { color: var(--text-secondary); font-size: 13px; margin: 0 0 8px; }
.empty { color: var(--muted); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13px; color: var(--text-secondary); margin: 4px 0 6px; }
.key { display: inline-flex; align-items: center; gap: 6px; }
.swatch.line { width: 14px; height: 2px; border-radius: 1px; background: var(--c); display: inline-block; }
.swatch.rect { width: 10px; height: 10px; border-radius: 2px; background: var(--c); display: inline-block; }
.plot svg { max-width: 100%%; height: auto; display: block; overflow: visible; }
.scroll { overflow-x: auto; }
svg .grid { stroke: var(--grid); stroke-width: 1; }
svg .baseline { stroke: var(--axis); stroke-width: 1; }
svg .tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .axis-label { fill: var(--muted); font-size: 11px; }
svg .cat { fill: var(--text-secondary); font-size: 12px; }
svg .value { fill: var(--text-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .end-label { fill: var(--text-secondary); font-size: 12px; }
svg .line { fill: none; stroke: var(--c); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
svg .end { fill: var(--c); stroke: var(--surface-1); stroke-width: 2; }
svg .bar { fill: var(--series-1); }
svg .bar:hover, svg .bar:focus { opacity: 0.8; outline: none; }
svg .cell { stroke: none; }
svg .cell:hover, svg .cell:focus { stroke: var(--text-primary); stroke-width: 1.5; outline: none; }
svg .crosshair { stroke: var(--text-secondary); stroke-width: 1; pointer-events: none; }
svg .hit { fill: transparent; cursor: crosshair; }
svg .hit:focus { outline: 1px solid var(--axis); }
.scale { display: flex; align-items: center; gap: 2px; font-size: 12px; color: var(--text-secondary); margin: 0 0 6px; }
.scale .step { width: 18px; height: 10px; border-radius: 2px; display: inline-block; }
.scale span:first-child { margin-right: 6px; } .scale .muted { color: var(--muted); margin-left: 8px; }
table { border-collapse: collapse; width: 100%%; font-size: 12px; }
th, td { padding: 4px 6px; border-bottom: 1px solid var(--grid); text-align: left; white-space: nowrap; }
th { color: var(--text-secondary); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
details.table-view { margin-top: 8px; font-size: 13px; }
details.table-view summary { cursor: pointer; color: var(--text-secondary); }
.tooltip { position: fixed; z-index: 10; pointer-events: none; background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); max-width: 280px; }
.tooltip .t-title { color: var(--text-secondary); margin-bottom: 4px; }
.tooltip .t-row { display: flex; align-items: center; gap: 8px; }
.tooltip .t-val { font-weight: 600; color: var(--text-primary); font-variant-numeric: tabular-nums; }
.tooltip .t-name { color: var(--text-secondary); }
.tooltip .t-key { width: 12px; height: 2px; border-radius: 1px; display: inline-block; }
footer { color: var(--muted); font-size: 12px; margin-top: 24px; }
footer, .meta, .callout li, .note, .banner, figcaption { overflow-wrap: anywhere; }
@media (max-width: 600px) { .tile-value { font-size: 17px; } }
"""

JS = """
(() => {
  const root = document.querySelector('.viz-root');
  const tip = document.createElement('div');
  tip.className = 'tooltip'; tip.hidden = true; root.appendChild(tip);
  const place = (x, y) => {
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, x + 14)) + 'px';
    tip.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, y + 14)) + 'px';
  };
  const show = (title, rows, x, y) => {
    tip.replaceChildren();
    const t = document.createElement('div'); t.className = 't-title'; t.textContent = title; tip.appendChild(t);
    for (const r of rows) {
      const row = document.createElement('div'); row.className = 't-row';
      if (r.slot) { const k = document.createElement('span'); k.className = 't-key';
        k.style.background = 'var(--series-' + r.slot + ')'; row.appendChild(k); }
      const v = document.createElement('span'); v.className = 't-val'; v.textContent = r.value; row.appendChild(v);
      const n = document.createElement('span'); n.className = 't-name'; n.textContent = r.name; row.appendChild(n);
      tip.appendChild(row);
    }
    tip.hidden = false; place(x, y);
  };
  const hide = () => { tip.hidden = true; };
  document.querySelectorAll('figure.line-chart').forEach((fig) => {
    const data = JSON.parse(fig.querySelector('script.chart-data').textContent);
    const svg = fig.querySelector('svg'), hit = svg.querySelector('rect.hit'), cross = svg.querySelector('line.crosshair');
    if (!hit || !data.xs.length) return;
    let current = data.xs.length - 1;
    hit.setAttribute('tabindex', '0');
    const nearest = (evt) => {
      const pt = svg.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      let best = 0, d = Infinity;
      data.xs.forEach((x, i) => { const dd = Math.abs(x - p.x); if (dd < d) { d = dd; best = i; } });
      return best;
    };
    const at = (i, x, y) => {
      current = i; const sx = data.xs[i];
      cross.setAttribute('x1', sx); cross.setAttribute('x2', sx); cross.setAttribute('visibility', 'visible');
      show(data.labels[i], data.series.map((s) => ({ value: s.values[i], name: s.name, slot: s.slot })), x, y);
    };
    hit.addEventListener('pointermove', (e) => at(nearest(e), e.clientX, e.clientY));
    hit.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); hide(); });
    const fromBox = () => { const b = hit.getBoundingClientRect(); return [b.left + b.width / 2, b.top]; };
    hit.addEventListener('focus', () => at(current, ...fromBox()));
    hit.addEventListener('blur', () => { cross.setAttribute('visibility', 'hidden'); hide(); });
    hit.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        e.preventDefault();
        const i = Math.max(0, Math.min(data.xs.length - 1, current + (e.key === 'ArrowRight' ? 1 : -1)));
        at(i, ...fromBox());
      }
    });
  });
  document.querySelectorAll('[data-tip]').forEach((el) => {
    const parts = el.getAttribute('data-tip').split('|');
    const open = (x, y) => show(parts[0], [{ value: parts[1], name: parts[2] || '' }], x, y);
    el.addEventListener('pointerenter', (e) => open(e.clientX, e.clientY));
    el.addEventListener('pointermove', (e) => place(e.clientX, e.clientY));
    el.addEventListener('pointerleave', hide);
    el.addEventListener('focus', () => { const b = el.getBoundingClientRect(); open(b.right, b.top); });
    el.addEventListener('blur', hide);
  });
})();
"""


def render(run: RunData, *, generated: datetime | None = None) -> str:
    """The whole page for one run."""
    generated = generated or datetime.now(timezone.utc)
    state = run.state or {}
    title = SHORT.get(run.kind, "Simulation run")
    meta = [TITLES.get(run.kind, run.kind), run.name]
    if state.get("started_utc"):
        meta.append(f"started {state['started_utc'].replace('T', ' ').replace('Z', ' UTC')}")
    commit = state.get("git_commit") or run.config.get("git_commit")
    if commit:
        meta.append(f"commit {commit}")
    banner = ""
    if run.partial:
        status = state.get("status", "running")
        cls = "banner failed" if status == "failed" else "banner"
        what = (f"Run {status}: {state.get('error')}" if status == "failed" else
                f"Partial: {_cells_done(run)} cells finished, phase {state.get('phase') or 'running'}. "
                "This page refreshes itself every ten seconds.")
        banner = f"<p class='{cls}'>{esc(what)}</p>"
    elif state.get("status") == "failed":
        banner = f"<p class='banner failed'>{esc('Run failed: ' + str(state.get('error')))}</p>"
    summary = run.metrics.get("summary") or {}
    summary_html = _callout("The run", [f"{k}: {v}" for k, v in summary.items()])
    body = SECTIONS.get(run.kind, lambda r: _frame_table(r.grid, [("cell", "cell", "text")], "Cells"))(run)
    css = CSS % {"div_light": diverging_css("#f0efec", "#e34948", "#256abf"),
                 "div_dark": diverging_css("#383835", "#e66767", "#3987e5")}
    refresh = "<meta http-equiv='refresh' content='10'>" if run.partial and \
        state.get("status") == "running" else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"{refresh}<title>{esc(title)}</title><style>{css}</style></head><body>"
        f"<main class='viz-root'><div class='wrap'><header><h1>{esc(TITLES.get(run.kind, title))}</h1>"
        f"<p class='meta'>{esc('  |  '.join(meta))}</p>{banner}</header>{body}{summary_html}"
        f"<footer>Generated {generated:%Y-%m-%d %H:%M} UTC from {esc(run.path)}. "
        "Every chart has its table under it.</footer></div></main>"
        f"<script>{JS}</script></body></html>"
    )


def write_dashboard(path: str | Path, out: str | Path | None = None) -> Path:
    """Render the run at `path` into `out` (default: dashboard.html beside its files)."""
    run = load_run(path)
    target = Path(out) if out is not None else Path(path) / "dashboard.html"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(render(run), encoding="utf-8")
    os.replace(tmp, target)
    return target


__all__ = ["RunData", "load_run", "render", "write_dashboard"]
