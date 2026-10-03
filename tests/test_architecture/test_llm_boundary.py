"""Architecture guard: the LLM never enters the pricing or ordering path.

Role decision 2026-09-29 (docs/plans/2026-09-30-spark-research-split-design.md,
section 6): the LLM is a data processor, the numeric model predicts. Pure-AST
scan of the guarded directories and scripts, same approach as
test_import_bans.py. Three self-tests keep the guard honest: every configured
path must contribute files, a planted import in a temp tree must be
reported, and every import form (absolute, aliased, relative) must be seen.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

GUARDED_DIRS = (
    ("climate",),
    ("src", "trading"),
    ("src", "venues"),
    ("src", "edge"),
    ("src", "domain"),
    ("monitoring",),
)
GUARDED_FILES = (
    ("scripts", "run_ibkr_paper.py"),
    ("scripts", "sync_ibkr_paper.py"),
    ("scripts", "record_ibkr_ticks.py"),
    ("scripts", "run_gtm_paper.py"),
)
LLM_PACKAGE = "src.llm"


def _names_llm(module: str) -> bool:
    return module == LLM_PACKAGE or module.startswith(LLM_PACKAGE + ".")


def llm_imports(tree: ast.AST, package: tuple[str, ...] = ()) -> list[int]:
    """Line numbers of every import that binds anything from src.llm.

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
    return hits


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


def test_a_planted_import_in_a_temp_tree_is_reported(tmp_path: Path) -> None:
    (tmp_path / "climate").mkdir()
    (tmp_path / "climate" / "clean.py").write_text("import json\n", encoding="utf-8")
    (tmp_path / "climate" / "planted.py").write_text(
        "from src.llm.client import LocalLLMClient\n", encoding="utf-8"
    )
    (tmp_path / "src" / "trading").mkdir(parents=True)
    (tmp_path / "src" / "trading" / "relative.py").write_text(
        "from ..llm import client\n", encoding="utf-8"
    )
    assert violations(tmp_path) == [
        "climate/planted.py:1: imports src.llm",
        "src/trading/relative.py:1: imports src.llm",
    ]


def test_scanner_recognises_every_import_form() -> None:
    source = (
        "import src.llm\n"
        "import src.llm.client as c\n"
        "from src.llm import LocalLLMClient\n"
        "from src.llm.settings import LocalLLMSettings\n"
        "from src import llm\n"
        "from src.venues.base import VenueAdapter\n"
        "from . import something\n"
    )
    assert llm_imports(ast.parse(source), package=("src", "trading")) == [1, 2, 3, 4, 5]
