"""Architecture guard: venue SDKs must not leak outside the venue layer.

Pure-AST scan (no module is imported) of every *.py under the scanned
top-level directories. Banned roots may only appear under src/venues/,
legacy/, or in scripts/verify_ibkr_entitlements.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SCANNED_DIRS = (
    "src",
    "collectors",
    "features",
    "decision",
    "model",
    "monitoring",
    "analysis",
    "weather",
    "scripts",
    "mcp_server",
)

BANNED_ROOTS = frozenset({"ib_async", "ibapi", "py_clob_client", "web3", "eth_account"})

ALLOWED_DIR_PREFIXES = (
    ("src", "venues"),
    ("legacy",),
)
ALLOWED_FILES = frozenset({("scripts", "verify_ibkr_entitlements.py")})


def _is_allowed(rel_path: Path) -> bool:
    parts = rel_path.parts
    if parts in ALLOWED_FILES:
        return True
    return any(parts[: len(prefix)] == prefix for prefix in ALLOWED_DIR_PREFIXES)


def _banned_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """(lineno, banned_root) for every absolute import of a banned root."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_ROOTS:
                    hits.append((node.lineno, root))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root in BANNED_ROOTS:
                    hits.append((node.lineno, root))
    return hits


def test_no_banned_venue_sdk_imports_outside_venue_layer() -> None:
    violations: list[str] = []
    for dirname in SCANNED_DIRS:
        top = REPO_ROOT / dirname
        if not top.is_dir():
            continue
        for path in sorted(top.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT)
            if _is_allowed(rel):
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                tree = ast.parse(source, filename=str(rel))
            except SyntaxError:
                continue
            for lineno, root in _banned_imports(tree):
                violations.append(f"{rel.as_posix()}:{lineno}: imports banned root '{root}'")
    assert not violations, (
        "venue SDK imports outside src/venues/, legacy/, or "
        "scripts/verify_ibkr_entitlements.py:\n" + "\n".join(violations)
    )
