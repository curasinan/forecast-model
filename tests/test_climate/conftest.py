"""Fixtures shared by the climate tests."""

from __future__ import annotations

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
