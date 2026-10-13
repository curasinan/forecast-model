"""Guard: the suite must never resolve a default ``~/quant_data`` root."""

from __future__ import annotations

import os
import site
import subprocess
import sys
from pathlib import Path

import pytest


def quant_data_entries(home: Path) -> list[str]:
    """Every path under ``<home>/quant_data`` as posix-relative strings."""
    root = home / "quant_data"
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def test_home_is_redirected_to_a_throwaway_dir(isolated_home) -> None:
    home = isolated_home.path
    assert home.is_dir()
    assert Path.home() == home
    assert Path(os.path.expanduser("~")) == home
    assert os.environ["USERPROFILE"] == str(home)
    assert os.environ["HOME"] == str(home)
    assert os.environ["HOMEDRIVE"] + os.environ["HOMEPATH"] == str(home)


def test_child_processes_keep_the_real_user_site() -> None:
    env = {key: value for key, value in os.environ.items() if key != "APPDATA"}
    child = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getuserbase())"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == site.getuserbase()


def test_lister_reports_a_planted_quant_data_write(tmp_path: Path) -> None:
    """Positive control: the guard's lister must CATCH a known-bad layout."""
    assert quant_data_entries(tmp_path) == []
    planted = tmp_path / "quant_data" / "polymarket_weather" / "weather.db"
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"")
    assert quant_data_entries(tmp_path) == [
        "polymarket_weather",
        "polymarket_weather/weather.db",
    ]


def test_suite_never_wrote_under_home_quant_data(
    request: pytest.FixtureRequest, isolated_home
) -> None:
    """Runs last (tests/conftest.py::pytest_collection_finish)."""
    assert request.session.items[-1] is request.node, (
        "the home-write guard must be the final test of the session; "
        "check pytest_collection_finish in tests/conftest.py"
    )
    leaked = quant_data_entries(isolated_home.path)
    assert not leaked and not (isolated_home.path / "quant_data").exists(), (
        "a test (or a module import) resolved a default ~/quant_data root (pin it to "
        "tmp_path via settings.weather_data_root / CLIMATE_DATA_ROOT / IBKRSettings.data_root). "
        f"First seen after {isolated_home.first_offender!r}; contents: {leaked}. "
        f"The throwaway home is kept for inspection at {isolated_home.path}"
    )
