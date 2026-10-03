"""Guard: the suite must never resolve a default ``~/quant_data`` root.

weather/paths.py, climate/paths.py and src/venues/ibkr/settings.py all fall
back to ``Path.home() / "quant_data" / <lane>`` when nothing pins a root,
and the weather/climate resolvers mkdir that root as a side effect. On the
operator's Zenbook that directory is live data; on a synced mirror it is a
synced copy. So:

* ``isolated_home`` (tests/conftest.py, session-scoped, autouse) redirects
  the process's home -- USERPROFILE on Windows, HOME elsewhere -- to a
  throwaway directory for the whole run, so a leak can never reach the
  real home.
* tests/conftest.py reorders collection so this module runs after every
  other test, and the last test here asserts that nothing appeared under
  the throwaway home's ``quant_data``. A per-test hook records which test
  first populated it, so a failure names the offender.

The 2026-10-01 plan review found the suite creating
``~/quant_data/polymarket_weather/`` (weather.db, meta.json, six
sub-directories) even with HOME pointed elsewhere, because
``init_weather_db`` resolves the default root before it looks at the
explicit db path it was given. Pin roots in tests (see
tests/test_weather/conftest.py); do not loosen this guard.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def quant_data_entries(home: Path) -> list[str]:
    """Every path under ``<home>/quant_data`` as posix-relative strings.

    Returns [] when the directory does not exist. Used both by the guard
    (to name what leaked) and by a self-test that plants a file, so a
    silently broken lister cannot make the guard pass vacuously.
    """
    root = home / "quant_data"
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def test_home_is_redirected_to_a_throwaway_dir(
    isolated_home, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The redirect must be effective in-process on this platform: Python
    reads USERPROFILE on Windows and HOME on POSIX, and both are patched."""
    home = isolated_home.path
    assert Path.home() == home
    assert Path(os.path.expanduser("~")) == home
    assert os.environ["USERPROFILE"] == str(home)
    assert os.environ["HOME"] == str(home)
    assert home.is_relative_to(tmp_path_factory.getbasetemp())


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
    """Runs last (tests/conftest.py::pytest_collection_modifyitems).

    Fails when any earlier test resolved a default data root, naming the
    first test after which ``<home>/quant_data`` was seen to exist.
    """
    assert request.session.items[-1] is request.node, (
        "the home-write guard must be the final test of the session; "
        "check pytest_collection_modifyitems in tests/conftest.py"
    )
    leaked = quant_data_entries(isolated_home.path)
    assert not isolated_home.quant_data.exists(), (
        "a test resolved a default ~/quant_data root (pin it to tmp_path via "
        "settings.weather_data_root / CLIMATE_DATA_ROOT / IBKRSettings.data_root). "
        f"First seen after {isolated_home.first_offender!r}; contents: {leaked}"
    )
