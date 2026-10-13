"""Source credibility weighting."""

from src.features.base import BaseFeatureExtractor
from src.models.evidence import EvidenceBundle

SOURCE_CREDIBILITY: dict[str, float] = {
    # Tier 1: Major wire services and established tech press
    "Reuters": 0.95,
    "Associated Press": 0.95,
    "Bloomberg": 0.90,
    "The New York Times": 0.90,
    "The Wall Street Journal": 0.90,
    "TechCrunch": 0.85,
    "The Verge": 0.85,
    "Ars Technica": 0.85,
    "Wired": 0.80,
    "MIT Technology Review": 0.85,
    # Tier 2: Good tech coverage but more opinionated
    "The Information": 0.85,
    "Hacker News": 0.70,
    "VentureBeat": 0.75,
    "Engadget": 0.70,
    "CNBC": 0.75,
    "BBC News": 0.85,
    # Tier 3: Aggregators and community sources
    "Google News": 0.60,
    "Reddit": 0.50,
    "Medium": 0.45,
    "Substack": 0.50,
    # Market data (not news credibility — treated differently)
    "Polymarket": 0.80,
    # Default for unknown sources
    "_default": 0.40,
}


def get_credibility(source_name: str) -> float:
    """Look up credibility score for a source, with fuzzy matching."""
    # Exact match
    if source_name in SOURCE_CREDIBILITY:
        return SOURCE_CREDIBILITY[source_name]

    # Partial match (e.g., "TechCrunch Feed" matches "TechCrunch")
    source_lower = source_name.lower()
    for known_source, score in SOURCE_CREDIBILITY.items():
        if known_source == "_default":
            continue
        if known_source.lower() in source_lower or source_lower in known_source.lower():
            return score

    return SOURCE_CREDIBILITY["_default"]


class CredibilityExtractor(BaseFeatureExtractor):
    """Extracts source credibility features from evidence."""

    def extract(self, bundle: EvidenceBundle) -> dict[str, float]:
        if not bundle.items:
            return {
                "avg_credibility": 0.5,
                "high_credibility_ratio": 0.0,
                "credibility_weighted_sentiment": 0.0,
            }

        credibilities = [get_credibility(item.source_name) for item in bundle.items]

        avg_cred = sum(credibilities) / len(credibilities)
        high_cred = [c for c in credibilities if c >= 0.75]
        high_ratio = len(high_cred) / len(credibilities)

        weighted_sentiments = []
        total_weight = 0.0
        for item, cred in zip(bundle.items, credibilities):
            weighted_sentiments.append(item.sentiment_score * cred)
            total_weight += cred

        weighted_sentiment = (
            sum(weighted_sentiments) / total_weight if total_weight > 0 else 0.0
        )

        return {
            "avg_credibility": avg_cred,
            "high_credibility_ratio": high_ratio,
            "credibility_weighted_sentiment": weighted_sentiment,
        }
