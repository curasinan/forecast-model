"""Architecture guard: the LLM never enters the pricing, ordering, fitting or labelling path.

Role decision 2026-09-29 (docs/plans/2026-09-30-spark-research-split-design.md,
section 6): the LLM is a data processor, the numeric model predicts. Pure-AST
scan of the guarded directories and scripts, same approach as
test_import_bans.py.

The self-tests keep the guard from passing vacuously. They check that every
configured path exists and contributes files (and that an empty one reads as
empty), that the required scope is still configured (the tests built from the
configuration cannot notice an entry deleted from it), that a planted import is
reported in every guarded directory (at nested depth) and file, that the report
for a mixed tree is exact, that every import form is seen (absolute, aliased,
relative up to three levels, nested in a function, an if or a try), that a file
which cannot be parsed is reported and not skipped, and that the guarded
package still exists.

What the scan cannot see: transitive imports (a guarded module importing an
unguarded one that imports src.llm), importlib / __import__ / string-named
imports, and ``from src import *``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

GUARDED_DIRS = (
    ("climate",),
    ("src", "trading"),
    ("src", "venues"),
    ("src", "edge"),
    ("src", "domain"),
    # Sizing inputs the guarded modules import: EV and Kelly (math), EdgeSignal (models).
    ("src", "math"),
    ("src", "models"),
    ("monitoring",),
)
GUARDED_FILES = (
    ("scripts", "run_ibkr_paper.py"),
    ("scripts", "sync_ibkr_paper.py"),
    ("scripts", "record_ibkr_ticks.py"),
    ("scripts", "run_gtm_paper.py"),
    # Thin CLIs over climate/, yet outside it: the fit writes the operational model and the
    # labels script writes the training labels, so an src.llm import in either would feed
    # the model or its labels unseen (final review, ruling R-Z; design section 11, R16).
    ("scripts", "fit_gtm_emos.py"),
    ("scripts", "collect_climate_labels.py"),
)
LLM_PACKAGE = "src.llm"

# The scope the guard must keep, spelled out here instead of derived from GUARDED_*: every
# test built from that configuration shrinks with it, so deleting an entry would delete its
# own test as well. Widening the configuration needs no edit here; narrowing it does, on purpose.
REQUIRED_SCOPE = (
    "climate",
    "src/trading",
    "src/venues",
    "src/edge",
    "src/domain",
    "src/math",
    "src/models",
    "monitoring",
    "scripts/run_ibkr_paper.py",
    "scripts/sync_ibkr_paper.py",
    "scripts/record_ibkr_ticks.py",
    "scripts/run_gtm_paper.py",
    "scripts/fit_gtm_emos.py",
    "scripts/collect_climate_labels.py",
)


def _names_llm(module: str) -> bool:
    return module == LLM_PACKAGE or module.startswith(LLM_PACKAGE + ".")


def llm_imports(tree: ast.AST, package: tuple[str, ...] = ()) -> list[int]:
    """Line numbers, ascending, of every import that binds anything from src.llm.

    ``package`` is the scanned file's package (its directory relative to
    the repo root); relative imports such as ``from ..llm import client``
    inside ``src/trading/x.py`` resolve against it.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_names_llm(alias.name) for alias in node.names):
                hits.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module or ""
            else:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                module = ".".join([*base, node.module]) if node.module else ".".join(base)
            imports_llm_name = module == "src" and any(a.name == "llm" for a in node.names)
            if _names_llm(module) or imports_llm_name:
                hits.append(node.lineno)
    return sorted(hits)


def guarded_files(root: Path) -> dict[tuple[str, ...], list[Path]]:
    """Scanned files per configured entry; an entry with no files is a guard failure."""
    found: dict[tuple[str, ...], list[Path]] = {}
    for parts in GUARDED_DIRS:
        top = root.joinpath(*parts)
        found[parts] = sorted(top.rglob("*.py")) if top.is_dir() else []
    for parts in GUARDED_FILES:
        path = root.joinpath(*parts)
        found[parts] = [path] if path.is_file() else []
    return found


def violations(root: Path) -> list[str]:
    out: list[str] = []
    for files in guarded_files(root).values():
        for path in files:
            rel = path.relative_to(root)
            package = tuple(rel.parts[:-1])
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source, filename=rel.as_posix())
            except SyntaxError:
                out.append(f"{rel.as_posix()}: unparseable, cannot be guarded")
                continue
            out.extend(
                f"{rel.as_posix()}:{lineno}: imports {LLM_PACKAGE}"
                for lineno in llm_imports(tree, package)
            )
    return out


def test_llm_never_imported_in_pricing_or_ordering_path() -> None:
    found = violations(REPO_ROOT)
    assert not found, (
        "the LLM is a data processor, never the predictor (design section 6):\n"
        + "\n".join(found)
    )


def test_every_guarded_path_contributes_files() -> None:
    empty = [parts for parts, files in guarded_files(REPO_ROOT).items() if not files]
    assert not empty, f"guard configured for paths that no longer exist: {empty}"


