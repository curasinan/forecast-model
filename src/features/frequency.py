"""Frequency feature extractor — mention counts, momentum, acceleration."""

from src.features.base import BaseFeatureExtractor
from src.models.evidence import EvidenceBundle


class FrequencyExtractor(BaseFeatureExtractor):
    """Extracts mention frequency and momentum features."""

    def extract(self, bundle: EvidenceBundle) -> dict[str, float]:
        total = bundle.item_count
        recent_7d = len(bundle.recent(days=7))
        recent_30d = len(bundle.recent(days=30))

        # Acceleration: ratio of last-7d mentions to previous-23d mentions
        # > 1 means mentions are accelerating (something is happening)
        older_23d = recent_30d - recent_7d
        if older_23d > 0:
            acceleration = recent_7d / (older_23d / (23 / 7))  # Normalize for time period
        elif recent_7d > 0:
            acceleration = 2.0  # New activity where there was none
        else:
            acceleration = 0.0

        # Source diversity: how many unique sources?
        unique_sources = len({item.source_name for item in bundle.items})

        return {
            "total_mentions": float(total),
            "mentions_last_7d": float(recent_7d),
            "mentions_last_30d": float(recent_30d),
            "mention_acceleration": min(acceleration, 5.0),  # Cap at 5x
            "source_diversity": float(unique_sources),
        }
