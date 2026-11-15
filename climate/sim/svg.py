"""Inline SVG charts for the simulation dashboards: no script or font from the network.

Marks follow one spec: 2px lines, end dots with a 2px surface ring, bars at
most 24px thick with a 4px rounded data end and a square baseline, solid
hairline grid. Colours are CSS custom properties (light and dark set in the
page), never raw hex in a mark; text uses the ink tokens, never a series
colour. Every chart carries a legend from two series on, a table view, and
the data its hover layer reads (crosshair on lines, one tooltip per bar or
cell). Series past the eighth are refused rather than given a ninth colour.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

MAX_SERIES = 8
W, H = 900, 300
M_LEFT, M_RIGHT, M_TOP, M_BOTTOM = 64, 110, 14, 34
END_LABEL_GAP = 13


@dataclass
class Series:
    name: str
    x: Sequence
    y: Sequence[float]


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """Round tick values covering [lo, hi], about `n` of them."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return [0.0]
    if hi - lo < 1e-12:
        lo, hi = lo - 1.0, hi + 1.0
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 1e-9:
        ticks.append(round(t, 10))
        t += step
    if ticks[0] > lo:
        ticks.insert(0, round(ticks[0] - step, 10))
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def fmt_num(v, digits: int = 2, signed: bool = False) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    return f"{v:+,.{digits}f}" if signed else f"{v:,.{digits}f}"


def _tick_label(v: float) -> str:
    if abs(v) >= 10_000:
        return f"{v / 1000:,.0f}K"
    if abs(v - round(v)) < 1e-9:
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _time_ticks(lo: pd.Timestamp, hi: pd.Timestamp) -> tuple[list[pd.Timestamp], str]:
    span = (hi - lo).days
    if span > 3 * 365:
        years = range(lo.year + (lo.month > 1 or lo.day > 1), hi.year + 1)
        step = max(1, math.ceil(len(years) / 7))
        return [pd.Timestamp(y, 1, 1, tz=lo.tz) for y in years][::step], "%Y"
    if span > 75:
        months = pd.date_range(lo.normalize().replace(day=1) + pd.offsets.MonthBegin(1), hi, freq="MS")
        step = max(1, math.ceil(len(months) / 7))
        return list(months[::step]), "%Y-%m"
    days = pd.date_range(lo.normalize() + pd.Timedelta(days=1), hi, freq="D")
    step = max(1, math.ceil(len(days) / 7))
    return list(days[::step]), "%m-%d"


def _table(headers: Sequence[str], rows: Sequence[Sequence], caption: str) -> str:
    head = "".join(f"<th scope='col'>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    return (f"<details class='table-view'><summary>Table: {esc(caption)}</summary>"
            f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
            f"</div></details>")


def _legend(names: Sequence[str], key: str = "line") -> str:
    if len(names) < 2:
        return ""
    items = "".join(f"<span class='key'><span class='swatch {key}' style='--c: var(--series-{i + 1})'>"
                    f"</span>{esc(n)}</span>" for i, n in enumerate(names))
    return f"<div class='legend'>{items}</div>"


