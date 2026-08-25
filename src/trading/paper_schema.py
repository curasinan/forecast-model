"""Shared schema for the IBKR paper book (`ibkr_paper.db`).

This module is the single source of truth for the `fill_events` DDL. Both
the v2 migration (`scripts/migrations/migrate_ibkr_paper_v2.py`, Task 1.4)
and the reconciliation core (`src/trading/reconcile.py`, Task 1.5) import
`FILL_EVENTS_DDL` from here instead of keeping their own copy.

Timestamp/commission semantics (operator-approved 2026-08-25):

- `detected_ts_utc` is the sync **detection** time, always set. On the
  position-diff path the TRUE fill time is unknown -- a fill that landed
  days ago is only detected at the next sync. Neither `detected_ts_utc`
  nor `ts_utc` may be treated as the fill time in P&L timing analysis.
- `ts_utc` is the best-known event time: for sync-created rows today it
  equals `detected_ts_utc`; a future execution-report enrichment may set
  it to the venue execution time instead.
- `commission_usd` stays NULL today; the column exists now so a future
  `reqExecutions`/`CommissionReport` enrichment does not need a second
  migration.
"""

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
