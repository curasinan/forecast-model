"""Architecture guard: a file that imports torch caps its GPU memory with apply_gpu_budget."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_MODULE = "analysis.gpu_budget"
HELPER = "apply_gpu_budget"
EXEMPT = ("analysis/gpu_budget.py",)  # the helper imports torch to apply the cap itself
SKIP_DIRS = {"__pycache__", "node_modules", "build", "dist"}


def _skipped(rel: Path) -> bool:
    return any(part.startswith(".") or part in SKIP_DIRS or part.endswith(".egg-info")
               for part in rel.parts[:-1])


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if not _skipped(p.relative_to(root)))


def _is_torch(module: str) -> bool:
    return module == "torch" or module.startswith("torch.")


def torch_import_lines(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(_is_torch(a.name) for a in node.names):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and _is_torch(node.module or ""):
            lines.append(node.lineno)
    return sorted(lines)


def imports_helper(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == HELPER_MODULE:
            if any(a.name == HELPER for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any(a.name == HELPER_MODULE for a in node.names):
            return True
    return False


def calls_helper(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == HELPER) or (
                isinstance(func, ast.Attribute) and func.attr == HELPER
            ):
                return True
    return False


def violations(root: Path) -> list[str]:
    out: list[str] = []
    for path in python_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in EXEMPT:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError:
            out.append(f"{rel}: unparseable, cannot be guarded")
            continue
        lines = torch_import_lines(tree)
        if not lines:
            continue
        if not imports_helper(tree):
            out.append(f"{rel}:{lines[0]}: imports torch without importing {HELPER_MODULE}.{HELPER}")
        elif not calls_helper(tree):
            out.append(f"{rel}:{lines[0]}: imports torch and never calls {HELPER}")
    return out


def torch_importers(root: Path) -> list[str]:
    found = []
    for path in python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        if torch_import_lines(tree):
            found.append(path.relative_to(root).as_posix())
    return found


def test_every_torch_importer_calls_the_gpu_budget() -> None:
    found = violations(REPO_ROOT)
    assert not found, (
        "a torch job must cap its GPU memory first (analysis/gpu_budget.py):\n" + "\n".join(found)
    )


def test_the_scan_reaches_the_repo_and_a_real_torch_importer() -> None:
    assert len(python_files(REPO_ROOT)) > 100
    assert "scripts/spark/cgroup_cuda_check.py" in torch_importers(REPO_ROOT)


def test_the_exempt_helper_exists_and_defines_the_call() -> None:
    for rel in EXEMPT:
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert HELPER in names, f"{rel} no longer defines {HELPER}"


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


GOOD = "from analysis.gpu_budget import apply_gpu_budget\nimport torch\napply_gpu_budget(8)\n"
FORMS = {
    "plain.py": "import torch\n",
    "dotted.py": "import torch.nn as nn\n",
    "from_root.py": "from torch import nn\n",
    "from_sub.py": "from torch.utils.data import DataLoader\n",
    "nested.py": "def build():\n    import torch\n    return torch\n",
    "in_try.py": "try:\n    import torch\nexcept ImportError:\n    torch = None\n",
}


def test_every_import_form_is_reported(tmp_path: Path) -> None:
    for name, source in FORMS.items():
        _write(tmp_path, f"research/deep/{name}", source)
    found = violations(tmp_path)
    assert len(found) == len(FORMS)
    assert all("without importing analysis.gpu_budget" in line for line in found)


def test_import_and_call_together_pass_and_either_alone_does_not(tmp_path: Path) -> None:
    _write(tmp_path, "jobs/good.py", GOOD)
    _write(tmp_path, "jobs/module_form.py",
           "import analysis.gpu_budget as gb\nimport torch\ngb.apply_gpu_budget(4)\n")
    _write(tmp_path, "jobs/stand_in.py",
           "import torch\ndef apply_gpu_budget(x):\n    pass\napply_gpu_budget(8)\n")
    _write(tmp_path, "jobs/never_called.py",
           "from analysis.gpu_budget import apply_gpu_budget\nimport torch\n")
    assert violations(tmp_path) == [
        "jobs/never_called.py:2: imports torch and never calls apply_gpu_budget",
        "jobs/stand_in.py:1: imports torch without importing analysis.gpu_budget.apply_gpu_budget",
    ]


def test_an_unparseable_file_is_reported_not_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "jobs/broken.py", "import torch\ndef (:\n")
    assert violations(tmp_path) == ["jobs/broken.py: unparseable, cannot be guarded"]


def test_a_virtualenv_inside_the_repo_is_not_scanned(tmp_path: Path) -> None:
    _write(tmp_path, ".venv/lib/python3.13/site-packages/torch/__init__.py", "import torch._C\n")
    _write(tmp_path, "learning/study.py", "import torch\n")
    assert violations(tmp_path) == [
        "learning/study.py:1: imports torch without importing analysis.gpu_budget.apply_gpu_budget"
    ]
