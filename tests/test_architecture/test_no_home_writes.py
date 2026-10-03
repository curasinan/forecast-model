"""Guard: the suite must never resolve a default ``~/quant_data`` root.

weather/paths.py, climate/paths.py and src/venues/ibkr/settings.py all fall
back to ``Path.home() / "quant_data" / <lane>`` when nothing pins a root,
and the weather/climate resolvers mkdir that root as a side effect. On the
operator's Zenbook that directory is live data; on a synced mirror it is a
synced copy. So:

* ``pytest_configure`` (tests/conftest.py) redirects the process's home --
  USERPROFILE on Windows, HOME elsewhere -- to a throwaway directory before
  collection starts, so neither an import-time write nor a test can reach
  the real home. The throwaway is removed when the session ends, unless a
  ``quant_data`` directory appeared in it (then it is kept as evidence).
* ``pytest_collection_finish`` (tests/conftest.py) pins the last test here
  to the very end of the session -- ``--ff``/``--lf`` reorder before it runs,
  so they cannot move it -- and that test asserts that nothing appeared under
  the throwaway home's ``quant_data``. A per-test hook records which test
  first populated it (or that collection did), so a failure names the
  offender.
* test_home_guard_end_to_end.py runs this very module in child sessions
  against planted leaks, so the guard cannot rot unnoticed.

The 2026-10-01 plan review found the suite creating
``~/quant_data/polymarket_weather/`` (weather.db, meta.json, six
sub-directories) even with HOME pointed elsewhere, because
``init_weather_db`` resolves the default root before it looks at the
explicit db path it was given. Pin roots in tests (see
tests/test_weather/conftest.py); do not loosen this guard.

Keep this module self-contained (standard library and pytest only): the
end-to-end tests copy it into their child trees.
"""

from __future__ import annotations

import os
import site
import subprocess
import sys
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


def test_home_is_redirected_to_a_throwaway_dir(isolated_home) -> None:
    """The redirect must be effective in-process on this platform: Python
    reads USERPROFILE on Windows and HOME on POSIX, and both are patched --
    HOMEDRIVE + HOMEPATH too, the Windows fallback when USERPROFILE is gone."""
    home = isolated_home.path
    assert home.is_dir()
    assert Path.home() == home
    assert Path(os.path.expanduser("~")) == home
    assert os.environ["USERPROFILE"] == str(home)
    assert os.environ["HOME"] == str(home)
    assert os.environ["HOMEDRIVE"] + os.environ["HOMEPATH"] == str(home)


def test_child_processes_keep_the_real_user_site() -> None:
    """Moving HOME must not move a child's user site-packages: POSIX derives
    ``~/.local`` from HOME, so the redirect pins PYTHONUSERBASE first. Windows
    derives it from APPDATA instead; that is dropped here so the child falls
    back to the (redirected) home exactly as a POSIX child does."""
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
    """Runs last (tests/conftest.py::pytest_collection_finish).

    Fails when anything earlier -- a test, or collection itself -- resolved a
    default data root, naming the first test after which (or the phase in
    which) ``<home>/quant_data`` was seen to exist.
    """
    assert request.session.items[-1] is request.node, (
        "the home-write guard must be the final test of the session; "
        "check pytest_collection_finish in tests/conftest.py"
    )
    # Through the same lister the positive control above exercises, plus a
    # direct existence check: an empty ``quant_data`` directory lists as [].
    leaked = quant_data_entries(isolated_home.path)
    assert not leaked and not (isolated_home.path / "quant_data").exists(), (
        "a test (or a module import) resolved a default ~/quant_data root (pin it to "
        "tmp_path via settings.weather_data_root / CLIMATE_DATA_ROOT / IBKRSettings.data_root). "
        f"First seen after {isolated_home.first_offender!r}; contents: {leaked}. "
        f"The throwaway home is kept for inspection at {isolated_home.path}"
    )