def line_chart(series: Sequence[Series], *, title: str, y_label: str, x_kind: str = "time",
               x_label: str = "", digits: int = 2, note: str = "") -> str:
    """A line per series on one y axis, with a crosshair tooltip and a table view."""
    series = [s for s in series if len(s.x)]
    if not series:
        return f"<figure class='chart'><figcaption>{esc(title)}</figcaption><p class='empty'>No data yet.</p></figure>"
    if len(series) > MAX_SERIES:
        raise ValueError(f"{len(series)} series: at most {MAX_SERIES} share one chart")
    if x_kind == "time":
        xs_all = [pd.Timestamp(v) for s in series for v in s.x]
        lo_x, hi_x = min(xs_all), max(xs_all)
        to_num = (lambda v: pd.Timestamp(v).value)  # noqa: E731
        x_lo, x_hi = lo_x.value, hi_x.value
    else:
        xs_all = [float(v) for s in series for v in s.x]
        x_lo, x_hi = min(xs_all), max(xs_all)
        to_num = float
    if x_hi == x_lo:
        x_hi = x_lo + 1
    ys = [float(v) for s in series for v in s.y if v is not None and math.isfinite(float(v))]
    y_lo, y_hi = (min(ys + [0.0]), max(ys + [0.0])) if ys else (0.0, 1.0)
    yt = nice_ticks(y_lo, y_hi)
    y_lo, y_hi = yt[0], yt[-1]
    pw, ph = W - M_LEFT - M_RIGHT, H - M_TOP - M_BOTTOM

    def sx(v):
        return M_LEFT + (to_num(v) - x_lo) / (x_hi - x_lo) * pw

    def sy(v):
        return M_TOP + (y_hi - float(v)) / (y_hi - y_lo) * ph

    parts = [f"<svg viewBox='0 0 {W} {H}' width='{W}' role='img' aria-label='{esc(title)}'>"]
    for t in yt:
        cls = "baseline" if abs(t) < 1e-12 else "grid"
        parts.append(f"<line class='{cls}' x1='{M_LEFT}' x2='{M_LEFT + pw}' y1='{sy(t):.1f}' y2='{sy(t):.1f}'/>")
        parts.append(f"<text class='tick' x='{M_LEFT - 8}' y='{sy(t) + 4:.1f}' text-anchor='end'>{esc(_tick_label(t))}</text>")
    if x_kind == "time":
        ticks, fmt = _time_ticks(lo_x, hi_x)
        for t in ticks:
            parts.append(f"<text class='tick' x='{sx(t):.1f}' y='{H - 12}' text-anchor='middle'>{t:{fmt}}</text>")
    else:
        for t in nice_ticks(x_lo, x_hi, 6):
            if x_kind == "year" and abs(t - round(t)) > 1e-9:
                continue
            if x_lo - 1e-9 <= t <= x_hi + 1e-9:
                label = f"{int(round(t))}" if x_kind == "year" else _tick_label(t)
                parts.append(f"<text class='tick' x='{sx(t):.1f}' y='{H - 12}' text-anchor='middle'>{esc(label)}</text>")
    parts.append(f"<text class='axis-label' x='14' y='{M_TOP + ph / 2:.0f}' transform='rotate(-90 14 {M_TOP + ph / 2:.0f})' text-anchor='middle'>{esc(y_label)}</text>")
    ends = []
    for i, s in enumerate(series):
        segs, cur = [], []
        for x, y in zip(s.x, s.y):
            if y is None or not math.isfinite(float(y)):
                if cur:
                    segs.append(cur)
                cur = []
                continue
            cur.append(f"{sx(x):.1f},{sy(y):.1f}")
        if cur:
            segs.append(cur)
        for seg in segs:
            d = "M" + " L".join(seg)
            parts.append(f"<path class='line' d='{d}' style='--c: var(--series-{i + 1})'/>")
        finite = [(x, y) for x, y in zip(s.x, s.y) if y is not None and math.isfinite(float(y))]
        if finite:
            x, y = finite[-1]
            parts.append(f"<circle class='end' cx='{sx(x):.1f}' cy='{sy(y):.1f}' r='4' style='--c: var(--series-{i + 1})'/>")
            ends.append((sy(y), sx(x), s.name))
    if 1 < len(series) <= 4 and ends:
        ordered = sorted(e[0] for e in ends)
        if all(b - a >= END_LABEL_GAP for a, b in zip(ordered, ordered[1:])):
            for y, x, name in ends:
                parts.append(f"<text class='end-label' x='{x + 8:.1f}' y='{y + 4:.1f}'>{esc(name)}</text>")
    # the hover layer: crosshair and a transparent catch-all rectangle
    parts.append(f"<line class='crosshair' x1='0' x2='0' y1='{M_TOP}' y2='{M_TOP + ph}' visibility='hidden'/>")
    parts.append(f"<rect class='hit' x='{M_LEFT}' y='{M_TOP}' width='{pw}' height='{ph}'/>")
    parts.append("</svg>")

    # union of x positions for the crosshair and the table
    xkeys = sorted({to_num(v) for s in series for v in s.x})
    lookup = [{to_num(x): y for x, y in zip(s.x, s.y)} for s in series]

    def x_text(k):
        if x_kind == "time":
            ts = pd.Timestamp(k, tz="UTC")
            return ts.strftime("%Y-%m-%d") if ts.hour == 0 and ts.minute == 0 else ts.strftime("%Y-%m-%d %H:%M")
        return f"{int(round(k))}" if x_kind == "year" else _tick_label(k)

    data = {"xs": [round(M_LEFT + (k - x_lo) / (x_hi - x_lo) * pw, 1) for k in xkeys],
            "labels": [x_text(k) for k in xkeys],
            "series": [{"name": s.name, "slot": i + 1,
                        "values": [fmt_num(lookup[i].get(k), digits) for k in xkeys]}
                       for i, s in enumerate(series)]}
    rows = [[x_text(k)] + [fmt_num(lookup[i].get(k), digits) for i in range(len(series))] for k in xkeys]
    caption = f"<figcaption>{esc(title)}</figcaption>"
    sub = f"<p class='note'>{esc(note)}</p>" if note else ""
    return (f"<figure class='chart line-chart'>{caption}{sub}{_legend([s.name for s in series])}"
            f"<div class='plot'>{''.join(parts)}</div>"
            f"<script type='application/json' class='chart-data'>{_json(data)}</script>"
            f"{_table([x_label or 'x'] + [s.name for s in series], rows, title)}</figure>")


