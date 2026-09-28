"""Task 3.1 (audit finding M2a): fingerprint the fitted model through the
chain. A paper-trading measurement is only interpretable if every order
record can be traced to the exact fitted model that produced it, so the
short sha256 fingerprint computed here has to survive a save/load
round-trip unchanged, move when any coefficient moves, and show up in
both the on-disk model artifact and the signals payload
``climate.paper.run_paper`` builds for ``scripts/run_ibkr_paper.py``.
"""

from __future__ import annotations

import json

import pandas as pd

from climate.dataset import build_frame_from_inputs
from climate.emos import EmosModel, LeadParams
from climate.fit import fit_gtm, model_fingerprint, write_artifacts
from climate.ladder import SOURCE_LABEL
from climate.paper import run_paper
from tests.test_climate.test_fit import _synthetic_inputs


def _model(a: float = 0.0) -> EmosModel:
    model = EmosModel(trend=False)
    model.params[1] = LeadParams(
        lead_month=1, a=a, b=1.0, c=0.01, d=0.0, n_train=10, train_crps=0.1
    )
    return model


def test_fingerprint_stable_across_a_save_load_roundtrip():
    model = _model()
    fp = model_fingerprint(model)
    restored = EmosModel.from_json(model.to_json())
    assert model_fingerprint(restored) == fp


def test_fingerprint_changes_when_a_leadparams_coefficient_changes():
    fp_a = model_fingerprint(_model(a=0.0))
    fp_b = model_fingerprint(_model(a=0.001))
    assert fp_a != fp_b


def test_fingerprint_is_a_short_stable_hex_digest():
    fp = model_fingerprint(_model())
    assert isinstance(fp, str)
    assert len(fp) == 16
    int(fp, 16)  # raises ValueError if this isn't hex


def test_from_json_accepts_a_model_file_without_a_fingerprint_field():
    """Regress note: EmosModel.to_json never emits a `fingerprint` key (the
    field lives in the artifacts climate/fit.py writes, not in the
    model's own canonical serialisation) -- older/foreign model files
    lacking the key must still load cleanly."""
    model = _model()
    text = model.to_json()
    assert "fingerprint" not in json.loads(text)
    restored = EmosModel.from_json(text)
    assert restored.params.keys() == model.params.keys()


def test_run_paper_payload_carries_the_fitted_models_fingerprint():
    frame = pd.DataFrame(
        {
            "target_month": ["2026-08"],
            "lead_month": [1],
            "y_C": [float("nan")],
            "fc_mean_C": [1.10],
            "fc_sd_C": [0.12],
            "target_year_frac": [2026.625],
            "month_of_year": [8],
        }
    )
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
    model = _model()
    payload = run_paper(frame, model, quotes_df=quotes)
    assert payload["model_fingerprint"] == model_fingerprint(model)


def test_write_artifacts_embeds_the_fingerprint_in_the_model_json(tmp_path):
    seas5, era5, noaa = _synthetic_inputs()
    frame = build_frame_from_inputs(seas5, era5, noaa)
    result = fit_gtm(frame, trend=False, leads=range(1, 4), min_train_targets=12)
    fp = model_fingerprint(result.model)

    paths = write_artifacts(result, root=tmp_path)

    model_payload = json.loads(paths["model"].read_text(encoding="utf-8"))
    assert model_payload["fingerprint"] == fp

    report_payload = json.loads(paths["report_json"].read_text(encoding="utf-8"))
    assert report_payload["fingerprint"] == fp

    # The field itself must never be part of the hashed payload -- a model
    # reloaded from the just-written (fingerprint-carrying) file has to
    # fingerprint identically to the in-memory model it came from.
    restored = EmosModel.from_json(paths["model"])
    assert model_fingerprint(restored) == fp
