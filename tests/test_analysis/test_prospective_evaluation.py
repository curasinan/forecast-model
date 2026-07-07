"""Tests for the pre-registered prospective evaluation harness.

Covers: contemporaneous-price join, disagreement/winner logic, contamination
guards, the n>=40 refusal, status-mode blinding, and the Wilson interval.
Synthetic DB only.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import analysis.prospective_evaluation as pe

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE markets (id TEXT PRIMARY KEY, question TEXT, status TEXT, "
        "resolution TEXT, last_updated TEXT)"
    )
    conn.execute(
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, timestamp TEXT, midpoint REAL)"
    )
    conn.execute(
        "CREATE TABLE llm_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, scored_at TEXT, model_name TEXT, probability REAL, "
        "confidence TEXT, reasoning TEXT, recognized_outcome INTEGER, "
        "category TEXT, search_keywords TEXT, prompt_version TEXT, "
        "retrospective INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE news_volume (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, collected_at TEXT, articles_72h INTEGER, "
        "articles_30d INTEGER, spike_ratio REAL)"
    )
    conn.commit()
    conn.close()
    return db


def _add_market(db, mid, resolution="1", question="Will X happen?"):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO markets VALUES (?, ?, 'resolved_or_removed', ?, ?)",
                 (mid, question, resolution, T0.isoformat()))
    conn.commit()
    conn.close()


def _add_snapshot(db, mid, hours_from_t0, midpoint):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO snapshots (market_id, timestamp, midpoint) VALUES (?, ?, ?)",
        (mid, (T0 + timedelta(hours=hours_from_t0)).isoformat(), midpoint),
    )
    conn.commit()
    conn.close()


def _add_score(db, mid, hours_from_t0, probability, recognized=0,
               confidence="medium", category="other", retrospective=0):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO llm_scores (market_id, scored_at, model_name, probability, "
        "confidence, reasoning, recognized_outcome, category, search_keywords, "
        "prompt_version, retrospective) VALUES (?, ?, 'test', ?, ?, '', ?, ?, "
        "'[]', 'v1', ?)",
        (mid, (T0 + timedelta(hours=hours_from_t0)).isoformat(), probability,
         confidence, recognized, category, retrospective),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Contemporaneous-price join
# --------------------------------------------------------------------------- #


def test_nearest_snapshot_join_direction_agnostic(tmp_path):
    db = _make_db(tmp_path)
    _add_market(db, "m1")
    _add_snapshot(db, "m1", -2.0, 0.30)    # 2h before scoring
    _add_snapshot(db, "m1", +0.5, 0.40)    # 30min after scoring  <- nearest
    _add_snapshot(db, "m1", +6.0, 0.90)    # 6h after
    _add_score(db, "m1", 0.0, 0.55)

    rows = pe.load_eligible_rows(db)
    assert len(rows) == 1
    assert rows[0]["market_price_at_scoring"] == pytest.approx(0.40)
    assert rows[0]["price_snapshot_gap_minutes"] == pytest.approx(30.0, abs=0.1)


def test_earliest_score_selected(tmp_path):
    db = _make_db(tmp_path)
    _add_market(db, "m1")
    _add_snapshot(db, "m1", 0.0, 0.50)
    _add_snapshot(db, "m1", 50.0, 0.50)
    _add_score(db, "m1", 1.0, 0.20)   # earliest -> used
    _add_score(db, "m1", 48.0, 0.90)  # re-score -> ignored
    rows = pe.load_eligible_rows(db)
    assert len(rows) == 1
    assert rows[0]["llm_probability"] == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# Disagreement + winner logic
# --------------------------------------------------------------------------- #


def test_disagreement_and_winner(tmp_path):
    db = _make_db(tmp_path)
    # LLM wins: p=0.80, price=0.60, resolved YES.
    _add_market(db, "w", resolution="1")
    _add_snapshot(db, "w", 0.0, 0.60)
    _add_snapshot(db, "w", 24.0, 0.60)
    _add_score(db, "w", 0.0, 0.80)
    # Market wins: p=0.80, price=0.60, resolved NO.
    _add_market(db, "l", resolution="0")
    _add_snapshot(db, "l", 0.0, 0.60)
    _add_snapshot(db, "l", 24.0, 0.60)
    _add_score(db, "l", 0.0, 0.80)
    # No disagreement: |0.65 - 0.60| = 0.05 <= 0.10.
    _add_market(db, "n", resolution="1")
    _add_snapshot(db, "n", 0.0, 0.60)
    _add_snapshot(db, "n", 24.0, 0.60)
    _add_score(db, "n", 0.0, 0.65)
    # Boundary: exactly 0.10 apart is NOT a disagreement (strict >).
    _add_market(db, "b", resolution="1")
    _add_snapshot(db, "b", 0.0, 0.60)
    _add_snapshot(db, "b", 24.0, 0.60)
    _add_score(db, "b", 0.0, 0.70)

    rows = {r["market_id"]: r for r in pe.load_eligible_rows(db)}
    assert rows["w"]["disagreement"] and rows["w"]["winner"] == "llm"
    assert rows["l"]["disagreement"] and rows["l"]["winner"] == "market"
    assert not rows["n"]["disagreement"] and rows["n"]["winner"] == ""
    assert not rows["b"]["disagreement"]

    stats = pe.edge_stats(list(rows.values()))
    assert stats["n_markets"] == 4
    assert stats["n_disagreements"] == 2
    assert stats["llm_wins"] == 1
    assert stats["llm_win_rate"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Contamination guards
# --------------------------------------------------------------------------- #


def test_recognized_outcome_scores_dropped_before_earliest_pick(tmp_path):
    db = _make_db(tmp_path)
    _add_market(db, "m1")
    _add_snapshot(db, "m1", 0.0, 0.50)
    _add_snapshot(db, "m1", 30.0, 0.50)
    _add_score(db, "m1", 1.0, 0.10, recognized=1)   # contaminated earliest
    _add_score(db, "m1", 24.0, 0.90, recognized=0)  # clean later score -> used
    rows = pe.load_eligible_rows(db)
    assert len(rows) == 1
    assert rows[0]["llm_probability"] == pytest.approx(0.90)

    # All scores contaminated -> market excluded entirely.
    _add_market(db, "m2")
    _add_snapshot(db, "m2", 0.0, 0.50)
    _add_score(db, "m2", 1.0, 0.10, recognized=1)
    rows = pe.load_eligible_rows(db)
    assert {r["market_id"] for r in rows} == {"m1"}


def test_scored_after_final_snapshot_excluded_unless_skipped(tmp_path):
    db = _make_db(tmp_path)
    _add_market(db, "m1")
    _add_snapshot(db, "m1", 0.0, 0.50)   # final snapshot at T0
    _add_score(db, "m1", 5.0, 0.80)      # scored 5h AFTER final snapshot
    assert pe.load_eligible_rows(db) == []
    rows = pe.load_eligible_rows(db, skip_final_snapshot_guard=True)
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# n >= 40 refusal
# --------------------------------------------------------------------------- #


def test_verdict_refuses_below_gate(tmp_path, monkeypatch, caplog):
    db = _make_db(tmp_path)
    for i in range(5):
        mid = f"m{i}"
        _add_market(db, mid)
        _add_snapshot(db, mid, 0.0, 0.50)
        _add_snapshot(db, mid, 24.0, 0.50)
        _add_score(db, mid, 0.0, 0.80)

    fake_json = tmp_path / "verdict.json"
    fake_csv = tmp_path / "per_market.csv"
    monkeypatch.setattr(pe, "VERDICT_JSON_PATH", fake_json)
    monkeypatch.setattr(pe, "VERDICT_CSV_PATH", fake_csv)

    with caplog.at_level(logging.ERROR, logger="analysis.prospective_evaluation"):
        rc = pe.run_verdict(db, smoke=False)

    assert rc == 1
    assert not fake_json.exists() and not fake_csv.exists()
    joined = " ".join(r.message for r in caplog.records)
    assert "5" in joined and "40" in joined and "REFUSING" in joined


def test_verdict_runs_at_gate(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    for i in range(40):
        mid = f"m{i}"
        _add_market(db, mid, resolution="1" if i % 2 else "0")
        _add_snapshot(db, mid, 0.0, 0.50)
        _add_snapshot(db, mid, 24.0, 0.50)
        _add_score(db, mid, 0.0, 0.80)

    fake_json = tmp_path / "verdict.json"
    fake_csv = tmp_path / "per_market.csv"
    monkeypatch.setattr(pe, "VERDICT_JSON_PATH", fake_json)
    monkeypatch.setattr(pe, "VERDICT_CSV_PATH", fake_csv)

    rc = pe.run_verdict(db, smoke=False)
    assert rc == 0
    assert fake_json.exists() and fake_csv.exists()
    import json as _json
    payload = _json.loads(fake_json.read_text(encoding="utf-8"))
    assert payload["verdict"] in ("politics_open", "politics_closed")
    assert payload["preregistered_config"]["win_rate_gate"] == 0.55
    # p=0.80 vs price=0.50, half resolve YES -> win rate 0.5 < 0.55 -> closed.
    assert payload["verdict"] == "politics_closed"


# --------------------------------------------------------------------------- #
# Status-mode blinding
# --------------------------------------------------------------------------- #


FORBIDDEN_IN_STATUS = ("win", "edge", "closer", "rate", "%")


def test_status_output_is_blinded(tmp_path, monkeypatch, caplog):
    db = _make_db(tmp_path)
    _add_market(db, "m1")
    _add_snapshot(db, "m1", 0.0, 0.50)
    _add_snapshot(db, "m1", 24.0, 0.50)
    _add_score(db, "m1", 0.0, 0.80)

    # Status must never even CALL the directional-stats function.
    def _boom(*a, **k):
        raise AssertionError("edge_stats called from status mode")
    monkeypatch.setattr(pe, "edge_stats", _boom)

    with caplog.at_level(logging.INFO, logger="analysis.prospective_evaluation"):
        result = pe.run_status(db)

    assert result["eligible"] == 1
    output = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "1" in output  # the count is reported
    for token in FORBIDDEN_IN_STATUS:
        assert token not in output, f"blinding violated: {token!r} in status output"


# --------------------------------------------------------------------------- #
# Wilson interval
# --------------------------------------------------------------------------- #


def test_wilson_interval_known_value():
    lo, hi = pe.wilson_interval(30, 40)
    assert lo == pytest.approx(0.598, abs=0.01)
    assert hi == pytest.approx(0.858, abs=0.01)


def test_wilson_interval_degenerate():
    assert pe.wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = pe.wilson_interval(0, 10)
    assert lo == 0.0 and hi < 0.35
    lo, hi = pe.wilson_interval(10, 10)
    assert hi == 1.0 and lo > 0.65
