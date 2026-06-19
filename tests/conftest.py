"""Shared test fixtures."""

from datetime import datetime, timedelta

import pytest

from src.models.evidence import EvidenceBundle, EvidenceItem
from src.models.question import MarketData, Question


@pytest.fixture
def sample_question():
    return Question(
        id="test-gpt5",
        title="Will GPT-5 release before July 2026?",
        description="Test question",
        deadline=datetime(2026, 7, 1),
        tags=["openai", "gpt"],
        search_keywords=["GPT-5 release", "OpenAI GPT-5"],
        market=MarketData(yes_price=0.45, no_price=0.55, volume_24h=50000),
    )


@pytest.fixture
def sample_evidence_bundle():
    now = datetime.now()
    return EvidenceBundle(
        question_id="test-gpt5",
        items=[
            EvidenceItem(
                source_type="rss",
                source_name="TechCrunch",
                title="OpenAI reportedly preparing GPT-5 launch",
                url="https://example.com/1",
                content_snippet="Sources say OpenAI is in final stages of GPT-5 testing.",
                published_at=now - timedelta(days=2),
            ),
            EvidenceItem(
                source_type="rss",
                source_name="The Verge",
                title="Sam Altman hints at next-gen model release",
                url="https://example.com/2",
                content_snippet="In a recent interview, Altman discussed upcoming model capabilities.",
                published_at=now - timedelta(days=5),
            ),
            EvidenceItem(
                source_type="rss",
                source_name="Hacker News",
                title="GPT-5 benchmark leaks suggest major improvements",
                url="https://example.com/3",
                content_snippet="Leaked benchmarks show significant improvements over GPT-4.",
                published_at=now - timedelta(days=1),
            ),
            EvidenceItem(
                source_type="polymarket",
                source_name="Polymarket",
                title="Market: GPT-5 release",
                url="https://polymarket.com/test",
                content_snippet="YES: $0.45 | NO: $0.55 | Volume: $50,000",
                published_at=now,
            ),
        ],
    )


@pytest.fixture
def empty_evidence_bundle():
    return EvidenceBundle(question_id="test-empty", items=[])
