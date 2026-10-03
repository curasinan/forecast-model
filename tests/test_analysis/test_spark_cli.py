"""Thin CLIs under scripts/spark/: exit codes and outputs on the synthetic root.

Same import pattern as tests/test_climate/test_noaa_labels.py: the scripts
directory goes on sys.path and the script module is imported by name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import compare_json as compare_cli  # noqa: E402
import live_manifest as manifest_cli  # noqa: E402
import parity_score as score_cli  # noqa: E402
import verify_mirror as verify_cli  # noqa: E402

import analysis.spark_parity as sp  # noqa: E402


def test_live_manifest_cli_writes_the_manifest(spark_root, tmp_path, capsys):
    out = tmp_path / "m.json"
    rc = manifest_cli.main(["--root", str(spark_root.root), "--out", str(out)])
    assert rc == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["climate_data/models/gtm_emos.json"]["size"] == spark_root.model.stat().st_size
    assert "heartbeat mtime_ns=None" in capsys.readouterr().out


def test_verify_mirror_cli_exit_0_prints_identity(spark_root, capsys):
    rc = verify_cli.main(["--root", str(spark_root.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["sweep_count"] == 1


def test_verify_mirror_cli_exit_1_lists_failures(spark_root, capsys):
    spark_root.sweep.write_bytes(bytes(range(256)))
    rc = verify_cli.main(["--root", str(spark_root.root)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "quotes-220822Z.parquet" in captured.err
    assert captured.out == ""


def test_compare_json_cli_exit_codes(tmp_path, capsys):
    left = tmp_path / "a.json"
    right = tmp_path / "b.json"
    left.write_text(json.dumps({"x": 1.0, "identity": {"machine": {"python": "a"}}}),
                    encoding="utf-8")
    right.write_text(json.dumps({"x": 1.0, "identity": {"machine": {"python": "b"}}}),
                     encoding="utf-8")
    assert compare_cli.main([str(left), str(right)]) == 1
    assert "MISMATCH identity.machine.python" in capsys.readouterr().out
    assert compare_cli.main([str(left), str(right), "--ignore", "identity.machine"]) == 0
    assert "PARITY OK" in capsys.readouterr().out
    empty = tmp_path / "e.json"
    empty.write_text("{}", encoding="utf-8")
    assert compare_cli.main([str(empty), str(empty)]) == 1
    assert "floor" in capsys.readouterr().out


# ---- --differ, the machine summary and the dirty-tree warning (final review, Important 1) -----

ZENBOOK = {"machine": "AMD64", "system": "Windows", "python": "3.14.2", "git_dirty": False}
SPARK = {"machine": "aarch64", "system": "Linux", "python": "3.14.2", "git_dirty": False}
GATE = ["--ignore", "identity.machine"]  # the registered gate's ignore, without the tolerance flags


def _payload(machine, *, x=1.0):
    """A parity-shaped payload: a numeric leaf, an identity witness, optionally a machine block."""
    identity = {"snapshot": {"code_sha": "1f743a6", "sweep_count": 1}}
    if machine is not None:
        identity["machine"] = machine
    return {"x": x, "identity": identity}


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_differ_fails_a_payload_compared_with_its_own_copy(tmp_path, capsys):
    """The gap the final review found: with identity.machine ignored, a payload passes against
    a copy of itself. --differ evaluates the RAW payloads, so the ignore does not hide it."""
    left = _write(tmp_path, "a.json", _payload(ZENBOOK))
    right = _write(tmp_path, "b.json", _payload(ZENBOOK))
    assert compare_cli.main([left, right, *GATE]) == 0  # the old gate: vacuous
    assert "PARITY OK" in capsys.readouterr().out
    assert compare_cli.main([left, right, *GATE, "--differ", "identity.machine"]) == 1
    out = capsys.readouterr().out
    assert "PARITY FAILED: identity.machine is identical on both sides" in out
    assert "PARITY OK" not in out


def test_differ_passes_when_the_subtrees_differ(tmp_path, capsys):
    left = _write(tmp_path, "a.json", _payload(ZENBOOK))
    right = _write(tmp_path, "b.json", _payload(SPARK))
    assert compare_cli.main([left, right, *GATE, "--differ", "identity.machine"]) == 0
    out = capsys.readouterr().out
    assert "PARITY OK" in out
    assert "PARITY FAILED" not in out


@pytest.mark.parametrize(
    "left_machine, right_machine, missing",
    [(None, SPARK, ["left"]), (ZENBOOK, None, ["right"]), (None, None, ["left", "right"])],
    ids=["left", "right", "both"],
)
def test_differ_names_the_side_that_lacks_the_subtree(
    tmp_path, capsys, left_machine, right_machine, missing
):
    """A payload without the subtree fails, naming the side, even though --ignore covers it."""
    left = _write(tmp_path, "a.json", _payload(left_machine))
    right = _write(tmp_path, "b.json", _payload(right_machine))
    assert compare_cli.main([left, right, *GATE, "--differ", "identity.machine"]) == 1
    out = capsys.readouterr().out
    for side in ("left", "right"):
        line = f"PARITY FAILED: identity.machine is missing on {side}"
        assert (line in out) == (side in missing)
    assert "is identical on both sides" not in out  # nothing to compare when a side is absent


def test_differ_is_repeatable_and_judges_each_path_on_its_own(tmp_path, capsys):
    left = _write(tmp_path, "a.json", _payload(ZENBOOK))
    right = _write(tmp_path, "b.json", _payload(SPARK))  # the snapshot blocks are equal
    args = [left, right, *GATE, "--differ", "identity.machine", "--differ", "identity.snapshot"]
    assert compare_cli.main(args) == 1
    out = capsys.readouterr().out
    assert "PARITY FAILED: identity.snapshot is identical on both sides" in out
    assert "identity.machine is" not in out  # the machine blocks differ: no failure for them


def test_differ_treats_a_null_subtree_as_present(tmp_path, capsys):
    """Null on both sides is identical (not missing); null against 0 differs."""
    null_a = _write(tmp_path, "n1.json", {"x": 1.0, "bridge": None})
    null_b = _write(tmp_path, "n2.json", {"x": 1.0, "bridge": None})
    zero = _write(tmp_path, "n3.json", {"x": 1.0, "bridge": 0})
    assert compare_cli.main([null_a, null_b, "--ignore", "bridge", "--differ", "bridge"]) == 1
    out = capsys.readouterr().out
    assert "PARITY FAILED: bridge is identical on both sides" in out
    assert "missing" not in out
    assert compare_cli.main([null_a, zero, "--ignore", "bridge", "--differ", "bridge"]) == 0
    assert "PARITY OK" in capsys.readouterr().out


def test_differ_compares_plain_json_without_tolerance(tmp_path, capsys):
    """A 1e-13 relative difference is within compare_json's tolerance but is still a difference."""
    near_a = _write(tmp_path, "d1.json", {"v": {"a": 1.0}})
    near_b = _write(tmp_path, "d2.json", {"v": {"a": 1.0 + 1e-13}})
    assert compare_cli.main([near_a, near_b]) == 0  # the tolerance accepts the pair
    capsys.readouterr()
    assert compare_cli.main([near_a, near_b, "--differ", "v"]) == 0  # --differ sees a difference
    assert "PARITY OK" in capsys.readouterr().out
    assert compare_cli.main([near_a, near_a, "--differ", "v"]) == 1  # and a copy is identical
    assert "PARITY FAILED: v is identical on both sides" in capsys.readouterr().out


