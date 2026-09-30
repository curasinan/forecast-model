"""Tests for the quantitative math foundations."""

from src.math.bayesian import BayesianUpdater
from src.math.entropy import binary_entropy, opportunity_score
from src.math.expected_value import best_trade, expected_value, expected_value_no
from src.math.kelly import half_kelly, kelly_fraction, position_size
from src.math.log_odds import implied_probability, logit, odds_from_prob, sigmoid


class TestLogOdds:
    def test_logit_sigmoid_roundtrip(self):
        for p in [0.1, 0.25, 0.5, 0.75, 0.9]:
            assert abs(sigmoid(logit(p)) - p) < 1e-6

    def test_logit_at_half(self):
        assert abs(logit(0.5)) < 1e-6  # logit(0.5) = 0

    def test_sigmoid_at_zero(self):
        assert abs(sigmoid(0) - 0.5) < 1e-6  # sigmoid(0) = 0.5

    def test_logit_increases(self):
        assert logit(0.7) > logit(0.5) > logit(0.3)

    def test_extreme_values(self):
        # Should not crash on extreme inputs
        assert sigmoid(1000) > 0.99
        assert sigmoid(-1000) < 0.01

    def test_odds_from_prob(self):
        assert abs(odds_from_prob(0.5) - 2.0) < 1e-6
        assert abs(odds_from_prob(0.25) - 4.0) < 1e-6

    def test_implied_probability_clamps(self):
        assert implied_probability(0.0) == 0.01
        assert implied_probability(1.0) == 0.99
        assert implied_probability(0.5) == 0.5


class TestExpectedValue:
    def test_positive_ev(self):
        # Model says 50%, market at 35% -> buying YES has positive EV
        ev = expected_value(model_prob=0.5, market_price=0.35)
        assert ev > 0

    def test_negative_ev(self):
        # Model says 20%, market at 35% -> buying YES has negative EV
        ev = expected_value(model_prob=0.2, market_price=0.35)
        assert ev < 0

    def test_zero_ev_at_market(self):
        # When model agrees with market, EV should be ~0
        ev = expected_value(model_prob=0.5, market_price=0.5)
        assert abs(ev) < 1e-6

    def test_ev_no_positive(self):
        # Model says 20% YES -> 80% NO. Market YES at 0.60 -> NO at 0.40
        ev = expected_value_no(model_prob=0.2, market_price_yes=0.60)
        assert ev > 0

    def test_best_trade_yes(self):
        direction, ev = best_trade(0.7, 0.4)
        assert direction == "YES"
        assert ev > 0

    def test_best_trade_no(self):
        direction, ev = best_trade(0.2, 0.6)
        assert direction == "NO"
        assert ev > 0

    def test_best_trade_pass(self):
        direction, ev = best_trade(0.5, 0.5)
        assert direction == "PASS"


class TestKelly:
    def test_no_edge_no_bet(self):
        # Model and market agree
        f = kelly_fraction(model_prob=0.5, market_price=0.5)
        assert abs(f) < 1e-6

    def test_positive_edge(self):
        # Model sees edge
        f = kelly_fraction(model_prob=0.6, market_price=0.4)
        assert f > 0

    def test_negative_edge_returns_zero(self):
        # Model sees NO is more likely, Kelly for YES should be 0
        f = kelly_fraction(model_prob=0.3, market_price=0.5)
        assert f == 0.0

    def test_half_kelly_is_half(self):
        full = kelly_fraction(0.7, 0.4)
        half = half_kelly(0.7, 0.4)
        assert abs(half - full * 0.5) < 1e-6

    def test_position_size_capped(self):
        size = position_size(bankroll=10000, model_prob=0.9, market_price=0.1, max_fraction=0.25)
        assert size <= 2500  # Max 25% of bankroll

    def test_position_size_no_edge(self):
        size = position_size(bankroll=10000, model_prob=0.5, market_price=0.5)
        assert size == 0.0


class TestEntropy:
    def test_max_entropy_at_half(self):
        h = binary_entropy(0.5)
        assert abs(h - 1.0) < 1e-6  # Binary entropy is 1 bit at p=0.5

    def test_low_entropy_at_extremes(self):
        assert binary_entropy(0.95) < 0.4
        assert binary_entropy(0.05) < 0.4

    def test_entropy_symmetric(self):
        assert abs(binary_entropy(0.3) - binary_entropy(0.7)) < 1e-6

    def test_opportunity_score_favors_uncertainty(self):
        # Same volume, higher entropy = higher score
        score_uncertain = opportunity_score(0.5, 10000)
        score_certain = opportunity_score(0.95, 10000)
        assert score_uncertain > score_certain

    def test_opportunity_score_favors_volume(self):
        # Same entropy, higher volume = higher score
        score_high_vol = opportunity_score(0.5, 100000)
        score_low_vol = opportunity_score(0.5, 100)
        assert score_high_vol > score_low_vol


class TestBayesianUpdater:
    def test_no_update(self):
        u = BayesianUpdater(prior=0.5)
        assert u.current == 0.5
        assert len(u.history) == 0

    def test_positive_evidence_increases(self):
        u = BayesianUpdater(prior=0.5)
        u.update("Positive news", likelihood_yes=0.9, likelihood_no=0.2)
        assert u.current > 0.5

    def test_negative_evidence_decreases(self):
        u = BayesianUpdater(prior=0.5)
        u.update("Negative news", likelihood_yes=0.2, likelihood_no=0.8)
        assert u.current < 0.5

    def test_strong_evidence_larger_shift(self):
        u1 = BayesianUpdater(prior=0.5)
        u1.update("Weak", likelihood_yes=0.6, likelihood_no=0.5)

        u2 = BayesianUpdater(prior=0.5)
        u2.update("Strong", likelihood_yes=0.95, likelihood_no=0.1)

        assert abs(u2.current - 0.5) > abs(u1.current - 0.5)

    def test_sequential_updates(self):
        u = BayesianUpdater(prior=0.3)
        u.update("First", likelihood_yes=0.8, likelihood_no=0.3)
        u.update("Second", likelihood_yes=0.7, likelihood_no=0.4)
        assert len(u.history) == 2
        # Each update should have shifted the probability
        assert u.history[0].posterior != u.history[0].prior
        assert u.history[1].posterior != u.history[1].prior

    def test_probability_stays_bounded(self):
        u = BayesianUpdater(prior=0.5)
        # Many strong updates in one direction
        for _ in range(20):
            u.update("Very strong", likelihood_yes=0.99, likelihood_no=0.01)
        assert 0.01 <= u.current <= 0.99

    def test_total_shift(self):
        u = BayesianUpdater(prior=0.3)
        u.update("Positive", likelihood_yes=0.8, likelihood_no=0.3)
        assert u.total_shift > 0

    def test_reset(self):
        u = BayesianUpdater(prior=0.5)
        u.update("Test", likelihood_yes=0.9, likelihood_no=0.1)
        u.reset(prior=0.4)
        assert u.current == 0.4
        assert len(u.history) == 0
