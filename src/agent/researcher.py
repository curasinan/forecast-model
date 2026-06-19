"""Research agent — orchestrates the full prediction pipeline.

Wraps the entire collect -> features -> predict -> edge -> store flow
into a reusable class. Used by the scheduler for automated re-evaluation
and by the dashboard for on-demand analysis.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.collectors.github_activity import GitHubActivityCollector
from src.collectors.news_api import NewsAPICollector
from src.collectors.polymarket import PolymarketCollector
from src.collectors.registry import CollectorRegistry
from src.collectors.rss_feed import RSSFeedCollector
from src.config import settings
from src.edge.edge_finder import find_edge
from src.edge.vpin import assess_vpin, estimate_vpin_from_price_movement
from src.evaluation.tracker import EvaluationTracker
from src.features.credibility import CredibilityExtractor
from src.features.frequency import FrequencyExtractor
from src.features.market_features import MarketFeatureExtractor
from src.features.pipeline import FeaturePipeline
from src.features.sentiment import SentimentExtractor
from src.features.temporal import TemporalExtractor
from src.math.monte_carlo import simulate_edge, simulate_probability
from src.models.evidence import EvidenceBundle
from src.models.prediction import EdgeSignal, Prediction, PredictionRecord
from src.models.question import MarketData, Question
from src.predictors.bayesian_predictor import BayesianPredictor
from src.predictors.classical_ml import ClassicalMLPredictor
from src.predictors.ensemble import EnsemblePredictor, compute_ensemble_weights
from src.predictors.llm_predictor import LLMPredictor
from src.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass
class ResearchResult:
    """Complete result from analyzing one question."""

    question: Question
    bundle: EvidenceBundle
    features: dict[str, float]
    predictions: list[Prediction]
    ensemble: Prediction
    edge: EdgeSignal
    vpin_value: float
    vpin_level: str
    mc_prob_mean: float
    mc_prob_ci: tuple[float, float]
    mc_edge_mean: float
    mc_kelly_mean: float
    mc_cv_edge: float
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def has_edge(self) -> bool:
        return self.edge.has_edge

    @property
    def summary(self) -> str:
        """One-line summary of the result."""
        direction = self.edge.direction
        return (
            f"{self.question.title[:50]}: "
            f"ensemble={self.ensemble.probability:.0%} vs market={self.edge.market_prob:.0%} "
            f"({direction}, EV=${self.edge.expected_value:+.3f})"
        )


class Researcher:
    """Orchestrates the full prediction pipeline for a question."""

    def __init__(self, db: Database, tracker: EvaluationTracker):
        self._db = db
        self._tracker = tracker

    async def analyze(self, question: Question) -> ResearchResult:
        """Run the complete pipeline for one question.

        Steps:
        1. Collect evidence from all sources
        2. Fetch live market data
        3. Extract features
        4. Assess VPIN
        5. Run all predictors + ensemble
        6. Detect edge
        7. Run Monte Carlo
        8. Store everything
        """
        # Step 1: Collect evidence
        registry = CollectorRegistry()
        registry.register(RSSFeedCollector())
        registry.register(PolymarketCollector())
        registry.register(NewsAPICollector())
        registry.register(GitHubActivityCollector())

        bundle = await registry.collect_all(question)
        logger.info(f"Collected {bundle.item_count} items for '{question.id}'")

        # Step 2: Live market data
        polymarket = PolymarketCollector()
        market_data = question.market
        if question.market.market_id:
            live = await polymarket.get_market_by_id(question.market.market_id)
            if live:
                market_data = live
                question.market = live

        # Step 3: Features
        pipeline = FeaturePipeline()
        pipeline.add(TemporalExtractor(deadline=question.deadline))
        pipeline.add(FrequencyExtractor())
        pipeline.add(SentimentExtractor())
        pipeline.add(CredibilityExtractor())
        pipeline.add(MarketFeatureExtractor(market=market_data))
        features = pipeline.extract_all(bundle)

        # Step 4: VPIN
        vpin_value = estimate_vpin_from_price_movement(
            current_price=features.get("market_yes_price", 0.5),
            volume_24h=features.get("market_volume_24h", 0),
            price_change_24h=0.0,
        )
        vpin_assessment = assess_vpin(vpin_value)
        vpin_flag = not vpin_assessment["trust_market_price"]

        # Step 5: Predictors
        predictors = [
            BayesianPredictor(),
            ClassicalMLPredictor(),
            LLMPredictor(),
        ]

        predictions = []
        for predictor in predictors:
            try:
                pred = await predictor.predict(question, bundle, features)
                predictions.append(pred)
            except Exception as e:
                logger.warning(f"Predictor {predictor.name} failed: {e}")

        # Ensemble
        learned_weights = compute_ensemble_weights(self._tracker)
        ensemble = EnsemblePredictor(
            strategy="confidence_weighted",
            learned_weights=learned_weights,
        )
        ensemble.set_predictions(predictions)
        ensemble_pred = await ensemble.predict(question, bundle, features)

        # Step 6: Edge
        market_price = features.get("market_yes_price", 0.5)
        edge = find_edge(
            question_id=question.id,
            model_prob=ensemble_pred.probability,
            market_price_yes=market_price,
            confidence=ensemble_pred.confidence,
            vpin_flag=vpin_flag,
        )

        # Step 7: Monte Carlo
        mc_prob = simulate_probability(
            base_probability=ensemble_pred.probability,
            confidence=ensemble_pred.confidence,
            n_simulations=10_000,
        )
        mc_edge = simulate_edge(
            model_probability=ensemble_pred.probability,
            model_confidence=ensemble_pred.confidence,
            market_price=market_price,
            n_simulations=10_000,
        )

        # Step 8: Store
        record = PredictionRecord(
            question_id=question.id,
            predictions=predictions,
            ensemble_probability=ensemble_pred.probability,
            edge=edge,
        )
        self._db.store_question(question)
        self._db.store_evidence(bundle)
        self._db.store_prediction(record)
        self._db.store_market_snapshot(question.id, market_data)

        for pred in predictions:
            self._tracker.store_snapshot(
                question_id=question.id,
                predictor_name=pred.predictor_name,
                probability=pred.probability,
                confidence=pred.confidence,
                market_price=market_price,
                edge_pct=pred.probability - market_price,
                kelly_fraction=edge.kelly_fraction,
                evidence_count=pred.evidence_count,
                features=features,
            )

        self._tracker.store_snapshot(
            question_id=question.id,
            predictor_name="ensemble",
            probability=ensemble_pred.probability,
            confidence=ensemble_pred.confidence,
            market_price=market_price,
            edge_pct=edge.edge_pct,
            kelly_fraction=edge.kelly_fraction,
            evidence_count=bundle.item_count,
            features=features,
        )

        return ResearchResult(
            question=question,
            bundle=bundle,
            features=features,
            predictions=predictions,
            ensemble=ensemble_pred,
            edge=edge,
            vpin_value=vpin_value,
            vpin_level=vpin_assessment["level"],
            mc_prob_mean=mc_prob.mean,
            mc_prob_ci=(mc_prob.ci_5, mc_prob.ci_95),
            mc_edge_mean=mc_edge.mean_edge,
            mc_kelly_mean=mc_edge.mean_kelly,
            mc_cv_edge=mc_edge.cv_edge,
        )

    async def analyze_all(self, questions: list[Question]) -> list[ResearchResult]:
        """Analyze all questions sequentially."""
        results = []
        for q in questions:
            try:
                result = await self.analyze(q)
                results.append(result)
                logger.info(f"Analyzed: {result.summary}")
            except Exception as e:
                logger.error(f"Failed to analyze '{q.id}': {e}")
        return results
