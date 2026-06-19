from datetime import datetime

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    """A single piece of evidence relevant to a question."""

    source_type: str  # "rss", "news_api", "github", "polymarket"
    source_name: str  # "TechCrunch", "Hacker News", etc.
    title: str
    url: str = ""
    content_snippet: str = ""  # First ~500 chars or summary
    published_at: datetime | None = None
    collected_at: datetime = Field(default_factory=datetime.now)
    relevance_score: float = 0.0  # 0-1, set during feature extraction
    sentiment_score: float = 0.0  # -1 to 1


class EvidenceBundle(BaseModel):
    """All evidence collected for a question at a point in time."""

    question_id: str
    collected_at: datetime = Field(default_factory=datetime.now)
    items: list[EvidenceItem] = []

    @property
    def item_count(self) -> int:
        return len(self.items)

    def by_source(self, source_type: str) -> list[EvidenceItem]:
        return [item for item in self.items if item.source_type == source_type]

    def recent(self, days: int = 7) -> list[EvidenceItem]:
        cutoff = datetime.now().timestamp() - (days * 86400)
        return [
            item
            for item in self.items
            if item.published_at and item.published_at.timestamp() > cutoff
        ]
