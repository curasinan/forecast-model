"""Temporal feature extractor — time-based signals."""

from datetime import datetime

from src.features.base import BaseFeatureExtractor
from src.models.evidence import EvidenceBundle


def _to_naive(dt: datetime) -> datetime:
    """Strip timezone info for safe arithmetic with naive datetimes."""
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


class TemporalExtractor(BaseFeatureExtractor):
    """Extracts time-related features from evidence."""

    def __init__(self, deadline: datetime):
        self.deadline = _to_naive(deadline)

    def extract(self, bundle: EvidenceBundle) -> dict[str, float]:
        now = datetime.now()
        delta = self.deadline - now
        days_remaining = max(0, delta.total_seconds() / 86400)

        # Evidence recency: what fraction of evidence is from the last 7 days?
        recent_items = bundle.recent(days=7)
        recency_ratio = len(recent_items) / max(1, bundle.item_count)

        # Average age of evidence in days
        ages = []
        for item in bundle.items:
            if item.published_at:
                pub = _to_naive(item.published_at)
                age = (now - pub).total_seconds() / 86400
                ages.append(max(0, age))
        avg_age = sum(ages) / max(1, len(ages)) if ages else 30.0

        return {
            "days_to_deadline": days_remaining,
            "deadline_urgency": max(0, 1.0 - (days_remaining / 365)),  # 0=far, 1=imminent
            "evidence_recency_ratio": recency_ratio,
            "avg_evidence_age_days": avg_age,
        }