def bar_chart(categories: Sequence[str], values: Sequence[float], *, title: str, value_label: str,
              digits: int = 2, signed: bool = True, note: str = "") -> str:
    """One series of horizontal bars from a zero baseline (negative bars go left), slot-1 colour."""
    cats = [str(c) for c in categories]
    vals = [float(v) if v is not None and math.isfinite(float(v)) else float("nan") for v in values]
    if not cats:
        return f"<figure class='chart'><figcaption>{esc(title)}</figcaption><p class='empty'>No data yet.</p></figure>"
    finite = [v for v in vals if math.isfinite(v)] or [0.0]
    ticks = nice_ticks(min(finite + [0.0]), max(finite + [0.0]), 4)
    lo, hi = ticks[0], ticks[-1]
    label_w = min(190, 8 + 7 * max(len(c) for c in cats))
    left, right, row = label_w, 70, 26
    height = M_TOP + row * len(cats) + 30
    pw = W - left - right

    def sx(v):
        return left + (v - lo) / (hi - lo) * pw

    zero = sx(0.0)
    parts = [f"<svg viewBox='0 0 {W} {height}' width='{W}' role='img' aria-label='{esc(title)}'>"]
    for t in ticks:
        cls = "baseline" if abs(t) < 1e-12 else "grid"
        parts.append(f"<line class='{cls}' x1='{sx(t):.1f}' x2='{sx(t):.1f}' y1='{M_TOP - 4}' y2='{height - 26}'/>")
        parts.append(f"<text class='tick' x='{sx(t):.1f}' y='{height - 10}' text-anchor='middle'>{esc(_tick_label(t))}</text>")
    thick = 16
    for i, (c, v) in enumerate(zip(cats, vals)):
        cy = M_TOP + i * row + row / 2
        parts.append(f"<text class='cat' x='{left - 8}' y='{cy + 4:.1f}' text-anchor='end'>{esc(c)}</text>")
        tip = esc(f"{c}|{fmt_num(v, digits, signed)}|{value_label}")
        if not math.isfinite(v):
            parts.append(f"<text class='tick' x='{zero + 6:.1f}' y='{cy + 4:.1f}'>-</text>")
            continue
        end = sx(v)
        parts.append(f"<path class='bar' tabindex='0' data-tip='{tip}' d='{_bar_path(zero, end, cy, thick)}'/>")
        anchor, dx = ("start", 6) if v >= 0 else ("end", -6)
        parts.append(f"<text class='value' x='{end + dx:.1f}' y='{cy + 4:.1f}' text-anchor='{anchor}'>{esc(fmt_num(v, digits, signed))}</text>")
    parts.append("</svg>")
    rows = [[c, fmt_num(v, digits, signed)] for c, v in zip(cats, vals)]
    sub = f"<p class='note'>{esc(note)}</p>" if note else ""
    return (f"<figure class='chart bar-chart'><figcaption>{esc(title)}</figcaption>{sub}"
            f"<div class='plot'>{''.join(parts)}</div>{_table(['', value_label], rows, title)}</figure>")


def _bar_path(x0: float, x1: float, cy: float, thick: float, r: float = 4.0) -> str:
    """A bar from x0 (square baseline) to x1 (rounded data end)."""
    top, bot = cy - thick / 2, cy + thick / 2
    length = abs(x1 - x0)
    r = min(r, length / 2, thick / 2)
    if length < 0.5:
        return f"M{x0:.1f},{top:.1f} h0.5 v{thick:.1f} h-0.5 z"
    s = 1 if x1 >= x0 else -1
    return (f"M{x0:.1f},{top:.1f} L{x1 - s * r:.1f},{top:.1f} "
            f"Q{x1:.1f},{top:.1f} {x1:.1f},{top + r:.1f} L{x1:.1f},{bot - r:.1f} "
            f"Q{x1:.1f},{bot:.1f} {x1 - s * r:.1f},{bot:.1f} L{x0:.1f},{bot:.1f} Z")


