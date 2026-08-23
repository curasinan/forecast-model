"""GTM paper loop: model CDF vs injected quotes. No network."""

from __future__ import annotations

import numpy as np
import pandas as pd

from climate.emos import EmosModel, LeadParams
from climate.ladder import SOURCE_LABEL
from climate.paper import predictive_by_target_month, run_paper, write_paper_payload


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
