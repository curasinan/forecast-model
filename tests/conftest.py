"""Shared test fixtures."""

from __future__ import annotations

import shutil
import site
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.models.evidence import EvidenceBundle, EvidenceItem
from src.models.question import MarketData, Question

HOME_GUARD_MODULE = "test_no_home_writes.py"
HOME_GUARD_TEST = "test_suite_never_wrote_under_home_quant_data"
# Prefix of the throwaway home directory created in the system temp dir.
HOME_PREFIX = "pytest-home-"
COLLECTION_OFFENDER = "<collection: written before any test ran>"


@dataclass
class IsolatedHome:
    path: Path
    first_offender: str | None = None

    @property
    def quant_data(self) -> Path:
        return self.path / "quant_data"


_ISOLATED_HOME = pytest.StashKey[IsolatedHome]()


def pytest_configure(config: pytest.Config) -> None:
    """Redirect the process's home to a throwaway directory for the whole run."""
    home = Path(tempfile.mkdtemp(prefix=HOME_PREFIX))
    mp = pytest.MonkeyPatch()
    config.add_cleanup(lambda: _release_home(mp, home))
    mp.setenv("PYTHONUSERBASE", site.getuserbase())
    mp.setenv("USERPROFILE", str(home))
    mp.setenv("HOME", str(home))
    mp.setenv("HOMEDRIVE", home.drive)
    mp.setenv("HOMEPATH", str(home)[len(home.drive) :])
    if Path.home() != home:
        pytest.exit(
            f"home redirect ineffective: Path.home() is {Path.home()}, expected {home}",
            returncode=3,
        )
    config.stash[_ISOLATED_HOME] = IsolatedHome(home)


def _release_home(mp: pytest.MonkeyPatch, home: Path) -> None:
    mp.undo()
    if not (home / "quant_data").exists():
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(scope="session")
def isolated_home(pytestconfig: pytest.Config) -> IsolatedHome:
    """The throwaway home that pytest_configure redirected this session to."""
    return pytestconfig.stash[_ISOLATED_HOME]


@pytest.fixture(autouse=True)
def _attribute_home_writes(
    request: pytest.FixtureRequest, isolated_home: IsolatedHome
) -> Iterator[None]:
    yield
    if isolated_home.first_offender is None and isolated_home.quant_data.exists():
        isolated_home.first_offender = request.node.nodeid


def pytest_collection_finish(session: pytest.Session) -> None:
    home = session.config.stash.get(_ISOLATED_HOME, None)
    if home is not None and home.quant_data.exists():
        home.first_offender = COLLECTION_OFFENDER
    last = [
        item
        for item in session.items
        if item.path.name == HOME_GUARD_MODULE and item.name == HOME_GUARD_TEST
    ]
    if last:
        session.items[:] = [item for item in session.items if item not in last] + last


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
