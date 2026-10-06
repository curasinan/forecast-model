"""climate.paths.root_path: the root data_root would return, without creating it."""

from __future__ import annotations

from pathlib import Path

from climate.paths import ENV_VAR, data_root, root_path


def test_root_path_follows_the_override_then_the_variable_then_the_home(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert root_path() == Path.home() / "quant_data" / "climate_data"

    monkeypatch.setenv(ENV_VAR, str(tmp_path / "from_env"))
    assert root_path() == (tmp_path / "from_env").resolve()
    assert root_path(str(tmp_path / "named")) == (tmp_path / "named").resolve()
    assert root_path("   ") == (tmp_path / "from_env").resolve()  # a blank override is no override


def test_root_path_creates_nothing_and_data_root_agrees_with_it(tmp_path):
    target = tmp_path / "climate_data"
    assert root_path(str(target)) == target.resolve()
    assert not target.exists()

    assert data_root(str(target)) == root_path(str(target))
    assert (target / "labels").is_dir() and (target / "raw").is_dir()
