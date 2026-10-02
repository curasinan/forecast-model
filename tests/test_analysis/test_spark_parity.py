"""analysis/spark_parity.py: mirror checks, identity, scorer, compare.

Design: docs/plans/2026-09-30-spark-research-split-design.md section 5.
No network, no real data: the spark_root fixture (conftest) builds a
synthetic quant-data root in tmp_path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import analysis.spark_parity as sp
from climate.storage import ledger_path

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


def _tree_listing(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def test_check_sqlite_survives_a_hash_in_the_path_and_creates_nothing(tmp_path):
    # An unescaped '#' ends the SQLite URI path, so the old probe opened (and
    # created) a stray file named "a" next to the folder and reported a false ok.
    folder = tmp_path / "a#b"
    folder.mkdir()
    db = folder / "paper.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    before = _tree_listing(tmp_path)
    assert sp.check_sqlite(db) is None
    assert _tree_listing(tmp_path) == before


def test_check_sqlite_rejects_an_empty_file(tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert sp.check_sqlite(empty) == "empty file (0 bytes)"


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


def test_verify_mirror_reports_a_non_object_model_instead_of_aborting(spark_root):
    # json.loads("[]") succeeds and EmosModel.from_dict then raises AttributeError,
    # which the old narrow catch let escape and abort the whole scan.
    spark_root.model.write_text("[]", encoding="utf-8")
    failures = sp.verify_mirror(spark_root.root)
    assert len(failures) == 1
    assert failures[0].startswith("climate_data/models/gtm_emos.json: ")


def test_verify_mirror_reports_missing_required_files(tmp_path):
    root = tmp_path / "empty"
    (root / "ibkr_ticks").mkdir(parents=True)
    (root / "climate_data").mkdir()
    failures = sp.verify_mirror(root)
    assert len(failures) == 5
    assert any("no sweep parquet" in f for f in failures)
    assert any("seas5_global_mean.parquet: missing" in f for f in failures)
    assert any("era5_global_mean.parquet: missing" in f for f in failures)
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


def test_ledger_rel_is_the_owners_path():
    # LEDGER_REL is the one hand-written path constant (the others are imported
    # from climate.reduce / climate.fit): pin it to climate.storage's own path.
    assert sp.LEDGER_REL == ledger_path(Path("x")).relative_to(Path("x")).parts


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


# --------------------------------------------------------------------------- #
# compare_json
# --------------------------------------------------------------------------- #


def test_compare_identical_documents_ok():
    doc = {"x": 1.0, "s": "a", "l": [1, 2, {"k": True}], "n": None}
    res = sp.compare_json(doc, json.loads(json.dumps(doc)))
    assert res.ok
    assert res.n_numeric == 3
    assert res.max_abs == 0.0


def test_compare_within_tolerance_records_max_diffs():
    res = sp.compare_json({"x": 1.0}, {"x": 1.0 + 1e-12})
    assert res.ok
    assert 0.9e-12 < res.max_abs < 1.1e-12
    assert 0.9e-12 < res.max_rel < 1.1e-12
    # near zero rtol*scale vanishes, so only atol can accept this pair
    assert sp.compare_json({"x": 0.0}, {"x": 1e-13}).ok


def test_compare_beyond_tolerance_fails_with_the_path():
    res = sp.compare_json({"a": {"b": [0.5, 0.25]}}, {"a": {"b": [0.5, 0.2500001]}})
    assert not res.ok
    assert res.mismatches[0].startswith("a.b[1]:")
    # and a near-zero pair that is past atol is rejected
    assert not sp.compare_json({"x": 0.0}, {"x": 1e-11}).ok


def test_compare_nan_equals_nan_but_none_does_not():
    assert sp.compare_json({"v": float("nan")}, {"v": float("nan")}).ok
    res = sp.compare_json({"v": None}, {"v": float("nan")})
    assert not res.ok
    assert res.mismatches[0].startswith("v:")
    # a NaN or an infinity against anything else must fail loudly: without the
    # guard both |a-b| and the scale go NaN/inf and ``nan > nan`` is False
    nan, inf = float("nan"), float("inf")
    assert not sp.compare_json({"v": nan}, {"v": 1.0}).ok
    assert not sp.compare_json({"v": 1.0}, {"v": nan}).ok
    assert not sp.compare_json({"v": inf}, {"v": 1.0}).ok
    assert not sp.compare_json({"v": inf}, {"v": -inf}).ok
    assert sp.compare_json({"v": inf}, {"v": inf}).ok


def test_compare_missing_extra_keys_and_list_lengths():
    res = sp.compare_json({"a": 1, "b": 2, "l": [1]}, {"a": 1, "c": 3, "l": [1, 2]})
    assert {m.split(":")[0] for m in res.mismatches} == {"b", "c", "l"}
    assert "b: missing on right" in res.mismatches
    assert "c: missing on left" in res.mismatches


def test_compare_ignore_skips_a_dotted_subtree():
    left = {"identity": {"snapshot": {"n": 1}, "machine": {"python": "3.14.2"}}}
    right = {"identity": {"snapshot": {"n": 1}, "machine": {"python": "3.12.3"}}}
    assert not sp.compare_json(left, right).ok
    assert sp.compare_json(left, right, ignore=["identity.machine"]).ok


def test_compare_bool_and_int_are_distinct_but_int_and_float_are_numbers():
    assert not sp.compare_json({"t": True}, {"t": 1}).ok
    assert sp.compare_json({"t": 1}, {"t": 1.0}).ok
    # int against float still goes through the tolerance (only int-int is exact)
    assert sp.compare_json({"t": 1}, {"t": 1.0 + 1e-12}).ok
    # two ints are exact (design 5.3): at the gate's tolerances an id off by 3
    # (or by 1, 5, 8 at 8e8) must fail, never hide inside rtol * scale
    gate = {"rtol": 1e-8, "atol": 1e-10}
    res = sp.compare_json({"i": 812345678}, {"i": 812345681}, **gate)
    assert not res.ok
    assert res.mismatches[0].startswith("i:")
    assert res.n_numeric == 1
    assert sp.compare_json({"i": 812345678}, {"i": 812345678}, **gate).ok
    for delta in (1, 5, 8):
        assert not sp.compare_json({"i": 800_000_000}, {"i": 800_000_000 + delta}, **gate).ok
