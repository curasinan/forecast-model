"""Shared schema for the IBKR paper book (`ibkr_paper.db`)."""

from __future__ import annotations

FILL_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS fill_events (
    perm_id         TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    detected_ts_utc TEXT NOT NULL,      -- when reconciliation OBSERVED the state change
    cum_filled      REAL NOT NULL,
    fill_delta      REAL NOT NULL,
    avg_price       REAL,
    commission_usd  REAL,               -- NULL today (see module docstring semantics note)
    source          TEXT NOT NULL,      -- 'sync_open_order' | 'sync_position_diff' | 'manual'
    PRIMARY KEY (perm_id, cum_filled)
)
"""
