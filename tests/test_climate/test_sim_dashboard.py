"""The HTML dashboards: chart rules, every run kind, partial runs, and the rewrite script."""

from __future__ import annotations

import html
import json
import re

import pandas as pd
import pytest

from climate.sim.dashboard import load_run, render, write_dashboard
from climate.sim.progress import ACTIVE, ProgressWriter
from climate.sim.svg import Series, bar_chart, heatmap, line_chart, nice_ticks
from scripts.research import (
    gtm_sim_book,
    gtm_sim_dashboard,
    gtm_sim_hindcast,
    gtm_sim_replay,
    gtm_sim_score,
)
from tests.test_climate.sim_fixtures import ladder_quotes, payload
from tests.test_climate.sim_replay_fixtures import (
    DEC_EXPIRY,
    DEC_QUOTES,
    StubForecaster,
    times,
    utc,
    write_ladder,
)


def _no_network(page: str) -> None:
    assert not re.search(r"""(src|href)\s*=\s*['"]?https?:""", page)
    assert "<link" not in page and "<script src" not in page


# -- the chart rules -------------------------------------------------------------

def test_nice_ticks_cover_the_range():
    ticks = nice_ticks(-3.2, 17.0)
    assert ticks[0] <= -3.2 and ticks[-1] >= 17.0
    assert len({round(b - a, 9) for a, b in zip(ticks, ticks[1:])}) == 1


def test_a_ninth_series_is_refused():
    with pytest.raises(ValueError, match="at most 8"):
        line_chart([Series(f"s{i}", [1, 2], [i, i]) for i in range(9)], title="t", y_label="y",
                   x_kind="number")


def test_one_series_has_no_legend_two_have_one():
    one = line_chart([Series("only", [1, 2, 3], [1, 2, 3])], title="t", y_label="y", x_kind="number")
    two = line_chart([Series("a", [1, 2], [1, 2]), Series("b", [1, 2], [5, 9])], title="t",
                     y_label="y", x_kind="number")
    assert "class='legend'" not in one and "class='legend'" in two
    data = json.loads(re.search(r"<script type='application/json' class='chart-data'>(.*?)</script>",
                                two).group(1))
    assert len(data["xs"]) == 2 and [s["name"] for s in data["series"]] == ["a", "b"]
    assert "class='end-label'" in two  # far apart at the right edge


def test_colliding_end_labels_fall_back_to_the_legend():
    close = line_chart([Series("a", [1, 2], [1.0, 5.0]), Series("b", [1, 2], [1.0, 5.01])],
                       title="t", y_label="y", x_kind="number")
    assert "class='end-label'" not in close and "class='legend'" in close


def test_names_from_data_are_escaped():
    page = bar_chart(["<script>alert(1)</script>"], [1.0], title="t", value_label="v")
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_bars_grow_from_the_baseline_with_a_rounded_end():
    page = bar_chart(["up", "down"], [2.0, -1.0], title="t", value_label="v")
    paths = re.findall(r"class='bar'[^>]* d='([^']+)'", page)
    assert len(paths) == 2 and all("Q" in p for p in paths)
    starts = {p.split()[0].split(",")[0] for p in paths}
    assert len(starts) == 1  # both start at the zero line


def test_heatmap_cells_take_the_diverging_scale_and_a_tooltip():
    page = heatmap(["a", "b"], ["2001", "2002"], [[5.0, -5.0], [0.0, float("nan")]], title="t",
                   value_label="P&L")
    assert "var(--div-p5)" in page and "var(--div-n5)" in page and "var(--div-0)" in page
    assert page.count("data-tip=") == 4 and "var(--surface-2)" in page


def test_year_axes_carry_no_thousands_separator():
    page = line_chart([Series("a", [2001.0, 2010.0], [1.0, 2.0])], title="t", y_label="y",
                      x_kind="year")
    assert ">2004<" in page or ">2002<" in page
    assert "2,0" not in page


# -- the run kinds ---------------------------------------------------------------

def test_a_hindcast_page(tmp_path, synthetic_root):
    out = tmp_path / "run"
    assert gtm_sim_hindcast.main(["--root", str(synthetic_root), "--first-year", "2006",
                                  "--last-year", "2007", "--proxies", "m0,m1", "--blends", "0,0.5",
                                  "--informed-sd", "0.05,0.3", "--out", str(out)]) == 0
    page = (out / "dashboard.html").read_text()
    for part in ("Break-even", "P&L against market quality", "P&L against an informed market",
                 "P&L by year and cell", "Cumulative P&L against each proxy alone", "All cells",
                 "Table:"):
        assert html.escape(part) in page
    _no_network(page)
    grid = pd.read_parquet(out / "grid.parquet")
    best = grid.sort_values("total_pnl").iloc[-1]
    assert f"{best['cell']}  {best['total_pnl']:+,.2f}" in page


