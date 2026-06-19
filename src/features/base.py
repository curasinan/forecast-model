"""Abstract base class for feature extractors."""

from abc import ABC, abstractmethod

from src.models.evidence import EvidenceBundle


class BaseFeatureExtractor(ABC):
    """Interface for all feature extractors.

    Each extractor takes an EvidenceBundle and produces a dict of
    {feature_name: float_value} that can be fed to prediction models.
    """

    @abstractmethod
    def extract(self, bundle: EvidenceBundle) -> dict[str, float]:
        """Extract features from evidence.

        Returns:
            Dict mapping feature names to float values.
        """
        ...
