from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class QuestionStatus(str, Enum):
    OPEN = "open"
    RESOLVED_YES = "resolved_yes"
    RESOLVED_NO = "resolved_no"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


class MarketData(BaseModel):
    """Live Polymarket data linked to a question."""

    market_id: str = ""  # Polymarket condition ID / token ID
    slug: str = ""  # Polymarket market slug for URL
    yes_price: float = 0.5  # Current YES token price (0-1)
    no_price: float = 0.5  # Current NO token price (0-1)
    volume_24h: float = 0.0  # 24h trading volume in USD
    total_volume: float = 0.0  # All-time volume
    liquidity: float = 0.0  # Current liquidity depth
    last_updated: datetime | None = None


class Question(BaseModel):
    """A binary prediction question about a tech event."""

    id: str  # e.g. "gpt5-release-june-2026"
    title: str  # "Will GPT-5 release before June 2026?"
    description: str = ""  # Detailed resolution criteria
    deadline: datetime  # When the question resolves
    created_at: datetime = Field(default_factory=datetime.now)
    status: QuestionStatus = QuestionStatus.OPEN
    resolution_date: datetime | None = None
    resolution_source: str | None = None
    tags: list[str] = []  # ["openai", "gpt", "llm", "release"]
    search_keywords: list[str] = []  # Keywords for collectors to search
    market: MarketData = Field(default_factory=MarketData)  # Polymarket link
