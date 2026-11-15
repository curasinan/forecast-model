"""Market-implied Gaussian fits of a GTM ladder (climate/sim/implied.py)."""

from __future__ import annotations

import numpy as np
import pytest

from climate.sim.implied import brier, fit_implied, usable_quotes
from tests.test_climate.sim_fixtures import ladder_quotes

STRIKES = np.round(np.arange(0.8, 1.81, 0.1), 2)


def _fit(quotes):
    k, b, a = zip(*quotes)
    return fit_implied(k, b, a)


def test_recovers_mu_and_sigma_of_a_clean_ladder():
    fit = _fit(ladder_quotes(STRIKES, 1.30, 0.15))
    assert fit.identified
    assert fit.mu == pytest.approx(1.30, abs=0.01)
    assert fit.sigma == pytest.approx(0.15, abs=0.01)
    assert fit.ceiling > 0.98


def test_a_plateau_below_one_is_read_as_a_ceiling_not_as_tail_mass():
    strikes = np.round(np.arange(-0.2, 1.71, 0.1), 2)
    fit = _fit(ladder_quotes(strikes, 1.30, 0.18, ceiling=0.93))
    assert fit.ceiling_fitted
    assert fit.ceiling == pytest.approx(0.93, abs=0.01)
    assert fit.mu == pytest.approx(1.30, abs=0.02)
    assert fit.sigma == pytest.approx(0.18, abs=0.02)


def test_no_plateau_means_no_ceiling():
    fit = _fit(ladder_quotes(np.round(np.arange(1.2, 1.61, 0.1), 2), 1.30, 0.15))
    assert not fit.ceiling_fitted and fit.ceiling == 1.0


def test_one_informative_quote_is_not_identified():
    # strikes 0.5 apart: only 1.5 sits inside the band
    strikes = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    fit = _fit(ladder_quotes(strikes, 1.33, 0.14, ceiling=0.965))
    assert fit is not None and fit.n_interior == 1 and not fit.identified


def test_dead_quotes_are_not_used():
    quotes = [(k, 0.01, 0.99) for k in STRIKES] + [(1.3, 0.48, 0.51)]
    assert _fit(quotes) is None


def test_usable_quotes_follows_the_suspect_rule():
    ok = usable_quotes([0.40, 0.01, np.nan, 0.50, 0.30], [0.43, 0.99, 0.5, 0.45, 0.41])
    assert ok.tolist() == [True, False, False, False, False]


def test_survival_takes_the_ceiling_out():
    fit = _fit(ladder_quotes(np.round(np.arange(-0.2, 1.71, 0.1), 2), 1.30, 0.18, ceiling=0.93))
    assert fit.survival(-0.2) == pytest.approx(1.0, abs=1e-6)
    assert fit.survival(fit.mu) == pytest.approx(0.5)


def test_brier():
    assert brier([1.0, 0.0, 0.5], [1, 0, 1]) == pytest.approx(0.25 / 3)
    assert np.isnan(brier([np.nan], [1]))
