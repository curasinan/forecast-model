"""Abstract base class for feature extractors."""

from abc import ABC, abstractmethod

from src.models.evidence import EvidenceBundle


class BaseFeatureExtractor(ABC):
    """Interface for all feature extractors."""

    @abstractmethod
    def extract(self, bundle: EvidenceBundle) -> dict[str, float]:
        """Extract features from evidence."""
        ...
