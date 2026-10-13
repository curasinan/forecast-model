"""Feature pipeline — combines all extractors into one feature vector."""

from src.features.base import BaseFeatureExtractor
from src.models.evidence import EvidenceBundle


class FeaturePipeline:
    """Runs all feature extractors and merges results."""

    def __init__(self, extractors: list[BaseFeatureExtractor] | None = None):
        self.extractors: list[BaseFeatureExtractor] = extractors or []

    def add(self, extractor: BaseFeatureExtractor) -> None:
        self.extractors.append(extractor)

    def extract_all(self, bundle: EvidenceBundle) -> dict[str, float]:
        """Run all extractors and combine into one feature dict."""
        features: dict[str, float] = {}
        for extractor in self.extractors:
            features.update(extractor.extract(bundle))
        return features
