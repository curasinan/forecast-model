"""Tests for model.train_outcome dataset assembly + 48h-before extraction.

Synthetic-only — no live DB or API.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from features.market_features import compute_features_batch
from model.train_outcome import (
    REFERENCE_HOURS_BEFORE,
    compute_reference_timestamps,
    load_candidate_market_ids,
)

BASE = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def _make_db(tmp_path: Path) -> Path:
    """Synthetic DB with the columns the queries + feature pipeline touch."""
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE markets (id TEXT PRIMARY KEY, question TEXT, status TEXT, "
        "resolution TEXT, end_date TEXT)"
    )
    conn.execute(
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, timestamp TEXT, best_bid REAL, best_ask REAL, "
        "bid_depth REAL, ask_depth REAL, midpoint REAL, spread REAL, "
        "last_price REAL, volume_24h REAL)"
    )

    def add_market(mid, question, status, resolution, hours_of_snaps, n_snaps,
                   ramp=(0.30, 0.99)):
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?)",
            (mid, question, status, resolution,
             (BASE + timedelta(days=30)).isoformat()),
        )
        # n_snaps evenly spaced over hours_of_snaps, midpoint ramping
        # linearly from ramp[0] to ramp[1] (simulates a market pinning).
        for i in range(n_snaps):
            frac = i / max(n_snaps - 1, 1)
            ts = BASE + timedelta(hours=frac * hours_of_snaps)
            mid_price = ramp[0] + frac * (ramp[1] - ramp[0])
            conn.execute(
                "INSERT INTO snapshots(market_id, timestamp, best_bid, best_ask, "
                "bid_depth, ask_depth, midpoint, spread, last_price, volume_24h) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, ts.isoformat(), mid_price - 0.005, mid_price + 0.005,
                 1000.0, 1000.0, mid_price, 0.01, mid_price, 5000.0),
            )

    # Political, resolved, backfilled, plenty of snapshots over 72h -> candidate
    add_market("pol1", "Will Trump win the election?", "resolved_or_removed",
               "1", hours_of_snaps=72, n_snaps=150)
    # Political, resolved, backfilled, but only 24h of history -> dropped by 48h rule
    add_market("pol2", "Will the ceasefire hold in Gaza?", "resolved_or_removed",
               "0", hours_of_snaps=24, n_snaps=120)
    # Political, resolved, NO resolution -> excluded by candidate query
    add_market("pol3", "Will Congress pass the bill on sanctions?",
               "resolved_or_removed", None, hours_of_snaps=72, n_snaps=150)
    # Political but still active -> excluded
    add_market("pol4", "Will Putin attend the summit?", "active", None,
               hours_of_snaps=72, n_snaps=150)
    # Non-political, resolved, backfilled -> excluded by regex
    add_market("sport1", "Will the Lakers beat the Celtics tonight?",
               "resolved_or_removed", "1", hours_of_snaps=72, n_snaps=150)
    # Political, resolved, backfilled, but too few snapshots -> excluded
    add_market("pol5", "Will Iran agree to the treaty?", "resolved_or_removed",
               "0", hours_of_snaps=72, n_snaps=10)

    conn.commit()
    conn.close()
    return db


# --------------------------------------------------------------------------- #
# Candidate / label assembly
# --------------------------------------------------------------------------- #


def test_candidate_query_filters_correctly(tmp_path):
    db = _make_db(tmp_path)
    ids = load_candidate_market_ids(db, min_snapshots=100)
    # pol1: qualifies. pol2: qualifies at the candidate stage (span filter is
    # a later step). pol3: no resolution. pol4: active. sport1: not political.
    # pol5: too few snapshots.
    assert sorted(ids) == ["pol1", "pol2"]


def test_outcome_labels_integration(tmp_path):
    from features.labels import generate_outcome_labels
    db = _make_db(tmp_path)
    labels = generate_outcome_labels(["pol1", "pol2", "pol3", "sport1"], db)
    got = dict(zip(labels["market_id"], labels["outcome"]))
    assert got == {"pol1": 1, "pol2": 0, "sport1": 1}  # pol3 has NULL resolution


# --------------------------------------------------------------------------- #
# 48h-before reference extraction
# --------------------------------------------------------------------------- #


def test_reference_timestamp_is_48h_before_last_snapshot(tmp_path):
    db = _make_db(tmp_path)
    refs, n_dropped = compute_reference_timestamps(db, ["pol1", "pol2"])
    # pol1 spans 72h -> survives; T_ref = last - 48h = BASE + 24h.
    assert "pol1" in refs
    expected = BASE + timedelta(hours=72) - timedelta(hours=REFERENCE_HOURS_BEFORE)
    assert refs["pol1"] == expected
    # pol2 spans only 24h -> first_snapshot > T_ref -> dropped.
    assert "pol2" not in refs
    assert n_dropped == 1


def test_features_at_reference_do_not_see_the_pin(tmp_path):
    """The anti-leakage property: price_now at T_ref reflects the market
    ~24h into its 72h life (midpoint ramp 0.30 -> 0.99), NOT the final pin."""
    db = _make_db(tmp_path)
    refs, _ = compute_reference_timestamps(db, ["pol1"])
    t_ref = refs["pol1"]

    feats = compute_features_batch(["pol1"], t_ref, db).iloc[0]
    price_at_ref = float(feats["price_now"])

    # At 24h of 72h the linear ramp 0.30->0.99 sits at ~0.53; the causal
    # lookup takes the latest snapshot <= T_ref so allow a small tolerance.
    assert 0.45 <= price_at_ref <= 0.56, price_at_ref
    # And it must be far from the pinned final value.
    assert price_at_ref < 0.90
