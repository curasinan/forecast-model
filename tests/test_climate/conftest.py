"""Fixtures shared by the climate tests."""

from __future__ import annotations

import pandas as pd
import pytest

from climate.noaa import LabelObservation
from climate.paths import ENV_VAR
from climate.reduce import ERA5_PARQUET, SEAS5_PARQUET
from climate.storage import append_observations
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
