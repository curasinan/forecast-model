"""Shared test fixtures.

Also the home-directory isolation for the whole session: see
``isolated_home`` below and tests/test_architecture/test_no_home_writes.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.models.evidence import EvidenceBundle, EvidenceItem
from src.models.question import MarketData, Question

# The guard module that must run after every other test (see
# pytest_collection_modifyitems).
HOME_GUARD_MODULE = "test_no_home_writes.py"


@dataclass
class IsolatedHome:
    """The session's throwaway home plus the first test seen to populate
    ``<home>/quant_data`` (so the guard can name the offender)."""

    path: Path
    first_offender: str | None = None

    @property
    def quant_data(self) -> Path:
        return self.path / "quant_data"


@pytest.fixture(scope="session", autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[IsolatedHome]:
    """Redirect the process's home to a throwaway directory for the session.

    weather/paths.py, climate/paths.py and src/venues/ibkr/settings.py fall
    back to ``Path.home() / "quant_data" / <lane>`` when no root is pinned,
    and the weather/climate resolvers mkdir it. On the operator's machine
    that is live data; on a synced mirror it is a synced copy. Python reads
    USERPROFILE on Windows and HOME on POSIX (HOMEDRIVE/HOMEPATH only as a
    Windows fallback), so all four are pointed at the throwaway directory.
    The redirect is verified at once: if ``Path.home()`` did not move, the
    session stops rather than run against the real home.

    Repo-relative paths (settings.database_path, fixtures/, data/) are
    unaffected -- only the home directory moves.
    """
    home = tmp_path_factory.mktemp("home")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("USERPROFILE", str(home))
        mp.setenv("HOME", str(home))
        mp.setenv("HOMEDRIVE", home.drive)
        mp.setenv("HOMEPATH", str(home)[len(home.drive) :])
        if Path.home() != home:
            pytest.exit(
                f"home redirect ineffective: Path.home() is {Path.home()}, expected {home}",
                returncode=3,
            )
        yield IsolatedHome(home)


@pytest.fixture(autouse=True)
def _attribute_home_writes(
    request: pytest.FixtureRequest, isolated_home: IsolatedHome
) -> Iterator[None]:
    """After each test, remember the first test after which the throwaway
    home's ``quant_data`` exists. One ``exists()`` per test; never deletes."""
    yield
    if isolated_home.first_offender is None and isolated_home.quant_data.exists():
        isolated_home.first_offender = request.node.nodeid


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Run the home-write guard module last so it observes the whole session."""
    guard = [item for item in items if item.path.name == HOME_GUARD_MODULE]
    if guard:
        items[:] = [item for item in items if item.path.name != HOME_GUARD_MODULE] + guard


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