DIVERGING_STEPS = 5


def heatmap(rows: Sequence[str], cols: Sequence[str], values, *, title: str, value_label: str,
            digits: int = 2, note: str = "") -> str:
    """Signed values on a blue (gain) to red (loss) scale with a gray midpoint; a tooltip per cell."""
    vals = np.asarray(values, dtype=float)
    if vals.size == 0:
        return f"<figure class='chart'><figcaption>{esc(title)}</figcaption><p class='empty'>No data yet.</p></figure>"
    top = float(np.nanmax(np.abs(vals))) if np.isfinite(vals).any() else 1.0
    top = top or 1.0
    label_w = min(170, 8 + 7 * max(len(r) for r in rows))
    cell_w = max(14.0, min(40.0, (W - label_w - 10) / max(len(cols), 1)))
    cell_h = 20.0
    width = label_w + cell_w * len(cols) + 10
    height = 26 + cell_h * len(rows) + 8
    parts = [f"<svg viewBox='0 0 {width:.0f} {height:.0f}' width='{width:.0f}' role='img' "
             f"aria-label='{esc(title)}'>"]
    every = max(1, math.ceil(len(cols) * 34 / max(cell_w * len(cols), 1)))
    for j, c in enumerate(cols):
        if j % every == 0:
            parts.append(f"<text class='tick' x='{label_w + j * cell_w + cell_w / 2:.1f}' y='16' text-anchor='middle'>{esc(c)}</text>")
    for i, r in enumerate(rows):
        y = 26 + i * cell_h
        parts.append(f"<text class='cat' x='{label_w - 8}' y='{y + cell_h / 2 + 4:.1f}' text-anchor='end'>{esc(r)}</text>")
        for j, c in enumerate(cols):
            v = vals[i, j]
            if math.isfinite(v):
                k = int(min(DIVERGING_STEPS, math.ceil(abs(v) / top * DIVERGING_STEPS)))
                var = "--div-0" if k == 0 else f"--div-{'p' if v > 0 else 'n'}{k}"
            else:
                var = "--surface-2"
            tip = esc(f"{r}, {c}|{fmt_num(v, digits, True)}|{value_label}")
            parts.append(f"<rect class='cell' tabindex='0' data-tip='{tip}' x='{label_w + j * cell_w + 1:.1f}' "
                         f"y='{y + 1:.1f}' width='{cell_w - 2:.1f}' height='{cell_h - 2:.1f}' rx='2' "
                         f"style='fill: var({var})'/>")
    parts.append("</svg>")
    scale = ("<div class='scale'><span>loss</span>"
             + "".join(f"<span class='step' style='background: var(--div-n{k})'></span>"
                       for k in range(DIVERGING_STEPS, 0, -1))
             + "<span class='step' style='background: var(--div-0)'></span>"
             + "".join(f"<span class='step' style='background: var(--div-p{k})'></span>"
                       for k in range(1, DIVERGING_STEPS + 1))
             + f"<span>gain</span><span class='muted'>(darkest = {fmt_num(top, digits)})</span></div>")
    table_rows = [[r] + [fmt_num(vals[i, j], digits, True) for j in range(len(cols))]
                  for i, r in enumerate(rows)]
    sub = f"<p class='note'>{esc(note)}</p>" if note else ""
    return (f"<figure class='chart heatmap'><figcaption>{esc(title)}</figcaption>{sub}{scale}"
            f"<div class='plot scroll'>{''.join(parts)}</div>{_table([''] + list(cols), table_rows, title)}</figure>")


def _json(obj) -> str:
    return json.dumps(obj).replace("</", "<\\/")


def diverging_css(mid: str, neg: str, pos: str, steps: int = DIVERGING_STEPS) -> str:
    """CSS custom properties --div-n{k}, --div-0, --div-p{k}, interpolated from the midpoint."""
    def rgb(h):
        h = h.lstrip("#")
        return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=float)

    def hexa(a):
        return "#" + "".join(f"{int(round(v)):02x}" for v in a)

    m, n, p = rgb(mid), rgb(neg), rgb(pos)
    out = [f"--div-0: {mid};"]
    for k in range(1, steps + 1):
        f = k / steps
        out.append(f"--div-n{k}: {hexa(m + (n - m) * f)};")
        out.append(f"--div-p{k}: {hexa(m + (p - m) * f)};")
    return " ".join(out)


__all__ = ["MAX_SERIES", "Series", "bar_chart", "diverging_css", "esc", "fmt_num", "heatmap",
           "line_chart", "nice_ticks"]
