"""Fixtures shared by the climate tests."""

from __future__ import annotations

from functools import partial

import numpy as np
import pandas as pd
import pytest

from climate.nn.daily import DAILY_PARQUET
from climate.noaa import LabelObservation
from climate.paths import ENV_VAR
from climate.reduce import ERA5_PARQUET, SEAS5_PARQUET
from climate.storage import append_observations, ledger_path
from tests.test_climate import nn_fixtures


@pytest.fixture
def synthetic_inputs():
    """The deterministic (seas5, era5_K, noaa_C) triple of nn_fixtures.synthetic_inputs."""
    return nn_fixtures.synthetic_inputs()


@pytest.fixture
def synthetic_root(tmp_path, monkeypatch, synthetic_inputs):
    """A climate data root holding synthetic_inputs as the research files; the env var names it."""
    seas5, era5_K, noaa_C = synthetic_inputs
    root = tmp_path / "climate_root"
    seas5_path = root / SEAS5_PARQUET[0] / SEAS5_PARQUET[1]
    era5_path = root / ERA5_PARQUET[0] / ERA5_PARQUET[1]
    for path in (seas5_path, era5_path):
        path.parent.mkdir(parents=True)
    seas5.to_parquet(seas5_path, index=False)
    era5 = pd.DataFrame({"month": era5_K.index, "t2m_global_K": era5_K.to_numpy()})
    era5.to_parquet(era5_path, index=False)
    labels = [LabelObservation("gtm", period, float(y), "") for period, y in noaa_C.items()]
    # backfill vintage: a live first print stamped now would move every window cutoff to 2026
    append_observations(labels, backfill=True, root=root)
    monkeypatch.setenv(ENV_VAR, str(root))
    return root


@pytest.fixture
def synthetic_nowcast():
    """The deterministic (daily_K, noaa_C, seas5, ledger) of nn_fixtures.synthetic_nowcast."""
    return nn_fixtures.synthetic_nowcast()


@pytest.fixture
def nowcast_root(tmp_path, monkeypatch, synthetic_nowcast):
    """A climate data root holding synthetic_nowcast as the files the nowcast benchmark reads."""
    daily_K, _, seas5, ledger = synthetic_nowcast
    root = tmp_path / "nowcast_root"
    seas5_path = root / SEAS5_PARQUET[0] / SEAS5_PARQUET[1]
    daily_path = root / DAILY_PARQUET[0] / DAILY_PARQUET[1]
    for path in (seas5_path, daily_path, ledger_path(root)):
        path.parent.mkdir(parents=True, exist_ok=True)
    seas5.to_parquet(seas5_path, index=False)
    daily = pd.DataFrame({"date": daily_K.index, "t2m_global_K": daily_K.to_numpy()})
    daily.to_parquet(daily_path, index=False)
    ledger.to_parquet(ledger_path(root), index=False)
    monkeypatch.setenv(ENV_VAR, str(root))
    return root


def _quick_fit(model, rows: pd.DataFrame):
    """Fit the model on the rows with every network stopped after 10 epochs."""
    from climate.nn import seasonal_model
    from climate.nn.torch_model import FitConfig

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(seasonal_model, "FitConfig", partial(FitConfig, max_epochs=10))
        return model.fit(rows)


@pytest.fixture
def synthetic_root_with_daily(tmp_path, monkeypatch, synthetic_nowcast):
    """A research root of synthetic_nowcast whose daily series lacks the day 2011-05-04."""
    from climate.nn.nowcast_windows import monthly_means

    daily_K, noaa_C, seas5, _ = synthetic_nowcast
    daily_K = daily_K.drop(pd.Timestamp("2011-05-04"))
    monthly = monthly_means(daily_K)
    root = tmp_path / "daily_root"
    files = {
        SEAS5_PARQUET: seas5,
        DAILY_PARQUET: pd.DataFrame({"date": daily_K.index, "t2m_global_K": daily_K.to_numpy()}),
        ERA5_PARQUET: pd.DataFrame({"month": monthly.index, "t2m_global_K": monthly.to_numpy()}),
    }
    for (folder, name), table in files.items():
        (root / folder).mkdir(parents=True, exist_ok=True)
        table.to_parquet(root / folder / name, index=False)
    labels = [LabelObservation("gtm", period, float(y), "") for period, y in noaa_C.items()]
    # backfill vintage: a live first print stamped now would move every window cutoff to 2026
    append_observations(labels, backfill=True, root=root)
    monkeypatch.setenv(ENV_VAR, str(root))
    return root


@pytest.fixture(scope="session")
def saved_nowcast_model(tmp_path_factory):
    """A one-seed nowcast network fitted through 2010 for 10 epochs, saved with its provenance."""
    pytest.importorskip("torch")
    from climate.bridge import Bridge
    from climate.emos import target_year
    from climate.nn.features import seasonal_frame
    from climate.nn.nowcast import nowcast_frame
    from climate.nn.nowcast_model import GtmNowcastModel
    from climate.nn.nowcast_windows import monthly_means
    from climate.nn.report import write_provenance

    daily_K, noaa_C, seas5, ledger = nn_fixtures.synthetic_nowcast()
    monthly = monthly_means(daily_K)
    bridge = Bridge().fit(monthly, noaa_C, end="2010-12")
    seasonal = seasonal_frame(seas5, monthly, noaa_C, bridge=bridge)
    frame = nowcast_frame(daily_K, noaa_C, seasonal, bridge=bridge, ledger=ledger)
    rows = frame[(target_year(frame) <= 2010) & np.isfinite(frame["y_C"])]
    model_dir = tmp_path_factory.mktemp("nowcast_model")
    _quick_fit(GtmNowcastModel(seeds=(0,), hidden=8), rows).save(model_dir)
    provenance = {
        "kind": "nowcast",
        "protocol": "strict",
        "test_year": 2011,
        "labels_through": "2010-12",
        "bridge": bridge.params,
    }
    write_provenance(model_dir, provenance)
    return model_dir


@pytest.fixture(scope="session")
def saved_models(tmp_path_factory):
    """(emos_json, nn_dir): EMOS and a one-seed 10-epoch seasonal network, fitted through 2011."""
    pytest.importorskip("torch")
    from climate.bridge import Bridge
    from climate.dataset import build_frame_from_inputs
    from climate.emos import EmosModel, target_year
    from climate.nn.features import seasonal_frame
    from climate.nn.report import write_provenance
    from climate.nn.seasonal_model import GtmNnModel

    seas5, era5_K, noaa_C = nn_fixtures.synthetic_inputs()
    folder = tmp_path_factory.mktemp("saved_models")
    plain = build_frame_from_inputs(seas5, era5_K, noaa_C)
    labelled = plain[(target_year(plain) <= 2011) & np.isfinite(plain["y_C"])]
    emos_json = folder / "gtm_emos.json"
    EmosModel(trend=True).fit(labelled).to_json(emos_json)
    bridge = Bridge().fit(era5_K, noaa_C, end="2011-12")
    seasonal = seasonal_frame(seas5, era5_K, noaa_C, bridge=bridge)
    rows = seasonal[(target_year(seasonal) <= 2011) & np.isfinite(seasonal["y_C"])]
    nn_dir = folder / "nn"
    _quick_fit(GtmNnModel(seeds=(0,), hidden=8), rows).save(nn_dir)
    provenance = {
        "kind": "seasonal",
        "protocol": "strict",
        "test_year": 2012,
        "labels_through": "2011-12",
        "bridge": bridge.params,
    }
    write_provenance(nn_dir, provenance)
    return emos_json, nn_dir