def test_differ_equality_is_the_json_text_so_nan_matches_nan_and_one_is_not_one_point_zero(
    tmp_path, capsys
):
    """Two parsed NaN objects are never ``==``, which would make a copy of a payload with a NaN
    in the subtree look different; 1 and 1.0 are different JSON text, so they count as different;
    the order of the keys is not data."""
    nan = float("nan")
    nan_a = _write(tmp_path, "nan1.json", {"x": 1.0, "m": {"v": nan, "k": [1, {"w": nan}]}})
    nan_b = _write(tmp_path, "nan2.json", {"x": 1.0, "m": {"v": nan, "k": [1, {"w": nan}]}})
    assert compare_cli.main([nan_a, nan_b, "--ignore", "m", "--differ", "m"]) == 1
    assert "PARITY FAILED: m is identical on both sides" in capsys.readouterr().out
    reordered = _write(tmp_path, "keys.json", {"m": {"k": [1, {"w": nan}], "v": nan}, "x": 1.0})
    assert compare_cli.main([nan_a, reordered, "--ignore", "m", "--differ", "m"]) == 1
    assert "PARITY FAILED: m is identical on both sides" in capsys.readouterr().out
    one = _write(tmp_path, "one.json", {"x": 1.0, "m": {"v": 1}})
    one_point_zero = _write(tmp_path, "one0.json", {"x": 1.0, "m": {"v": 1.0}})
    assert compare_cli.main([one, one_point_zero, "--ignore", "m", "--differ", "m"]) == 0
    assert "PARITY OK" in capsys.readouterr().out


