"""Isolate the LLM tests from the operator's shell environment."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_") and key != "LLM_LIVE":
            monkeypatch.delenv(key, raising=False)