def test_a_replay_page(tmp_path, monkeypatch):
    ticks = tmp_path / "ticks"
    write_ladder(ticks, times(utc(2026, 10, 14, 13, 0), utc(2026, 10, 15, 16, 0)),
                 lambda ts: DEC_QUOTES, DEC_EXPIRY)
    monkeypatch.setattr(gtm_sim_replay, "build_forecaster",
                        lambda *a, **k: StubForecaster({"2026-12": (1.425, 0.115)}))
    out = tmp_path / "run"
    assert gtm_sim_replay.main(["--root", str(tmp_path / "climate"), "--ticks-root", str(ticks),
                                "--universe-json", str(tmp_path / "none.json"), "--out", str(out),
                                "--policies", "ask,mid", "--fill", "paper"]) == 0
    page = (out / "dashboard.html").read_text()
    for part in ("D1, the limit rule", "Equity over the replay", "Total P&L by cell",
                 "Hours to the first fill", "P&L by target month", "All cells"):
        assert html.escape(part) in page
    _no_network(page)


def _payload_file(tmp_path):
    dec = ladder_quotes([round(-0.2 + 0.1 * i, 1) for i in range(20)], 1.30, 0.18, ceiling=0.93)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(payload("2026-10-16T13:31:46Z", {"2026-12": dec},
                                       {"2026-12": (1.43, 0.12)})))
    return path


def test_score_and_book_pages(tmp_path):
    path = _payload_file(tmp_path)
    assert gtm_sim_score.main(["--payload", str(path), "--no-labels", "--out", str(tmp_path / "s")]) == 0
    score = (tmp_path / "s" / "dashboard.html").read_text()
    assert "Model mean minus market mean" in score and "Model against market" in score
    assert gtm_sim_book.main(["--payload", str(path), "--target", "2026-12",
                              "--book", "cap15=1.3:YES:22:0.44,1.2:YES:8:0.65",
                              "--out", str(tmp_path / "b")]) == 0
    book = (tmp_path / "b" / "dashboard.html").read_text()
    assert "Expected P&amp;L at settlement: cap15" in book and "Every scenario" in book
    _no_network(score)
    _no_network(book)


# -- partial and failed runs --------------------------------------------------------

def _active(tmp_path):
    w = ProgressWriter(tmp_path / "runs" / "gtm_sim_hindcast_20261113T101500Z", "gtm_sim_hindcast",
                       ["m0_w0", "m0_w0.5"], 24, start_thread=False)
    w.cell_started("m0_w0")
    yearly = pd.DataFrame({"year": [2006, 2007], "pnl": [3.0, -1.0], "orders_filled": [4, 2],
                           "contracts": [40.0, 20.0], "cost": [20.0, 10.0], "hit_rate": [0.5, 0.5]})
    equity = pd.DataFrame({"ts": [pd.Timestamp("2006-01-14", tz="UTC"), pd.Timestamp("2007-12-14", tz="UTC")],
                           "equity_bid": [0.0, 2.0]})
    w.cell_finished("m0_w0", {"cell": "m0_w0", "proxy": "m0", "weight": 0.0, "sd": 0.0,
                              "total_pnl": 2.0, "orders_filled": 6},
                    frames={"yearly": yearly, "equity": equity})
    return w


def test_a_running_run_renders_partial_and_refreshes(tmp_path):
    w = _active(tmp_path)
    run = load_run(w.dir)
    assert run.partial and list(run.grid["cell"]) == ["m0_w0"]
    page = render(run)
    assert "Partial: 1 of 2 cells finished" in page and "http-equiv='refresh'" in page
    assert "P&amp;L by year and cell" in page
    w.failed("RuntimeError: boom")
    failed = render(load_run(w.dir))
    assert "Run failed: RuntimeError: boom" in failed and "http-equiv='refresh'" not in failed


def test_the_rewrite_script(tmp_path, capsys):
    w = _active(tmp_path)
    root = tmp_path / "runs"
    assert gtm_sim_dashboard.main(["latest", "--runs-root", str(root)]) == 0
    assert (root / ACTIVE / w.run_dir.name / "dashboard.html").exists()
    out = tmp_path / "copy.html"
    assert gtm_sim_dashboard.main([w.run_dir.name, "--runs-root", str(root), "--out", str(out)]) == 0
    assert out.exists() and "S3 hindcast" in out.read_text()
    assert gtm_sim_dashboard.main(["nothing", "--runs-root", str(root)]) == 2


def test_write_dashboard_refuses_an_empty_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_dashboard(tmp_path)
