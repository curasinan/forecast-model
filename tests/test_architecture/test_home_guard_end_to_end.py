"""The home-write guard, proven end to end in throwaway child sessions."""

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

WRITES_AT_IMPORT = '''\
from pathlib import Path

(Path.home() / "quant_data" / "import_time").mkdir(parents=True, exist_ok=True)


def test_nothing():
    pass
'''

PINS_HOME = '''\
import pathlib

_pinned = pathlib.Path(__file__).parent / "pinned_home"
pathlib.Path.home = classmethod(lambda cls: _pinned)
'''

UNINHERITED = {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST", "PYTHONWARNINGS"}


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    output: str
    real_home: list[str]  # what the child left in its stand-in "real" home
    throwaways: list[Path]  # the throwaway homes it left in its temp dir


class ChildTree:
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
            PYTHONUSERBASE=site.getuserbase(),
            PYTHONPATH=os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")])),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONIOENCODING="utf-8",
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
    tree = ChildTree(tmp_path, {"test_planted.py": WRITES_QUANT_DATA})
    assert tree.run(cache=True).returncode == 1
    result = tree.run("--ff", cache=True)
    assert result.returncode == 1, result.output
    assert "contents: ['x']" in result.output
