"""LocalLLMSettings guards: foreign .env keys, numeric bounds, the api key as a secret.

test_settings.py pins the defaults and the env wiring; these tests pin what it leaves open,
each against the single regression it exists to catch (named in its docstring). Every
construction passes _env_file=None or a throwaway dotenv in tmp_path: the real .env is
never read.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.llm.settings import LocalLLMSettings

SECRET = "sekrit-value-123"


def test_dotenv_keys_for_other_services_are_ignored(tmp_path):
    """Kills: extra="forbid".

    The shared .env also holds IBKR_* and other services' keys. The env source never
    collects names that are not fields, only the dotenv source does, so a test that stays
    on os.environ cannot tell "ignore" from "forbid"; this one goes through a file.
    """
    env_file = tmp_path / "llm.env"
    env_file.write_text(
        "LLM_MODEL=qwen-test\nLLM_TEMPERATURE=\nLLM_SOMETHING_ELSE=y\nIBKR_HOST=x\n",
        encoding="utf-8",
    )
    settings = LocalLLMSettings(_env_file=env_file)
    assert settings.model == "qwen-test"
    assert settings.temperature is None  # the documented "empty means unset" line, via the file


@pytest.mark.parametrize(
    "name, bad",
    [
        ("max_tokens", "0"),
        ("max_tokens", "-5"),
        ("connect_timeout_s", "0"),
        ("connect_timeout_s", "-1"),
        ("connect_timeout_s", "inf"),
        ("connect_timeout_s", "nan"),
        ("read_timeout_s", "0"),
        ("read_timeout_s", "-1"),
        ("read_timeout_s", "inf"),
        ("read_timeout_s", "nan"),
        ("temperature", "-0.1"),
        ("temperature", "nan"),
        ("temperature", "inf"),
        ("temperature", "-inf"),
    ],
)
def test_numeric_settings_reject_values_the_server_cannot_use(monkeypatch, name, bad):
    """Kills: a bound dropped, an infinite timeout let through (socket.settimeout(inf) raises
    OverflowError at request time). Fails at construction, not as a 400 or a hang mid-run."""
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv(f"LLM_{name.upper()}", bad)
    with pytest.raises(ValidationError) as info:
        LocalLLMSettings(_env_file=None)
    assert [error["loc"] for error in info.value.errors()] == [(name,)]


def test_numeric_settings_accept_the_boundary_values(monkeypatch):
    """Kills: a bound made too strict. Greedy decoding (temperature 0) must stay valid."""
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv("LLM_TEMPERATURE", "0")
    monkeypatch.setenv("LLM_MAX_TOKENS", "1")
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_S", "0.001")
    monkeypatch.setenv("LLM_READ_TIMEOUT_S", "0.001")
    settings = LocalLLMSettings(_env_file=None)
    assert (settings.temperature, settings.max_tokens) == (0.0, 1)
    assert (settings.connect_timeout_s, settings.read_timeout_s) == (0.001, 0.001)


def test_api_key_is_not_echoed_when_validation_fails(monkeypatch):
    """Kills: dropping hide_input_in_errors. For a missing field pydantic echoes the whole
    input dict, api_key included. LLM_MODEL is deliberately absent."""
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    with pytest.raises(ValidationError) as info:
        LocalLLMSettings(_env_file=None)
    assert "model" in str(info.value)  # it is the missing-model error ...
    assert SECRET not in str(info.value)  # ... with the key withheld
    assert SECRET not in repr(info.value)


def test_api_key_is_masked_in_repr_and_str_but_still_usable(monkeypatch):
    """Kills: api_key going back to a plain str (a log line with the settings would leak it)."""
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    settings = LocalLLMSettings(_env_file=None)
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert settings.api_key.get_secret_value() == SECRET
