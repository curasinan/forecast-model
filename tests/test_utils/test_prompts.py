"""Prompt texts are loaded from an untracked directory, never from the code."""

from __future__ import annotations

import subprocess

import pytest

from src.utils import prompts
from src.utils.prompts import PromptNotFoundError, load_prompt, prompts_dir


@pytest.fixture(autouse=True)
def _fresh_cache():
    prompts._read.cache_clear()
    yield
    prompts._read.cache_clear()


def test_loads_the_named_file_from_the_override_directory(tmp_path, monkeypatch):
    (tmp_path / "scorer.txt").write_text("You estimate things.\n", encoding="utf-8")
    monkeypatch.setenv("LLM_PROMPTS_DIR", str(tmp_path))
    assert load_prompt("scorer") == "You estimate things."


def test_default_directory_is_prompts_under_the_repo_root(monkeypatch):
    monkeypatch.delenv("LLM_PROMPTS_DIR", raising=False)
    assert prompts_dir() == prompts.REPO_ROOT / "prompts"


def test_a_missing_file_names_the_path_and_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROMPTS_DIR", str(tmp_path))
    with pytest.raises(PromptNotFoundError) as excinfo:
        load_prompt("absent")
    assert str(tmp_path / "absent.txt") in str(excinfo.value)
    assert "LLM_PROMPTS_DIR" in str(excinfo.value)


def test_an_empty_file_is_refused(tmp_path, monkeypatch):
    (tmp_path / "blank.txt").write_text("  \n", encoding="utf-8")
    monkeypatch.setenv("LLM_PROMPTS_DIR", str(tmp_path))
    with pytest.raises(PromptNotFoundError):
        load_prompt("blank")


def test_no_prompt_text_is_tracked_in_the_repository():
    tracked = subprocess.run(
        ["git", "ls-files", "prompts"], cwd=prompts.REPO_ROOT, capture_output=True, text=True
    )
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")
    assert tracked.stdout.strip() == ""
