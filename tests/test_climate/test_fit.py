"""GTM fit + diagnostics report. Synthetic data only."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from climate.bridge import Bridge
from climate.dataset import build_frame_from_inputs, frame_bridge
from climate.fit import default_test_window, fit_gtm, render_markdown, write_artifacts
from climate.paper_criteria import evaluate_hindcast_go_live


def _months(start="2000-01", end="2015-12") -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="MS")


def _synthetic_inputs(n_members: int = 4, seed: int = 0):
    rng = np.random.default_rng(seed)
    idx = _months()
    t = (idx.year - 2000) + (idx.month - 0.5) / 12.0
    era5 = pd.Series(
        287.0 + 1.5 * np.cos(2 * np.pi * (idx.month - 7) / 12.0) + 0.02 * t + rng.normal(0, 0.05, len(idx)),
        index=idx,
        name="t2m_global_K",
    )
    alpha = {m: -258.8 + 0.4 * np.sin(2 * np.pi * (m - 1) / 12.0) for m in range(1, 13)}
    noaa = pd.Series(
        [alpha[ts.month] + 0.95 * era5.loc[ts] + rng.normal(0, 0.03) for ts in idx],
        index=idx.strftime("%Y-%m"),
        name="y_C",
    )
    rows = []
    inits = pd.date_range("2000-01-01", "2015-07-01", freq="MS")
    for init in inits:
        for lead in range(1, 4):
            tm = pd.Period(init, "M") + (lead - 1)
            base = float(era5.loc[tm.to_timestamp()]) if tm.to_timestamp() in era5.index else 287.0
            for mem in range(n_members):
                rows.append((pd.Timestamp(init), lead, mem, base + rng.normal(0, 0.08)))
    seas5 = pd.DataFrame(rows, columns=["init_date", "lead_month", "member", "t2m_global_K"])
    return seas5, era5, noaa


def test_fit_gtm_writes_artifacts_and_scores(tmp_path):
    seas5, era5, noaa = _synthetic_inputs()
    frame = build_frame_from_inputs(seas5, era5, noaa)
    assert isinstance(frame_bridge(frame), Bridge)
    result = fit_gtm(frame, trend=False, leads=range(1, 4), min_train_targets=12)
    assert result.n_labelled > 100
    assert set(result.model.params) == {1, 2, 3}
    assert not result.summary.empty
    assert list(result.summary["lead_month"]) == [1, 2, 3]
    md = render_markdown(result)
    assert "GTM EMOS diagnostics" in md
    assert "skill vs trend" in md
    paths = write_artifacts(result, root=tmp_path)
    assert paths["model"].exists() and paths["report_md"].exists() and paths["report_json"].exists()
    payload = json.loads(paths["report_json"].read_text(encoding="utf-8"))
    assert payload["n_labelled"] == result.n_labelled
    assert "bridge" in payload and "emos" in payload
    # Hindcast gate is defined on this summary (may pass or fail; must be structured).
    verdict = evaluate_hindcast_go_live(result.summary, require_live_paper=False)
    assert isinstance(verdict.passed, bool)
    assert "leads" in verdict.metrics


def test_default_test_window_leaves_train_years():
    frame = pd.DataFrame(
        {
            "y_C": [1.0] * 12 * 10,
            "target_year_frac": np.repeat(np.arange(2000, 2010) + 0.5, 12),
            "lead_month": 1,
            "month_of_year": list(range(1, 13)) * 10,
            "fc_mean_C": 1.0,
            "fc_sd_C": 0.1,
        }
    )
    lo, hi = default_test_window(frame, min_train_years=8)
    assert (lo, hi) == (2008, 2009)
