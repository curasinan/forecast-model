"""LocalLLMSettings: LLM_ env prefix, required model, defaults from the Spark handover."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.llm.settings import LocalLLMSettings


def test_model_is_required_and_has_no_default(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(ValidationError):
        LocalLLMSettings(_env_file=None)


def test_env_prefix_and_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    settings = LocalLLMSettings(_env_file=None)
    assert settings.model == "qwen-test"
    assert settings.base_url == "http://127.0.0.1:8000/v1"
    assert settings.api_key.get_secret_value() == "EMPTY"
    assert settings.max_tokens == 8192
    assert settings.reasoning_effort == "low"
    assert settings.temperature is None
    assert settings.connect_timeout_s == 10.0
    assert settings.read_timeout_s == 900.0


def test_reasoning_effort_is_validated(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "max")
    with pytest.raises(ValidationError):
        LocalLLMSettings(_env_file=None)


def test_unrelated_env_is_ignored(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv("LLM_SOMETHING_ELSE", "x")
    assert LocalLLMSettings(_env_file=None).model == "qwen-test"


def test_empty_env_values_mean_unset(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv("LLM_TEMPERATURE", "")
    monkeypatch.setenv("LLM_MAX_TOKENS", "")
    settings = LocalLLMSettings(_env_file=None)
    assert settings.temperature is None
    assert settings.max_tokens == 8192