def test_a_path_without_python_files_contributes_nothing(tmp_path: Path) -> None:
    # The premise of the test above: an empty, text-only or missing entry must come back as
    # an empty list, or a guarded path that vanished would pass unnoticed.
    (tmp_path / "climate").mkdir()  # exists, holds nothing
    (tmp_path / "monitoring").mkdir()
    (tmp_path / "monitoring" / "notes.txt").write_text("not python\n", encoding="utf-8")
    found = guarded_files(tmp_path)  # every other configured path does not exist at all
    assert found
    assert all(files == [] for files in found.values())


def test_the_guard_still_covers_the_required_scope() -> None:
    configured = {"/".join(parts) for parts in GUARDED_DIRS + GUARDED_FILES}
    dropped = [entry for entry in REQUIRED_SCOPE if entry not in configured]
    assert not dropped, f"guard no longer covers the required scope: {dropped}"


def test_the_guarded_package_exists() -> None:
    init = REPO_ROOT.joinpath(*LLM_PACKAGE.split("."), "__init__.py")
    assert init.is_file(), (
        f"{LLM_PACKAGE} not found at {init}: if the package was renamed, update LLM_PACKAGE, "
        "or the guard scans for an import nobody can write"
    )


def _plant(root: Path, parts: tuple[str, ...]) -> str:
    """Plant one src.llm import where the guard must find it; return its report line.

    A directory entry gets the file two sub-packages down, so a scan that stops
    at the top level misses it; a file entry is planted in the file itself,
    under its real relative path.
    """
    rel = Path(*parts) if parts in GUARDED_FILES else Path(*parts, "pkg", "inner", "planted.py")
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("from src.llm.client import LocalLLMClient\n", encoding="utf-8")
    return f"{rel.as_posix()}:1: imports src.llm"


@pytest.mark.parametrize("parts", GUARDED_DIRS + GUARDED_FILES, ids=lambda parts: "/".join(parts))
def test_a_planted_import_is_reported_for_every_guarded_path(
    tmp_path: Path, parts: tuple[str, ...]
) -> None:
    expected = _plant(tmp_path, parts)
    assert violations(tmp_path) == [expected]


def test_a_planted_import_in_a_temp_tree_is_reported(tmp_path: Path) -> None:
    expected = [_plant(tmp_path, parts) for parts in GUARDED_DIRS + GUARDED_FILES]
    for parts in GUARDED_DIRS:  # a clean file beside each plant must stay unreported
        clean = tmp_path.joinpath(*parts, "pkg", "inner", "clean.py")
        clean.write_text("import json\n", encoding="utf-8")
    # Relative imports resolve against the file's own package, here through violations().
    for rel, source in (
        ("src/trading/relative.py", "from ..llm import client\n"),
        ("src/venues/ibkr/deep.py", "from ...llm import client\n"),
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(source, encoding="utf-8")
        expected.append(f"{rel}:1: imports src.llm")
    # Sorted on both sides: the report follows GUARDED_* order, which this test must not pin.
    assert sorted(violations(tmp_path)) == sorted(expected)


def test_an_unparseable_guarded_file_is_reported_not_skipped(tmp_path: Path) -> None:
    (tmp_path / "climate").mkdir()
    (tmp_path / "climate" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    assert violations(tmp_path) == ["climate/broken.py: unparseable, cannot be guarded"]


def test_scanner_recognises_every_import_form() -> None:
    source = (
        "import src.llm\n"
        "import src.llm.client as c\n"
        "from src.llm import LocalLLMClient\n"
        "from src.llm.settings import LocalLLMSettings\n"
        "from src import llm\n"
        "from src.venues.base import VenueAdapter\n"
        "from . import something\n"
        "import json, src.llm\n"
        "from src import util, llm\n"
        "from src.llm_cache import Cache\n"
        "def lazy():\n"
        "    from src.llm import LocalLLMClient\n"
        "from .. import llm\n"
        "if TYPE_CHECKING:\n"
        "    from src.llm import LocalLLMClient\n"
        "try:\n"
        "    import src.llm\n"
        "except ImportError:\n"
        "    pass\n"
    )
    # Lines 6, 7 and 10 are not the LLM package (src.llm_cache is a sibling). The nested import
    # on line 12 precedes the top-level one on line 13, but ast.walk reaches it last, so only
    # the sorted result is in line order.
    hits = llm_imports(ast.parse(source), package=("src", "trading"))
    assert hits == [1, 2, 3, 4, 5, 8, 9, 12, 13, 15, 17]


def test_scanner_resolves_relative_imports_by_level() -> None:
    # In src/venues/ibkr/ one dot is src.venues.ibkr, two are src.venues, three are src.
    source = (
        "from ...llm import LocalLLMClient\n"
        "from ... import llm\n"
        "from ..llm import LocalLLMClient\n"
        "from .. import llm\n"
        "from .llm import LocalLLMClient\n"
    )
    hits = llm_imports(ast.parse(source), package=("src", "venues", "ibkr"))
    assert hits == [1, 2]  # lines 3-5 are src.venues.llm and src.venues.ibkr.llm
