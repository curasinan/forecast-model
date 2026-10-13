"""Sentiment feature extractor using VADER (Valence Aware Dictionary and sEntiment Reasoner)."""

from nltk.sentiment.vader import SentimentIntensityAnalyzer

from src.features.base import BaseFeatureExtractor
from src.models.evidence import EvidenceBundle


class SentimentExtractor(BaseFeatureExtractor):
    """Extracts sentiment features from evidence text using VADER."""

    def __init__(self):
        self._analyzer = SentimentIntensityAnalyzer()

    def _score_text(self, text: str) -> float:
        """Get compound sentiment score for a text string."""
        if not text.strip():
            return 0.0
        scores = self._analyzer.polarity_scores(text)
        return scores["compound"]

    def extract(self, bundle: EvidenceBundle) -> dict[str, float]:
        if not bundle.items:
            return {
                "avg_sentiment": 0.0,
                "sentiment_std": 0.0,
                "positive_ratio": 0.0,
                "negative_ratio": 0.0,
                "neutral_ratio": 1.0,
                "sentiment_momentum": 0.0,
                "max_positive": 0.0,
                "max_negative": 0.0,
            }

        # Score each evidence item on title + snippet
        scores: list[float] = []
        for item in bundle.items:
            text = f"{item.title}. {item.content_snippet}"
            score = self._score_text(text)
            scores.append(score)
            # Also store on the item for later use
            item.sentiment_score = score

        # Aggregate statistics
        avg = sum(scores) / len(scores)
        variance = sum((s - avg) ** 2 for s in scores) / len(scores)
        std = variance ** 0.5

        positive = [s for s in scores if s > 0.05]
        negative = [s for s in scores if s < -0.05]
        neutral = [s for s in scores if -0.05 <= s <= 0.05]

        recent_items = bundle.recent(days=7)
        older_items = [i for i in bundle.items if i not in recent_items]

        if recent_items and older_items:
            recent_avg = sum(
                self._score_text(f"{i.title}. {i.content_snippet}") for i in recent_items
            ) / len(recent_items)
            older_avg = sum(
                self._score_text(f"{i.title}. {i.content_snippet}") for i in older_items
            ) / len(older_items)
            momentum = recent_avg - older_avg
        else:
            momentum = 0.0

        return {
            "avg_sentiment": avg,
            "sentiment_std": std,
            "positive_ratio": len(positive) / len(scores),
            "negative_ratio": len(negative) / len(scores),
            "neutral_ratio": len(neutral) / len(scores),
            "sentiment_momentum": momentum,
            "max_positive": max(scores),
            "max_negative": min(scores),
        }
