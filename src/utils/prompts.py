"""LLM prompt texts are kept out of the repository; this module loads them from disk."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR_ENV = "LLM_PROMPTS_DIR"


class PromptNotFoundError(FileNotFoundError):
    """The prompt file is missing or empty."""


def prompts_dir() -> Path:
    """``$LLM_PROMPTS_DIR`` when set, else ``prompts/`` under the repo root."""
    override = os.environ.get(PROMPTS_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else REPO_ROOT / "prompts"


@lru_cache(maxsize=None)
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_prompt(name: str) -> str:
    """The text of ``<prompts_dir>/<name>.txt``."""
    path = prompts_dir() / f"{name}.txt"
    try:
        text = _read(path)
    except FileNotFoundError:
        text = ""
    if not text:
        raise PromptNotFoundError(
            f"prompt file {path} is missing or empty: prompt texts are not shipped with the "
            f"code; write your own there or point {PROMPTS_DIR_ENV} at a directory that has it"
        )
    return text
