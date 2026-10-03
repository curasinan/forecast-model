"""The home-write guard, proven end to end in throwaway child sessions.

tests/test_architecture/test_no_home_writes.py guards the real suite, but a
guard that has never been seen to fail proves nothing: a typo in a path it
reads, a dead ordering hook or a redirect that arrives too late would each
leave it green. So every test here builds a tiny tree -- the real guard module,
copied at run time so it cannot drift, plus a planted test -- and runs a child
``python -m pytest`` on it with the real tests/conftest.py loaded as a plugin
(``-p tests.conftest``: imported, not copied). It then checks the child's exit
code and output.

A child runs entirely inside the calling test's tmp_path: its own temp dir
(where its throwaway home is created), its own base temp, and a stand-in
"real" home that must still be empty afterwards. Nothing it does can reach
this session's throwaway home or the operator's ``~/quant_data``.

This module lives apart from the guard on purpose: the guard module is the one
copied into every child, so a test there that spawned children would recurse,
and the tests here must not be the session's final leak check.
"""

from __future__ import annotations

import os
import shutil
import site
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tests.conftest import COLLECTION_OFFENDER, HOME_GUARD_MODULE, HOME_GUARD_TEST, HOME_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_SOURCE = Path(__file__).with_name(HOME_GUARD_MODULE)

# A test that writes where an unpinned data root would: <home>/quant_data/...
# (test_planted.py sorts after the guard module, so only the ordering hook
# makes the guard see the write.)
WRITES_QUANT_DATA = '''\
from pathlib import Path


def test_planted():
    (Path.home() / "quant_data" / "x").mkdir(parents=True)
'''

# The control: the same test without the write.
WRITES_NOTHING = '''\
from pathlib import Path


def test_planted():
    assert Path.home().is_dir()
'''

# A module that writes while it is imported, i.e. during collection, before any
# fixture exists.
WRITES_AT_IMPORT = '''\
from pathlib import Path

(Path.home() / "quant_data" / "import_time").mkdir(parents=True, exist_ok=True)


def test_nothing():
    pass
'''

# A root conftest that pins Path.home at import, so no redirect can take effect.
PINS_HOME = '''\
import pathlib

_pinned = pathlib.Path(__file__).parent / "pinned_home"
pathlib.Path.home = classmethod(lambda cls: _pinned)
'''

# Not inherited by a child: they would change what it runs or how it fails.
UNINHERITED = {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST", "PYTHONWARNINGS"}


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    output: str
    real_home: list[str]  # what the child left in its stand-in "real" home
    throwaways: list[Path]  # the throwaway homes it left in its temp dir


class ChildTree:
    """A tiny pytest tree holding the real guard module, plus the private
    directories (stand-in home, temp dir, base temp) its child sessions use."""

    def __init__(self, tmp_path: Path, tests: dict[str, str]) -> None:
        self.root = tmp_path / "tree"
        self.real_home = tmp_path / "real_home"
        self.temp = tmp_path / "temp"
        self.basetemp = tmp_path / "basetemp"
        for directory in (self.root, self.real_home, self.temp):
            directory.mkdir()
        (self.root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        shutil.copyfile(GUARD_SOURCE, self.root / HOME_GUARD_MODULE)
        for name, source in tests.items():
            (self.root / name).write_text(source, encoding="utf-8")

    def run(self, *args: str, cache: bool = False) -> ChildResult:
        env = {key: value for key, value in os.environ.items() if key not in UNINHERITED}
        home = self.real_home
        env.update(
            HOME=str(home),
            USERPROFILE=str(home),
            HOMEDRIVE=home.drive,
            HOMEPATH=str(home)[len(home.drive) :],
            TMPDIR=str(self.temp),
            TMP=str(self.temp),
            TEMP=str(self.temp),
            # Keep this interpreter's user site-packages (POSIX derives it from HOME).
            PYTHONUSERBASE=site.getuserbase(),
            PYTHONPATH=os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")])),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONIOENCODING="utf-8",
            # The child needs no third-party plugin: this keeps it independent of what is
            # installed and halves its start-up time.
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        )
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.conftest",
            f"--basetemp={self.basetemp}",
            *args,
        ]
        if not cache:
            command += ["-p", "no:cacheprovider"]
        done = subprocess.run(
            command,
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        return ChildResult(
            returncode=done.returncode,
            output=done.stdout + done.stderr,
            real_home=sorted(entry.name for entry in home.iterdir()),
            throwaways=sorted(self.temp.glob(f"{HOME_PREFIX}*")),
        )


def test_a_leak_fails_the_guard_and_names_the_offender(tmp_path: Path) -> None:
    result = ChildTree(tmp_path, {"test_planted.py": WRITES_QUANT_DATA}).run()
    assert result.returncode == 1, result.output
    assert f"{HOME_GUARD_MODULE}::{HOME_GUARD_TEST}" in result.output
    assert "test_planted.py::test_planted" in result.output  # the offender, by node id
    assert "contents: ['x']" in result.output
    assert result.real_home == [], "the leak reached the child's real home"
    # A leak keeps its evidence: the throwaway home, holding what was written.
    assert len(result.throwaways) == 1, result.throwaways
    assert (result.throwaways[0] / "quant_data" / "x").is_dir()


def test_the_same_tree_without_the_write_passes_and_leaves_nothing(tmp_path: Path) -> None:
    result = ChildTree(tmp_path, {"test_planted.py": WRITES_NOTHING}).run()
    assert result.returncode == 0, result.output
    assert result.real_home == []
    assert result.throwaways == [], "a clean session must remove its throwaway home"


def test_a_write_at_import_time_lands_in_the_throwaway_and_fails_the_guard(
    tmp_path: Path,
) -> None:
    result = ChildTree(tmp_path, {"test_imports.py": WRITES_AT_IMPORT}).run()
    assert result.returncode == 1, result.output
    assert "contents: ['import_time']" in result.output
    assert COLLECTION_OFFENDER in result.output  # not blamed on the first test
    assert result.real_home == [], "the import-time write reached the child's real home"


def test_a_redirect_that_does_not_take_effect_stops_the_session(tmp_path: Path) -> None:
    tree = ChildTree(tmp_path, {"conftest.py": PINS_HOME, "test_planted.py": WRITES_NOTHING})
    result = tree.run()
    assert result.returncode == 3, result.output
    assert "home redirect ineffective" in result.output
    assert "passed" not in result.output and "failed" not in result.output  # no test ran
    assert result.real_home == []
    assert result.throwaways == []


def test_failed_first_ordering_cannot_move_the_leak_check(tmp_path: Path) -> None:
    """``--ff`` moves the previously failed leak check to the front; it has to
    stay last, or it would run before the leak and fail on ordering instead."""
    tree = ChildTree(tmp_path, {"test_planted.py": WRITES_QUANT_DATA})
    assert tree.run(cache=True).returncode == 1
    result = tree.run("--ff", cache=True)
    assert result.returncode == 1, result.output
    assert "contents: ['x']" in result.output
