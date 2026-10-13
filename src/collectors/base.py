"""Abstract base class for all data collectors."""

from abc import ABC, abstractmethod

from src.models.evidence import EvidenceItem
from src.models.question import Question


class BaseCollector(ABC):
    """Interface every data collector implements."""

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Unique identifier for this collector type (e.g. 'rss', 'polymarket')."""
        ...

    @abstractmethod
    async def collect(self, question: Question, max_results: int = 20) -> list[EvidenceItem]:
        """Gather evidence relevant to the given question."""
        ...

    def is_available(self) -> bool:
        """Check if this collector has valid credentials and can operate."""
        return True
