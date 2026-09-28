"""GTM paper loop: model CDF vs injected quotes. No network."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from climate.emos import EmosModel, LeadParams
from climate.ladder import SOURCE_LABEL
from climate.paper import (
    HISTORY_REL,
    append_paper_history,
    predictive_by_target_month,
    run_paper,
    write_paper_payload,
)


def _frame() -> pd.DataFrame:
    # Two target months, two leads; lead 1 should win the per-month pick.
    return pd.DataFrame(
        {
            "target_month": ["2026-08", "2026-08", "2026-09", "2026-09"],
            "lead_month": [1, 2, 1, 2],
            "y_C": [np.nan, np.nan, np.nan, np.nan],
            "fc_mean_C": [1.10, 1.00, 1.20, 1.05],
            "fc_sd_C": [0.12, 0.18, 0.12, 0.18],
            "target_year_frac": [2026.625, 2026.625, 2026.708, 2026.708],
            "month_of_year": [8, 8, 9, 9],
        }
    )


def _identity_model() -> EmosModel:
    model = EmosModel(trend=False)
    for lead in (1, 2):
        model.params[lead] = LeadParams(
            lead_month=lead, a=0.0, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1
        )
    return model


def test_predictive_by_target_month_keeps_shortest_lead():
    pred = predictive_by_target_month(_frame(), _identity_model())
    assert set(pred) == {"2026-08", "2026-09"}
    assert pred["2026-08"][0] == 1.10
    assert pred["2026-09"][0] == 1.20


def test_run_paper_flags_tradeable_and_writes(tmp_path):
    quotes = pd.DataFrame(
        {
            "con_id": [1],
            "target_month": ["2026-08"],
            "strike": [0.6],
            "bid": [0.40],
            "ask": [0.44],
            "yes_mid": [0.42],
            "no_mid": [0.56],
            "spread": [0.04],
            "suspect_quote": [False],
            "target_month_source": [SOURCE_LABEL],
        }
    )
    payload = run_paper(_frame(), _identity_model(), quotes_df=quotes)
    assert payload["n_ladder"] == 1
    assert payload["n_tradeable"] == 1
    assert bool(payload["signals"][0]["tradeable"]) is True
    path = write_paper_payload(payload, root=tmp_path)
    assert path.exists() and "gtm_signals" in path.name


# --------------------------------------------------------------------------- #
# L8 (Task 3.8): run-by-run history alongside the overwritten latest file
# --------------------------------------------------------------------------- #


def test_append_paper_history_writes_one_json_line_per_call(tmp_path):
    p1 = append_paper_history({"ts_utc": "2026-01-01T00:00:00Z", "run": 1}, root=tmp_path)
    p2 = append_paper_history({"ts_utc": "2026-01-01T01:00:00Z", "run": 2}, root=tmp_path)

    assert p1 == p2 == tmp_path.joinpath(*HISTORY_REL)
    lines = p1.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"ts_utc": "2026-01-01T00:00:00Z", "run": 1}
    assert json.loads(lines[1]) == {"ts_utc": "2026-01-01T01:00:00Z", "run": 2}


def test_append_paper_history_writes_each_line_in_one_write_call(tmp_path, monkeypatch):
    """Final-review bundle B-4: the line and its newline used to go out in
    TWO write() calls, so a crash between them left a line with no newline
    that the NEXT run's line would be glued onto (two records corrupted).
    One write() of the full "json + newline" string closes that window."""
    import builtins

    import climate.paper as paper_mod

    writes: list[str] = []
    real_open = builtins.open

    class _RecordingFile:
        def __init__(self, fh):
            self._fh = fh

        def write(self, text):
            writes.append(text)
            return self._fh.write(text)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    monkeypatch.setattr(paper_mod, "open",
                        lambda *a, **k: _RecordingFile(real_open(*a, **k)), raising=False)

    path = append_paper_history({"ts_utc": "2026-01-01T00:00:00Z", "run": 1}, root=tmp_path)

    assert len(writes) == 1
    assert writes[0].endswith("\n") and writes[0].count("\n") == 1
    assert json.loads(writes[0]) == {"ts_utc": "2026-01-01T00:00:00Z", "run": 1}
    assert path.read_text(encoding="utf-8") == writes[0]


def test_append_paper_history_creates_the_paper_dir(tmp_path):
    # No prior write_paper_payload call in this test -- the history append
    # must create paper/ itself rather than assume it already exists.
    path = append_paper_history({"ts_utc": "2026-01-01T00:00:00Z"}, root=tmp_path)
    assert path.exists()
    assert path.parent.name == "paper"
