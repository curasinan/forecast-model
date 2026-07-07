"""Tests for collect_llm_scores: payload validation, divergence math, resumability.

Synthetic only — no live Anthropic or Gamma calls.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collectors.collect_llm_scores import (
    _validate_score_payload,
    build_user_prompt,
    get_base_rate_divergence,
)

GOOD = {
    "probability": 0.35,
    "confidence": "medium",
    "reasoning": "Base rates for such deals are low. Talks have stalled.",
    "recognized_outcome": False,
    "category": "diplomatic_summit",
    "search_keywords": ["Iran nuclear deal", "IAEA"],
}


# --------------------------------------------------------------------------- #
# Payload validation
# --------------------------------------------------------------------------- #


def test_valid_payload_normalized():
    out = _validate_score_payload(dict(GOOD))
    assert out is not None
    assert out["probability"] == pytest.approx(0.35)
    assert out["confidence"] == "medium"
    assert out["category"] == "diplomatic_summit"
    assert out["recognized_outcome"] == 0
    assert out["search_keywords"] == ["Iran nuclear deal", "IAEA"]


def test_probability_clamped():
    assert _validate_score_payload({**GOOD, "probability": 1.7})["probability"] == 1.0
    assert _validate_score_payload({**GOOD, "probability": -0.2})["probability"] == 0.0


def test_bad_enum_rejected():
    assert _validate_score_payload({**GOOD, "confidence": "very high"}) is None
    assert _validate_score_payload({**GOOD, "category": "sports"}) is None


def test_keywords_truncated_to_four():
    out = _validate_score_payload({**GOOD, "search_keywords": ["a", "b", "c", "d", "e"]})
    assert out["search_keywords"] == ["a", "b", "c", "d"]


def test_empty_keywords_rejected():
    assert _validate_score_payload({**GOOD, "search_keywords": []}) is None
    assert _validate_score_payload({**GOOD, "search_keywords": "not a list"}) is None


def test_garbage_rejected():
    assert _validate_score_payload(None) is None
    assert _validate_score_payload("no json here at all") is None
    assert _validate_score_payload({"probability": "n/a"}) is None


def test_json_extracted_from_string_with_fences():
    text = "```json\n" + json.dumps(GOOD) + "\n```"
    out = _validate_score_payload(text)
    assert out is not None and out["probability"] == pytest.approx(0.35)


def test_json_extracted_from_prose_wrapper():
    text = "Here is my estimate:\n" + json.dumps(GOOD) + "\nHope that helps."
    out = _validate_score_payload(text)
    assert out is not None and out["category"] == "diplomatic_summit"


def test_recognized_outcome_true_coerced():
    out = _validate_score_payload({**GOOD, "recognized_outcome": True})
    assert out["recognized_outcome"] == 1


# --------------------------------------------------------------------------- #
# Prompt builder never receives market data (constraint by construction)
# --------------------------------------------------------------------------- #


def test_user_prompt_contains_only_question_fields():
    prompt = build_user_prompt("Will X happen?", "Resolution: yes if X.", "2026-12-31")
    assert "Will X happen?" in prompt
    assert "Resolution: yes if X." in prompt
    assert "2026-12-31" in prompt
    for forbidden in ("price", "midpoint", "odds", "volume", "liquidity"):
        assert forbidden not in prompt.lower()


# --------------------------------------------------------------------------- #
# Base-rate divergence
# --------------------------------------------------------------------------- #


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE markets (id TEXT PRIMARY KEY, status TEXT)")
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
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    def add(mid, category, midpoint, retrospective=0):
        conn.execute("INSERT INTO markets VALUES (?, 'active')", (mid,))
        if midpoint is not None:
            conn.execute(
                "INSERT INTO snapshots (market_id, timestamp, midpoint) VALUES (?, ?, ?)",
                (mid, now.isoformat(), midpoint),
            )
        if category is not None:
            conn.execute(
                "INSERT INTO llm_scores (market_id, scored_at, model_name, probability, "
                "confidence, reasoning, recognized_outcome, category, search_keywords, "
                "prompt_version, retrospective) VALUES (?, ?, 'test', 0.5, 'low', '', 0, "
                "?, '[]', 'v1', ?)",
                (mid, now.isoformat(), category, retrospective),
            )

    add("m1", "nomination_confirmation", 0.70)   # base 0.875 -> div = -0.175
    add("m2", "election_open", 0.40)             # base null -> div null
    add("m3", None, 0.25)                        # no score -> nulls
    add("m4", "legislation", None)               # no snapshot -> div null
    add("m5", "judicial", 0.10, retrospective=1)  # retrospective-only -> treated as unscored
    conn.commit()
    conn.close()
    return db


def test_divergence_math(tmp_path):
    import pandas as pd

    db = _make_db(tmp_path)
    df = get_base_rate_divergence(["m1", "m2", "m3", "m4", "m5"], db)
    by_id = {r["market_id"]: r for _, r in df.iterrows()}

    assert by_id["m1"]["base_rate"] == pytest.approx(0.875)
    assert by_id["m1"]["divergence"] == pytest.approx(0.70 - 0.875)

    # Nulls arrive as NaN in numeric DataFrame columns (pandas convention).
    assert by_id["m2"]["category"] == "election_open"
    assert pd.isna(by_id["m2"]["base_rate"]) and pd.isna(by_id["m2"]["divergence"])

    assert pd.isna(by_id["m3"]["category"]) and pd.isna(by_id["m3"]["divergence"])

    assert by_id["m4"]["base_rate"] == pytest.approx(0.03)
    assert pd.isna(by_id["m4"]["current_midpoint"]) and pd.isna(by_id["m4"]["divergence"])

    # Retrospective scores must not feed the divergence helper.
    assert pd.isna(by_id["m5"]["category"])


def test_divergence_empty_input(tmp_path):
    db = _make_db(tmp_path)
    df = get_base_rate_divergence([], db)
    assert df.empty


# --------------------------------------------------------------------------- #
# Resumability window semantics (raw query behavior)
# --------------------------------------------------------------------------- #


def test_recent_scores_block_rescoring_but_retrospective_does_not(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    cutoff = (now - timedelta(days=7)).isoformat()

    # m1 scored now (recent, prospective) -> blocked.
    # m5 scored now but retrospective -> NOT blocked.
    # add m6 scored 10 days ago -> NOT blocked.
    conn.execute(
        "INSERT INTO llm_scores (market_id, scored_at, model_name, probability, "
        "confidence, reasoning, recognized_outcome, category, search_keywords, "
        "prompt_version, retrospective) VALUES ('m6', ?, 'test', 0.5, 'low', '', 0, "
        "'other', '[]', 'v1', 0)",
        ((now - timedelta(days=10)).isoformat(),),
    )
    conn.commit()
    blocked = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT market_id FROM llm_scores "
            "WHERE retrospective = 0 AND scored_at > ?", (cutoff,),
        ).fetchall()
    }
    conn.close()
    assert "m1" in blocked
    assert "m5" not in blocked   # retrospective doesn't block
    assert "m6" not in blocked   # stale score doesn't block
