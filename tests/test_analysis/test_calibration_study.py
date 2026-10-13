"""Tests for analysis.calibration_study binning, edges, and reference extraction."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from analysis.calibration_study import (
    BIN_EDGES,
    assign_bin,
    bin_table,
    extract_reference_rows,
    tradeability_table,
    tte_bucket_label,
)

BASE = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


# Binning


def test_bin_assignment_boundaries():
    assert assign_bin(0.0) == 0          # first bin [0.00, 0.02)
    assert assign_bin(0.019) == 0
    assert assign_bin(0.02) == 1         # boundary goes to the upper bin
    assert assign_bin(0.5) == 5          # [0.40, 0.60)
    assert assign_bin(0.98) == 10        # [0.98, 1.00]
    assert assign_bin(1.0) == 10         # last bin includes 1.0
    assert len(BIN_EDGES) - 2 == 10


def test_bin_table_rates_and_gap():
    # 4 markets in the [0.02, 0.05) bin: priced ~0.03, 1 of 4 resolved YES.
    rows = [
        {"price": 0.03, "outcome": 1, "spread": 0.02},
        {"price": 0.03, "outcome": 0, "spread": 0.02},
        {"price": 0.03, "outcome": 0, "spread": 0.02},
        {"price": 0.03, "outcome": 0, "spread": 0.02},
        # one market in the top bin, resolved YES
        {"price": 0.99, "outcome": 1, "spread": 0.01},
    ]
    bins = bin_table(rows)
    low = next(b for b in bins if b["bin"] == "0.02-0.05")
    assert low["n"] == 4
    assert low["avg_price"] == pytest.approx(0.03)
    assert low["actual_yes_rate"] == pytest.approx(0.25)
    assert low["gap"] == pytest.approx(0.22)
    assert low["avg_spread"] == pytest.approx(0.02)

    top = next(b for b in bins if b["bin"] == "0.98-1.00")
    assert top["n"] == 1
    assert top["actual_yes_rate"] == pytest.approx(1.0)

    # Empty bins present with n=0 and None stats
    empty = next(b for b in bins if b["bin"] == "0.40-0.60")
    assert empty["n"] == 0 and empty["avg_price"] is None

    # Sum of bin n == total rows
    assert sum(b["n"] for b in bins) == len(rows)


# Tradeability


def test_edge_survives_costs():
    bins = [{
        "bin": "0.02-0.05", "lo": 0.02, "hi": 0.05, "n": 10,
        "avg_price": 0.03, "actual_yes_rate": 0.08, "gap": 0.05,
        "se": 0.01, "avg_spread": 0.02,
    }]
    t = tradeability_table(bins)[0]
    assert t["direction"] == "buy_yes"
    assert t["gross_edge"] == pytest.approx(0.05)
    assert t["net_edge"] == pytest.approx(0.03)
    assert t["tradeable"] is True
    assert t["ceiling_per_contract_units"] == pytest.approx(0.30)


def test_edge_eaten_by_spread_not_tradeable():
    # Gross edge 0.02 but spread 0.05 -> net negative, not tradeable
    bins = [{
        "bin": "0.05-0.10", "lo": 0.05, "hi": 0.10, "n": 10,
        "avg_price": 0.07, "actual_yes_rate": 0.09, "gap": 0.02,
        "se": 0.01, "avg_spread": 0.05,
    }]
    t = tradeability_table(bins)[0]
    assert t["tradeable"] is False
    assert t["ceiling_per_contract_units"] == 0.0


def test_no_side_assumption_buy_no_direction():
    # Overpriced bin: priced 0.90, actual 0.80 -> buy-NO gross 0.10
    bins = [{
        "bin": "0.80-0.90", "lo": 0.80, "hi": 0.90, "n": 5,
        "avg_price": 0.90, "actual_yes_rate": 0.80, "gap": -0.10,
        "se": 0.05, "avg_spread": 0.03,
    }]
    t = tradeability_table(bins)[0]
    assert t["direction"] == "buy_no"
    assert t["gross_edge"] == pytest.approx(0.10)
    assert t["net_edge"] == pytest.approx(0.07)


# TTE buckets


def test_tte_bucket_labels():
    assert tte_bucket_label(10) == "<48h"
    assert tte_bucket_label(100) == "48h-7d"
    assert tte_bucket_label(500) == ">7d"
    assert tte_bucket_label(None) == "unknown"
    assert tte_bucket_label(-5) == "<48h"  # already past scheduled end


# Reference extraction on a synthetic DB


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE markets (id TEXT PRIMARY KEY, question TEXT, status TEXT, "
        "resolution TEXT, end_date TEXT)"
    )
    conn.execute(
        "CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "market_id TEXT, timestamp TEXT, midpoint REAL, spread REAL)"
    )

    def add(mid, question, resolution, snap_hours, ramp=(0.30, 0.99),
            status="resolved_or_removed", end_date=None):
        conn.execute("INSERT INTO markets VALUES (?, ?, ?, ?, ?)",
                     (mid, question, status, resolution, end_date))
        n = len(snap_hours)
        for i, h in enumerate(snap_hours):
            frac = i / max(n - 1, 1)
            price = ramp[0] + frac * (ramp[1] - ramp[0])
            conn.execute(
                "INSERT INTO snapshots(market_id, timestamp, midpoint, spread) "
                "VALUES (?, ?, ?, ?)",
                (mid, (BASE + timedelta(hours=h)).isoformat(), price, 0.01),
            )

    # m1: 72h of hourly snapshots, ramp 0.30 -> 0.99, resolved YES.
    add("m1", "Will Trump win?", "1", list(range(73)))
    add("m2", "Will the gap market resolve?", "0",
        list(range(11)) + list(range(60, 73)))
    # m3: resolution NULL -> excluded from cohort entirely.
    add("m3", "Null resolution market?", None, list(range(73)))
    conn.commit()
    conn.close()
    return db


def test_reference_extraction_price_and_staleness(tmp_path):
    db = _make_db(tmp_path)
    rows, funnel = extract_reference_rows(db)

    assert funnel["n_cohort"] == 2          # m3's NULL resolution excluded in SQL
    assert funnel["n_final"] == 1
    assert funnel["n_dropped_stale_reference"] == 1

    row = rows[0]
    assert row["market_id"] == "m1"
    assert row["outcome"] == 1
    assert row["political"] is True
    assert row["price"] == pytest.approx(0.76, abs=0.02)
    # Crucially NOT the pinned final value
    assert row["price"] < 0.90