def test_machine_summary_line_names_both_machines(tmp_path, capsys):
    left = _write(tmp_path, "a.json", _payload(ZENBOOK))
    right = _write(tmp_path, "b.json", _payload(SPARK))
    assert compare_cli.main([left, right, *GATE]) == 0
    out = capsys.readouterr().out
    assert "machines: left AMD64/Windows/py3.14.2 vs right aarch64/Linux/py3.14.2" in out
    assert "WARNING" not in out


def test_machine_summary_line_marks_an_absent_side_and_skips_when_both_are_absent(
    tmp_path, capsys
):
    with_block = _write(tmp_path, "a.json", _payload(SPARK))
    without = _write(tmp_path, "b.json", _payload(None))
    compare_cli.main([with_block, without, *GATE])
    assert "machines: left aarch64/Linux/py3.14.2 vs right absent" in capsys.readouterr().out
    compare_cli.main([without, without])
    assert "machines:" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "left_dirty, right_dirty, warned",
    [
        (True, False, ["left"]),
        (False, True, ["right"]),
        (True, True, ["left", "right"]),
        (False, False, []),
        (None, None, []),  # git unreadable: unknown, not dirty
    ],
    ids=["left", "right", "both", "clean", "unknown"],
)
def test_a_dirty_tree_is_a_warning_not_a_failure(
    tmp_path, capsys, left_dirty, right_dirty, warned
):
    left = _write(tmp_path, "a.json", _payload({**ZENBOOK, "git_dirty": left_dirty}))
    right = _write(tmp_path, "b.json", _payload({**SPARK, "git_dirty": right_dirty}))
    assert compare_cli.main([left, right, *GATE, "--differ", "identity.machine"]) == 0
    out = capsys.readouterr().out
    for side in ("left", "right"):
        assert (f"WARNING: {side} payload has git_dirty=true" in out) == (side in warned)
    assert "PARITY OK" in out


def test_the_summary_stays_the_first_line_and_the_verdict_the_last(tmp_path, capsys):
    """Operators read `sed -n '1p;$p'`: the added lines sit between the two."""
    left = _write(tmp_path, "a.json", _payload({**ZENBOOK, "git_dirty": True}))
    right = _write(tmp_path, "b.json", _payload(SPARK))
    compare_cli.main([left, right, *GATE, "--differ", "identity.machine"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("numeric leaves compared: ")
    assert any(line.startswith("machines: ") for line in lines[1:-1])
    assert any(line.startswith("WARNING: ") for line in lines[1:-1])
    assert lines[-1] == "PARITY OK"
    failing = _write(tmp_path, "c.json", _payload(SPARK, x=2.0))
    compare_cli.main([right, failing, *GATE])
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0].rstrip(":") for line in lines] == [
        "numeric",  # the summary
        "MISMATCH",  # then every mismatch ...
        "machines",  # ... then the machine line ...
        "PARITY",  # ... and the verdict
    ]
    assert lines[-1] == "PARITY FAILED: 1 mismatch(es)"


def test_parity_score_cli_writes_a_clockless_payload(
    spark_root, universe_json, dec_2026_frame, tmp_path, monkeypatch, capsys,
    climate_default_root,
):
    monkeypatch.setattr(sp, "build_training_frame", lambda root: dec_2026_frame)
    out = tmp_path / "parity" / "out.json"
    rc = score_cli.main([
        "--root", str(spark_root.root), "--out", str(out), "--universe-json", str(universe_json),
    ])
    assert rc == 0
    assert "sweep=2026-09-28T22:08:22Z" in capsys.readouterr().out  # the summary line
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "ts_utc" not in payload
    assert payload["identity"]["snapshot"]["sweep_count"] == 1
    assert not (spark_root.root / "climate_data" / "paper").exists()
    # the CLI is rooted at --root, never at the default climate root (an unrooted write
    # would land in the tmp dir conftest points CLIMATE_DATA_ROOT at)
    assert not (climate_default_root / "paper").exists()
