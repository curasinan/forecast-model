"""analysis/spark_parity.py: mirror checks, identity, scorer, compare.

Design: docs/plans/2026-09-30-spark-research-split-design.md section 5.
No network, no real data: the spark_root fixture (conftest) builds a
synthetic quant-data root in tmp_path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import analysis.spark_parity as sp

# --------------------------------------------------------------------------- #
# check_parquet / check_sqlite
# --------------------------------------------------------------------------- #


def test_check_parquet_accepts_a_complete_file(spark_root):
    assert sp.check_parquet(spark_root.sweep) is None


def test_check_parquet_names_a_truncated_file(spark_root):
    data = spark_root.sweep.read_bytes()
    spark_root.sweep.write_bytes(data[: len(data) // 2])
    err = sp.check_parquet(spark_root.sweep)
    assert err  # non-empty error text


def test_check_sqlite_ok_then_truncated(tmp_path):
    db = tmp_path / "paper.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(2000)])
    conn.commit()
    conn.close()
    assert sp.check_sqlite(db) is None
    db.write_bytes(db.read_bytes()[:1024])
    assert sp.check_sqlite(db)


def test_check_sqlite_rejects_a_non_database_file(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(bytes(range(256)))
    assert sp.check_sqlite(junk)


# --------------------------------------------------------------------------- #
# verify_mirror
# --------------------------------------------------------------------------- #


def test_verify_mirror_clean_root_has_no_failures(spark_root):
    assert sp.verify_mirror(spark_root.root) == []


def test_verify_mirror_lists_every_broken_file(spark_root):
    spark_root.sweep.write_bytes(bytes(range(256)))
    spark_root.model.write_text("{not json", encoding="utf-8")
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 2
    assert any("quotes-220822Z.parquet" in f for f in failures)
    assert any("gtm_emos.json" in f for f in failures)


def test_verify_mirror_reports_missing_required_files(tmp_path):
    root = tmp_path / "empty"
    (root / "ibkr_ticks").mkdir(parents=True)
    (root / "climate_data").mkdir()
    failures = sp.verify_mirror(root)
    assert any("no sweep parquet" in f for f in failures)
    assert any("labels_ledger.parquet: missing" in f for f in failures)
    assert any("gtm_emos.json: missing" in f for f in failures)


def test_verify_mirror_checks_optional_databases_when_present(spark_root):
    paper_db = spark_root.root / "ibkr_ticks" / "paper" / "ibkr_paper.db"
    paper_db.parent.mkdir(parents=True)
    paper_db.write_bytes(bytes(range(256)))
    cache_db = spark_root.root / "ibkr_ticks" / "contracts_cache.db"
    cache_db.write_bytes(bytes(range(256)))
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 2
    assert any("ibkr_paper.db" in f for f in failures)
    assert any("contracts_cache.db" in f for f in failures)


# --------------------------------------------------------------------------- #
# snapshot_identity
# --------------------------------------------------------------------------- #


def test_snapshot_identity_describes_the_files(spark_root):
    ident = sp.snapshot_identity(spark_root.root)
    assert ident["newest_sweep"] == "ibkr_ticks/quotes/date=2026-09-28/quotes-220822Z.parquet"
    assert ident["sweep_count"] == 1
    assert ident["newest_hourly"] is None and ident["hourly_count"] == 0
    assert ident["labels_rows"] == 1
    assert ident["labels_max_period"] == "2026-07"
    assert ident["labels_max_first_seen_utc"] == "2026-08-15T00:00:00Z"
    assert len(ident["model_fingerprint"]) >= 8
    assert len(ident["newest_sweep_sha256"]) == 64
    assert len(ident["labels_sha256"]) == 64
    assert ident["paper_db_quick_check"] is None
    assert ident["paper_runs_lines"] is None


def test_snapshot_identity_counts_paper_runs_and_checks_the_db(spark_root):
    runs = spark_root.root / "climate_data" / "paper" / "ibkr_paper_runs.jsonl"
    runs.parent.mkdir(parents=True)
    runs.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    db = spark_root.root / "ibkr_ticks" / "paper" / "ibkr_paper.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    ident = sp.snapshot_identity(spark_root.root)
    assert ident["paper_runs_lines"] == 2
    assert ident["paper_db_quick_check"] == "ok"


def test_snapshot_identity_round_trips_through_json(spark_root):
    ident = sp.snapshot_identity(spark_root.root)
    assert json.loads(json.dumps(ident, sort_keys=True)) == ident


def test_identity_hashes_match_hashlib_and_change_on_a_byte_flip(spark_root):
    before = sp.snapshot_identity(spark_root.root)
    assert before["newest_sweep_sha256"] == hashlib.sha256(spark_root.sweep.read_bytes()).hexdigest()
    assert before["labels_sha256"] == hashlib.sha256(spark_root.ledger.read_bytes()).hexdigest()
    data = bytearray(spark_root.sweep.read_bytes())
    data[len(data) // 2] ^= 0xFF
    spark_root.sweep.write_bytes(bytes(data))
    after = sp.snapshot_identity(spark_root.root)
    assert after["newest_sweep_sha256"] != before["newest_sweep_sha256"]
    assert after["labels_sha256"] == before["labels_sha256"]


def test_verify_and_identity_use_the_newest_sweep_and_hourly(
    spark_root, sweep_writer, hourly_writer
):
    newest_sweep = sweep_writer(spark_root.root, "2026-09-29", "101010")
    old_hourly = hourly_writer(spark_root.root, "2026-09-27", "bars-a")
    newest_hourly = hourly_writer(spark_root.root, "2026-09-28", "bars-b")
    ident = sp.snapshot_identity(spark_root.root)
    assert ident["newest_sweep"].endswith("date=2026-09-29/quotes-101010Z.parquet")
    assert ident["sweep_count"] == 2
    assert ident["newest_hourly"].endswith("date=2026-09-28/bars-b.parquet")
    assert ident["hourly_count"] == 2
    # a torn OLDER file is not this check's business; a torn NEWEST one is
    spark_root.sweep.write_bytes(bytes(range(256)))
    old_hourly.write_bytes(bytes(range(256)))
    assert sp.verify_mirror(spark_root.root) == []
    newest_sweep.write_bytes(bytes(range(256)))
    newest_hourly.write_bytes(bytes(range(256)))
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 2
    assert any("quotes-101010Z.parquet" in f for f in failures)
    assert any("bars-b.parquet" in f for f in failures)


def test_verify_mirror_requires_the_cds_inputs(spark_root):
    spark_root.seas5.unlink()
    spark_root.era5.write_bytes(bytes(range(256)))
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 2
    assert any("seas5_global_mean.parquet: missing" in f for f in failures)
    assert any("era5_global_mean.parquet" in f and "missing" not in f for f in failures)


def test_inputs_sha256_lists_every_input_present_or_not(spark_root):
    hashes = sp.inputs_sha256(spark_root.root)
    assert set(hashes) == {"/".join(p) for p in sp.INPUT_FILES}
    expected = hashlib.sha256(spark_root.model.read_bytes()).hexdigest()
    assert hashes["climate_data/models/gtm_emos.json"] == expected
    assert hashes["ibkr_ticks/contracts_cache.db"] is None
    assert sp.snapshot_identity(spark_root.root)["inputs_sha256"] == hashes


def test_code_identity_reports_the_commit_and_survives_a_non_repo(spark_root, tmp_path):
    here = sp.code_identity()
    assert len(here["git_sha"]) == 40
    assert isinstance(here["dirty"], bool)
    assert sp.snapshot_identity(spark_root.root)["code_sha"] == here["git_sha"]
    assert sp.code_identity(tmp_path) == {"git_sha": "unknown", "dirty": None}


def test_operational_manifest_lists_files_and_tracks_changes(spark_root):
    manifest = sp.operational_manifest(spark_root.root)
    assert set(manifest) == {"/".join(p) for p in sp.MANIFEST_FILES}
    entry = manifest["climate_data/models/gtm_emos.json"]
    assert set(entry) == {"sha256", "size", "mtime_ns"}
    assert entry["size"] == spark_root.model.stat().st_size
    assert manifest["ibkr_ticks/paper/ibkr_paper.db"] is None
    text = spark_root.model.read_text(encoding="utf-8")
    spark_root.model.write_text(text + "\n", encoding="utf-8")
    changed = sp.operational_manifest(spark_root.root)
    assert changed["climate_data/models/gtm_emos.json"]["sha256"] != entry["sha256"]
    assert {k for k in manifest if manifest[k] != changed[k]} == {"climate_data/models/gtm_emos.json"}
