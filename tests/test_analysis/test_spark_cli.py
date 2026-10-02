"""Thin CLIs under scripts/spark/: exit codes and outputs on the synthetic root.

Same import pattern as tests/test_climate/test_noaa_labels.py: the scripts
directory goes on sys.path and the script module is imported by name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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


def test_parity_score_cli_writes_a_clockless_payload(
    spark_root, universe_json, dec_2026_frame, tmp_path, monkeypatch
):
    monkeypatch.setattr(sp, "build_training_frame", lambda root: dec_2026_frame)
    out = tmp_path / "parity" / "out.json"
    rc = score_cli.main([
        "--root", str(spark_root.root), "--out", str(out), "--universe-json", str(universe_json),
    ])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "ts_utc" not in payload
    assert payload["identity"]["snapshot"]["sweep_count"] == 1
    assert not (spark_root.root / "climate_data" / "paper").exists()
