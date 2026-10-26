"""Fixtures shared by the climate tests."""

from __future__ import annotations

import pytest

from tests.test_climate import nn_fixtures


@pytest.fixture
def synthetic_inputs():
    """The deterministic (seas5, era5_K, noaa_C) triple of nn_fixtures.synthetic_inputs."""
    return nn_fixtures.synthetic_inputs()
