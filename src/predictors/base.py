"""Abstract base class for prediction models."""

from abc import ABC, abstractmethod

from src.models.evidence import EvidenceBundle
from src.models.prediction import Prediction
from src.models.question import Question


class BasePredictor(ABC):
    """Interface for all predictors.

    Each predictor takes a question, its evidence, and extracted features,
    and produces a Prediction with a probability estimate and reasoning.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this predictor."""
        ...

    @abstractmethod
    async def predict(
        self,
        question: Question,
        bundle: EvidenceBundle,
        features: dict[str, float],
    ) -> Prediction:
        """Produce a probability estimate for the question resolving YES."""
        ...
