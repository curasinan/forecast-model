# Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. STOP at every `CHECKPOINT` heading and wait for the operator.

**Goal:** Make the 365-day paper record measurement-valid: fills are captured durably, order placement is idempotent, pricing inputs are staleness-gated, and alerts reach a human — without breaking any verified invariant and without disturbing the live paper book.

**Architecture:** A new reconciliation subsystem (`src/trading/reconcile.py` + `scripts/sync_ibkr_paper.py`, dedicated clientId 4) reads venue truth (`reqAllOpenOrders` + positions) and writes an append-only `fill_events` ledger in `ibkr_paper.db`, keyed by IBKR `permId`. Placement (`run_ibkr_paper.py`) becomes idempotent by consulting the same two sources before transmitting. Staleness gates live with the other gates in `climate/paper_criteria.py`. Alerting gains a durable file sink plus operator-installed Telegram env vars.

**Tech Stack:** Python 3.14, ib_async 2.1.0, sqlite3, pyarrow, pydantic v2, pytest (synthetic fakes only), Windows Task Scheduler via PowerShell 5.1.

**Spec:** The 2026-08-25 read-only audit (delivered in-session, anchored at `2f64a5f`) plus the remediation mission prompt. The findings digest is reproduced in §Coverage below; every file:line was re-verified against `2f64a5f` on 2026-08-25 before this plan was written (HEAD unmoved, tracked tree clean).

## Global Constraints

- **Never touch the three resting GTC orders** (`GTM_1226_1.2/1.3/1.4_YES`, account DU0000000): no cancel, modify, replace, or duplicate from any code written or run under this plan. They are measurement state.
- **The agent never connects to the IB Gateway.** Every first execution of a venue-touching entrypoint is an `[OPERATOR]` task. Tests use fakes; no test opens a network socket.
- **`ibkr_paper.db` and `~/quant_data/**` are production measurement data.** Tests run against `tmp_path` copies only. Schema migration applies to a copy in tests; applying to the real DB is `[OPERATOR]`.
- **Never weaken a gate** (price band 0.05–0.95, `min_edge` floor 0.02, spread ≤ 0.10, suspect-quote rejection, mode guards, staleness refusals, paper criteria). If a gate blocks a task, surface the conflict; do not loosen.
- **Scheduled tasks:** operator-registered from an elevated PowerShell. S4U is forbidden (phantom runs, MIGRATION_CHECKPOINT §3). PowerShell 5.1: use `;`, never `&&`. `ExecutionTimeLimit` must never be `PT0S`.
- **Secrets:** Telegram token/chat-id live in HKCU user-level env vars only — never in the repo, never in a `.ps1`, never in logs or URLs beyond the Telegram API call that already exists (`monitoring/heartbeat.py` deliberately never logs the token; keep that property). This is the repo's analogue of the team's recorded auth-bypass lesson (secrets never travel in artifacts that get copied around).
- **Git:** single feature branch `audit-remediation` off `main`; no worktrees inside OneDrive; no history rewrites; nothing from `~/quant_data`, `.env`, or `logs/` enters a commit. Small commits, descriptive-sentence style matching the repo (`git log` for examples).
- **Do-not-break regression contract** (§Do-not-break at the end): the named pinning tests must stay green after every task.
- **Deadlines:** Phase 1 complete before any fill can land (target 2026-09-05). M9 fee re-verify ritual in place before **2026-09-24** (hard date: `IBKR_FEES_VERIFIED_ON=2026-08-25` + `max_age_days=30`).

## Design decisions (brainstormed 2026-08-25; adopt unless a blocker surfaces — then STOP and surface the tradeoff)

1. **Reconciliation clientId (Q1): dedicated role `paper_sync` = clientId 4**, venue-read/DB-write. Rationale: reconciliation needs `reqAllOpenOrders` (cross-client visibility; orders arrive unbindable with `orderId=0` but carry `permId` — which is exactly why permId is the join key) and account positions (visible from any clientId). It never cancels, so it never needs order binding. A dedicated id makes Gateway-level collision with `run_ibkr_paper` (clientId 1) impossible; DB-write races are excluded by a sidecar lockfile (`<db>.lock`, `O_EXCL`, stale after 10 min) shared by both entrypoints. Rejected alternative: reuse clientId 1 with mutual exclusion — a scheduled daily sync colliding with a manual placement run would 326-kill one of them for no benefit.
2. **Schema (Q2): ALTER-in-place via the existing `_ensure_columns` pattern** (`position_tracker.py:97`): `trade_log` gains `perm_id TEXT`, `last_synced_utc TEXT`; new append-only table `fill_events(perm_id, order_id, ts_utc, cum_filled, fill_delta, avg_price, source, PRIMARY KEY(perm_id, cum_filled))`; `positions` stays a derived cache. Status vocabulary grows in the code enum only (`PARTIALLY_FILLED`) — statuses are stored as strings, no DB change needed. Backfill of the three existing `placed` rows: `[OPERATOR]` captures their permIds while they still rest (Task 0.2), migration script consumes the mapping.
3. **Partial-fill semantics (Q3): append-only `fill_events` + derived positions.** Matches the house first-print-ledger philosophy (append-only, auditable), and the `(perm_id, cum_filled)` primary key makes reconciliation naturally idempotent. Rejected: mutating a cumulative position row in place (no audit trail, idempotence by convention only).
4. **Staleness thresholds (Q4): a frozen `StalenessGates` dataclass in `climate/paper_criteria.py`** (the gates' established home, `ClimateEdgeGate` precedent). Defaults: `max_sweep_age_min=40` (2 sweep cadences), `max_signal_age_min=90`, `require_fresh_init=True` with expected-latest init = current month when `today.day >= 7`, else previous month (SEAS5 arrives ~5th–6th). No CLI loosening flags — changing a threshold is a reviewed code change.
5. **Concentration cap (Q5): per correlated-group committed-notional cap** in `src.trading.RiskManager`: `RiskLimits.max_group_usd = 15.0` (1.5× the $10 position cap — **operator may veto the value at the phase checkpoint**), group key = `question_id` minus the strike suffix (`"gtm:2026-12"`). Seeded from the DB like total exposure. The already-resting $29.64 in `gtm:2026-12` exceeds the cap by design: the three orders stay (never touch), the cap blocks *further* additions to that print, and the paper-year reports annotate the three strikes as ONE correlated bet (Task 3.6).
6. **H4 robustness study (Q6): `scripts/study_bridge_window.py`** refits the ERA5→NOAA bridge inside each expanding window (train targets strictly before the test year), rebuilds `fc_mean_C/fc_sd_C` per window, and re-scores. Deliverable: `docs/gtm_bridge_robustness.md` (committed) + JSON sidecar via `--out`. Acceptance criterion: with the in-window bridge, `evaluate_hindcast_go_live` still passes (skill_vs_trend > 0 at ≥ 3 leads, n ≥ 24/lead, coverage bands unchanged); the report states the per-lead skill delta either way. If the gate fails, the study says so and the go-live evidence is flagged — the gate is not loosened.

## File map (created / modified)

| Path | Role |
|---|---|
| `src/trading/reconcile.py` (new) | Pure reconciliation logic: DB rows + venue orders/positions → fill/cancel state transitions |
| `src/trading/locks.py` (new) | Sidecar lockfile for `ibkr_paper.db` writers |
| `scripts/sync_ibkr_paper.py` (new) | Reconciliation entrypoint (clientId 4, paper-only) |
| `scripts/migrations/migrate_ibkr_paper_v2.py` (new) | Schema migration + permId backfill for the 3 legacy rows |
| `scripts/study_bridge_window.py` (new) | H4 robustness study |
| `climate/paper_progress.py` (new) | Gate inputs (paper_days, resolved_months) computed from ledgers |
| `scripts/scheduling/run_ibkr_health.ps1` (new) | Health-probe launcher with durable output capture |
| `src/venues/ibkr/clients.py` | readonly-by-role; `paper_sync`=4; docstring fix |
| `src/venues/ibkr/adapter.py` | `get_all_open_orders()`; permId into `Order.venue_meta` |
| `src/venues/ibkr/gateway.py` | `ClientIdInUseError` fails loud |
| `src/venues/ibkr/recorder.py` | snapshot-age warning |
| `src/venues/ibkr/backfill.py` | `missing_dates` also subtracts already-backfilled days |
| `src/domain/models.py` | `Order.venue_meta` field |
| `src/trading/order_manager.py` | `perm_id` on TradeOrder; `PARTIALLY_FILLED`; M8 NO-leg guard |
| `src/trading/position_tracker.py` | new columns, `fill_events`, derived-position writes |
| `src/trading/risk_manager.py` | group cap; exposure seeding API |
| `src/trading/executor.py` | edge floor from RiskLimits (L1); group exposure wiring |
| `scripts/run_ibkr_paper.py` | dedup, exposure seeding, staleness gate, lockfile |
| `scripts/run_gtm_paper.py` | staleness gate, computed gate inputs, signal history (L8) |
| `climate/paper_criteria.py` | `StalenessGates`; NaN-coverage fix; comment fix (L4) |
| `climate/paper.py` | `freshest_init_by_target`; fingerprint in payload |
| `climate/fit.py` | model fingerprint in artifacts |
| `scripts/check_ibkr_recorder.py` | durable file sink; rows-vs-universe check (M6) |
| `monitoring/heartbeat.py` | retire Polymarket-era main checks (send_alert stays) |
| `scripts/backfill_resolutions.py`, `collectors/collect_llm_scores.py`, `collectors/weather/parse_markets.py` | M3 runtime gates |
| `weather/paths.py` | default root off %LOCALAPPDATA% (M10) |
| `docs/ibkr_runbook.md`, `README.md`, `.env.example`, `docs/MIGRATION_CHECKPOINT.md`, `src/config.py`, `.gitignore` | H7/M10/L6/L9 doc+hygiene fixes |
| `tests/…` | one new test module per task, listed per task |

---

## Phase 0 — Branch, state capture, deadline insurance

### Task 0.1: Create the remediation branch and commit this plan

**Files:** none modified; branch + plan commit.

- [ ] **Step 1:** `git checkout -b audit-remediation`
- [ ] **Step 2:** `git add docs/plans/2026-08-25-audit-remediation.md ; git commit -m "Add the audit remediation plan"`
- [ ] **Step 3:** `python -m pytest -q` — expected: 694 passed, 3 skipped (baseline; record the numbers in the commit message of the next task if they differ).

**Acceptance:** branch exists; baseline suite green. **Regress note:** none (no code touched).

### Task 0.2 `[OPERATOR]`: Capture the three resting orders' permIds while they still rest

**Why now:** recoverable in one API call today; manual statement archaeology after a fill or cancel. **Never** cancel/modify while doing this.

- [ ] **Step 1 `[OPERATOR]`:** With the paper Gateway logged in, from an interactive Python session (role `research`, clientId 3, read-only — do NOT use clientId 1 or 2):

```python
# operator console — read-only capture; safe alongside recorder + resting orders
import json
from src.venues.ibkr.settings import IBKRSettings
from src.venues.ibkr.clients import connect_ib
ib = connect_ib("research", IBKRSettings())          # readonly=True
trades = ib.reqAllOpenOrders()
rows = [{
    "order_id_venue": t.order.orderId, "perm_id": t.order.permId,
    "con_id": t.contract.conId, "local_symbol": t.contract.localSymbol,
    "action": t.order.action, "qty": t.order.totalQuantity,
    "lmt": t.order.lmtPrice, "status": t.orderStatus.status,
} for t in trades]
print(json.dumps(rows, indent=2))
ib.disconnect()
```

- [ ] **Step 2 `[OPERATOR]`:** Save the output as `~/quant_data/ibkr_ticks/paper/permid_map.json` (outside the repo — it references the live measurement state). Expect 3 rows matching 29@0.34 / 41@0.24 / 71@0.14.

**Acceptance:** `permid_map.json` exists with 3 rows, each `perm_id != 0`. **Regress note:** read-only; if `reqAllOpenOrders` returns nothing, STOP and report (do not guess) — the orders may have filled/cancelled already, which changes Task 1.4's backfill path to statement lookup.

### Task 0.3 `[OPERATOR]`: Calendar insurance for the M9 fee cliff

- [ ] **Step 1 `[OPERATOR]`:** Create a recurring monthly calendar reminder (e.g. the 20th): "IBKR fee re-verify: run whatIf probe or read statement; bump `IBKR_COMMISSION_PER_CONTRACT`/`IBKR_FEES_VERIFIED_ON` in `.env`." First occurrence **before 2026-09-24**. (Runbook entry lands in Task 2.5; this calendar entry is the belt in case Phase 2 slips.)

**Acceptance:** reminder exists. **Regress note:** none.

### CHECKPOINT 0 — confirm permid_map.json contents with the operator before Phase 1.

---

## Phase 1 — P0: fill capture and idempotence (C1, C2, H1, L10, M1)

### Task 1.1: readonly-by-role in `connect_ib`; add role `paper_sync`; fix the false docstring (H1)

**Files:** Modify `src/venues/ibkr/clients.py:26,48-88`; Test `tests/test_venues/test_ibkr_clients_readonly.py` (new).
**Interfaces:** Produces `CLIENT_IDS = {"live_trading": 1, "data_collector": 2, "research": 3, "paper_sync": 4}` and `connect_ib(role, settings, timeout=10.0, readonly: bool | None = None)` where `None` resolves to `role != "live_trading"`.

- [ ] **Step 1: failing test**

```python
"""connect_ib resolves readonly by role and never auto-increments ids."""
import pytest
import src.venues.ibkr.clients as clients
from src.venues.ibkr.settings import IBKRSettings

class _FakeIB:
    last_kwargs: dict = {}
    def connect(self, host, port, clientId, timeout, readonly):
        _FakeIB.last_kwargs = dict(host=host, port=port, clientId=clientId,
                                   timeout=timeout, readonly=readonly)
    def disconnect(self): pass

@pytest.fixture
def fake_ib(monkeypatch):
    import ib_async
    monkeypatch.setattr(ib_async, "IB", _FakeIB)
    return _FakeIB

def _settings():
    return IBKRSettings(port=4002, trading_mode="paper")

@pytest.mark.parametrize("role,expected_readonly,expected_id", [
    ("live_trading", False, 1),
    ("data_collector", True, 2),
    ("research", True, 3),
    ("paper_sync", True, 4),
])
def test_readonly_resolves_by_role(fake_ib, role, expected_readonly, expected_id):
    clients.connect_ib(role, _settings())
    assert fake_ib.last_kwargs["readonly"] is expected_readonly
    assert fake_ib.last_kwargs["clientId"] == expected_id

def test_explicit_readonly_still_wins(fake_ib):
    clients.connect_ib("live_trading", _settings(), readonly=True)
    assert fake_ib.last_kwargs["readonly"] is True
```

- [ ] **Step 2:** `python -m pytest tests/test_venues/test_ibkr_clients_readonly.py -q` — expected: FAIL (`paper_sync` unknown role; readonly always True).
- [ ] **Step 3: implement** — in `clients.py`: add `"paper_sync": 4` to `CLIENT_IDS`; change signature to `readonly: bool | None = None`; first line of body: `if readonly is None: readonly = role != "live_trading"`. Rewrite the docstring sentence at `:53-54`: readonly does NOT gate transmission (the 2026-08-25 paper orders went out over `readonly=True`); the actual transmission gates are the adapter's `read_only` flag and the paper-account check in `_place_order`; what `readonly=False` buys `live_trading` is the startup open/completed-orders fetch so the session can see and bind its own resting orders.
- [ ] **Step 4:** re-run the test — PASS. Also `python -m pytest tests/test_venues -q` (adapter/gateway tests must stay green).
- [ ] **Step 5:** `git add -A ; git commit -m "Resolve ib_async readonly by role and add the paper_sync client id"`

**Acceptance:** table above enforced; docstring no longer claims readonly blocks transmission. **Regress note:** do NOT change `data_collector`/`research` to writable — single-writer rule; runbook client-id table update lands with Task 3.7.

### Task 1.2: `Order.venue_meta` + `get_all_open_orders()` on the adapter (H1, C1 groundwork)

**Files:** Modify `src/domain/models.py:120-125`, `src/venues/ibkr/adapter.py:231-257`; Test `tests/test_venues/test_ibkr_all_open_orders.py` (new).
**Interfaces:** Produces `Order.venue_meta: dict` (carries `{"perm_id": int}` for IBKR) and `IBKRAdapter.get_all_open_orders() -> list[Order]` calling `ib.reqAllOpenOrders()`.

- [ ] **Step 1: failing test** — extend the existing fake pattern from `tests/test_venues/test_ibkr_orders.py` (FakeIB with `_trades`): add a `reqAllOpenOrders()` method returning trades placed by *another* client (`order.orderId = 0`, `order.permId = 987654321`). Two fixtures, both building an `IBKRAdapter` whose `ib` attribute is the FakeIB (bypass `connect()` exactly the way `test_ibkr_orders.py` already does): `fake_adapter_with_foreign_trade` seeds one foreign resting trade as above; `fake_adapter_with_own_trade` seeds one own-session trade (`order.orderId = 16`, `order.permId = 111222333`). Assert:

```python
def test_all_open_orders_carries_perm_id(fake_adapter_with_foreign_trade):
    orders = fake_adapter_with_foreign_trade.get_all_open_orders()
    assert len(orders) == 1
    o = orders[0]
    assert o.venue_meta["perm_id"] == 987654321
    assert o.request.tif == "GTC" and o.request.side == "BUY"

def test_get_open_orders_also_fills_venue_meta(fake_adapter_with_own_trade):
    o = fake_adapter_with_own_trade.get_open_orders()[0]
    assert o.venue_meta["perm_id"] == 111222333
```

- [ ] **Step 2:** run — FAIL (`venue_meta` unknown field / method missing).
- [ ] **Step 3: implement** — `models.py`: add `venue_meta: dict = Field(default_factory=dict)` to `Order` (consistent with `Market`/`Position`; the module docstring's rule says venue-specific detail goes in `venue_meta`). `adapter.py`: factor the trade→Order mapping at `:236-256` into `_trade_to_order(trade)` that also sets `venue_meta={"perm_id": int(getattr(trade.order, "permId", 0) or 0)}`; `get_open_orders` uses it over `ib.openTrades()`; new `get_all_open_orders` uses it over `ib.reqAllOpenOrders()`.
- [ ] **Step 4:** run new + existing venue tests — PASS.
- [ ] **Step 5:** commit: `"Expose permId on orders and add a cross-client open-order fetch"`

**Acceptance:** both fetch paths carry permId; base `VenueAdapter` untouched (method is IBKR-specific; reconcile takes the adapter type). **Regress note:** `Order` is pydantic-frozen? (`OrderRequest` is frozen; `Order` is not — keep it that way). Do not touch `_place_order`.

### Task 1.3: Persist `perm_id` at placement (L10)

**Files:** Modify `src/trading/order_manager.py:60-82,259-297`, `src/trading/position_tracker.py:89-130`, `scripts/run_ibkr_paper.py:160-166`; Test `tests/test_trading/test_perm_id_persistence.py` (new).
**Interfaces:** Produces `TradeOrder.perm_id: str = ""` (stringified venue permId), stored in `trade_log.perm_id`, echoed in `ibkr_paper_runs.jsonl` records.

- [ ] **Step 1: failing test** (FakeVenue pattern from `tests/test_trading/test_executor_venue.py`; make `_place_order`'s ack carry `raw={"perm_id": 555000111}`):

```python
def test_perm_id_flows_ack_to_db(tmp_path):
    venue = FakeVenueWithPermId(read_only=False)
    ex = TradingExecutor(venue=venue, bankroll=100.0, db_path=str(tmp_path / "p.db"), simulate=False)
    rep = ex.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="auto")
    order = rep.decisions[0].order
    assert order.perm_id == "555000111"
    import sqlite3
    row = sqlite3.connect(tmp_path / "p.db").execute(
        "SELECT perm_id, status FROM trade_log WHERE order_id = ?", (order.order_id,)).fetchone()
    assert row == ("555000111", "placed")
```

- [ ] **Step 2:** run — FAIL. 
- [ ] **Step 3: implement** — `TradeOrder` gains `perm_id: str = ""`; `_place_venue_order` sets `order.perm_id = str(ack.raw.get("perm_id") or "")`; `position_tracker._ensure_columns` adds `{"perm_id": "TEXT", "last_synced_utc": "TEXT"}` to `trade_log` and `record_trade` writes them; `run_ibkr_paper.py` run-ledger dict gains `"perm_id": d.order.perm_id`.
- [ ] **Step 4:** run test + `tests/test_trading` — PASS. **Step 5:** commit `"Persist the venue permId from ack to trade log and run ledger"`.

**Acceptance:** permId survives placement into both durable records. **Regress note:** `INSERT OR REPLACE` in `record_trade` must keep writing all existing columns (extend the tuple; do not reorder).

### Task 1.4: Schema migration script + legacy-row backfill (Q2)

**Files:** Create `scripts/migrations/migrate_ibkr_paper_v2.py`; Test `tests/test_trading/test_migrate_ibkr_paper_v2.py` (new).
**Interfaces:** Produces `fill_events` table (DDL below) and `migrate(db_path, permid_map: dict[str, str] | None) -> dict` returning `{"columns_added": [...], "fill_events_created": bool, "permids_backfilled": int}`. Idempotent.

```sql
CREATE TABLE IF NOT EXISTS fill_events (
    perm_id         TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    detected_ts_utc TEXT NOT NULL,      -- when reconciliation OBSERVED the state change
    cum_filled      REAL NOT NULL,
    fill_delta      REAL NOT NULL,
    avg_price       REAL,
    commission_usd  REAL,               -- NULL today (see semantics note)
    source          TEXT NOT NULL,      -- 'sync_open_order' | 'sync_position_diff' | 'manual'
    PRIMARY KEY (perm_id, cum_filled)
)
```

**Timestamp/commission semantics (operator-approved 2026-08-25; copy this note into the module docstring):**
- `detected_ts_utc` is the sync **detection** time, always set. On the position-diff path the TRUE fill time is unknown — a fill that landed days ago is detected at the next sync. Neither `detected_ts_utc` nor `ts_utc` may be treated as the fill time in P&L timing analysis.
- `ts_utc` is the best-known event time: for sync-created rows today it equals `detected_ts_utc`; a future execution-report enrichment may set it to the venue execution time.
- `commission_usd` stays NULL today; the column exists now so the future `reqExecutions`/`CommissionReport` enrichment does not need a second migration.

- [ ] **Step 1: failing test** — build a tmp DB with the CURRENT production schema (create via `PositionTracker(str(tmp))` at the pre-migration code state is impossible post-1.3; instead embed the v1 DDL literally in the test) and three `placed` rows mimicking the real ones (order_ids `a1/a2/a3`, no `perm_id` column). Assert: migration adds columns + table; `migrate(db, {"a1": "111", "a2": "222", "a3": "333"})` backfills exactly 3 permIds; running `migrate` twice changes nothing (idempotent); rows with unknown order_id in the map raise `ValueError` naming the id.
- [ ] **Step 2:** FAIL. **Step 3: implement** (reuse `_ensure_columns`; map keyed by `order_id`, values permId strings; wrap in one transaction). CLI: `python scripts/migrations/migrate_ibkr_paper_v2.py --db <path> --permid-map <json>` where the JSON is the operator's `permid_map.json` reshaped `{order_id: perm_id}` — accept BOTH that shape and the raw Task-0.2 list (match rows by `qty`+`lmt` to `trade_log.size`+`price` when order_ids are absent; refuse on ambiguity).
- [ ] **Step 4:** PASS. **Step 5:** commit `"Add the ibkr_paper v2 migration with permId backfill"`.

**Acceptance:** migration idempotent on a copy; ambiguity refuses loudly. **Regress note:** never DROP/ALTER-narrow anything; `positions` table untouched.

### Task 1.5: Reconciliation core (C1 + M1)

**Files:** Create `src/trading/reconcile.py`; Test `tests/test_trading/test_reconcile.py` (new).
**Interfaces:** Consumes `IBKRAdapter.get_all_open_orders()` (Task 1.2), `VenueAdapter.get_positions()`. Produces:

```python
@dataclass
class ReconcileReport:
    examined: int; filled: int; partially_filled: int; cancelled: int
    unchanged: int; unmatched_venue_orders: int; notes: list[str]

def reconcile_paper_book(db_path: str, venue, now: datetime | None = None) -> ReconcileReport
```

Algorithm (pure, testable with fakes): load `trade_log` rows with `status IN ('placed','partially_filled')`; index venue open orders by `venue_meta["perm_id"]`; index positions by `market_id.con_id`.
For each DB row (join key `perm_id`; rows with empty perm_id get a note and are skipped — surfaced, never guessed):
1. **Still open, more filled than recorded** → append `fill_events(perm_id, cum_filled=venue.filled_quantity, fill_delta=venue.filled−db.filled_size, avg_price, commission_usd=NULL, ts_utc=detected_ts_utc=now, source='sync_open_order')`; set `status='partially_filled'`, `filled_size`, `last_synced_utc`. (Timestamp/commission semantics: Task 1.4 note — detection time is NOT fill time; carry that note into the `reconcile.py` docstring.)
2. **Gone from open orders** → consult positions for that row's `con_id`: position quantity ≥ (row size − recorded fills) → **FILLED**: final fill_event (`cum_filled=size`, `source='sync_position_diff'`, `avg_price=position.avg_price`), `status='filled'`, and upsert the derived `positions` DB row (reuse `_open_position`, idempotent per order_id). Position absent or smaller → **CANCELLED** (`status='cancelled'`; if partial fills were recorded, keep them and open the partial position; append a note when position quantity mismatches recorded fills — never invent).
3. Venue orders whose perm_id matches no DB row → count `unmatched_venue_orders` + note (foreign/manual order; never touched).
All writes in ONE sqlite transaction. `avg_price` caveat comment: IB `avgCost` for multiplier-1 OPT ≈ per-contract price; `[OPERATOR]` verifies against the first real fill statement (Task 1.9).

- [ ] **Step 1: failing tests** (all on tmp DBs + a `FakeSyncVenue` exposing the two fetches):
  - resting unchanged → all counters zero except `unchanged`;
  - partial fill appears → one event row, status `partially_filled`;
  - order gone + covering position → `filled`, derived position row exists, cost preserved;
  - order gone, no position → `cancelled`; partial-then-cancelled keeps its partial position (M1);
  - **idempotence:** running twice adds zero new `fill_events` rows;
  - **cross-process:** build DB, discard all objects, fresh call still reconciles (proves no in-memory dependence — the C1 red test).
- [ ] **Step 2:** FAIL. **Step 3: implement.** **Step 4:** PASS. **Step 5:** commit `"Add durable paper-book reconciliation keyed by permId"`.

**Acceptance:** the six scenarios above green; single transaction verified (inject a failing write → DB unchanged). **Regress note:** must not import ib_async (TID251 — reconcile lives outside `src/venues/`); consumes only domain types.

### Task 1.6: DB lockfile + `scripts/sync_ibkr_paper.py` entrypoint

**Files:** Create `src/trading/locks.py`, `scripts/sync_ibkr_paper.py`; Modify `scripts/run_ibkr_paper.py:134-136` (acquire lock around DB work); Test `tests/test_trading/test_db_lock.py` (new).
**Interfaces:** `acquire_db_lock(db_path, stale_after_min=10)` context manager → sidecar `<db>.lock` with `{pid, ts_utc}`, `O_EXCL`; stale locks are broken with a logged warning; a live lock raises `LockHeldError`.

- [ ] **Step 1: failing test:** concurrent acquisition raises; stale lock (ts older than threshold) is broken; lock released on context exit and on exception.
- [ ] **Step 2:** FAIL. **Step 3: implement** `locks.py`; then the entrypoint mirroring `run_ibkr_paper.py`'s guards exactly: `IBKRSettings()` (loud), refuse unless `trading_mode == "paper"` (exit 2), `IBKRAdapter(settings, role="paper_sync", read_only=True)`, `connect()` (DU/DF cross-check fires), belt-and-braces `adapter.mode != "paper"` → exit 2; then `with acquire_db_lock(db_path): report = reconcile_paper_book(db_path, adapter)`; print report; append a JSON line to `~/quant_data/.../paper/ibkr_sync_runs.jsonl` — the record MUST carry `{"ts_utc": ..., "ok": bool, ...report fields}` (`ok` is what the health probe keys on, Task 2.2); exit 0 (healthy) / 1 (reconcile error). Default `--db` identical to `run_ibkr_paper.py`'s (`settings.resolved_data_root / "paper" / "ibkr_paper.db"`).
- [ ] **Step 4:** lock tests PASS; `python -m pytest tests/test_trading -q` green. (The entrypoint itself is exercised in Task 1.9 by the operator — no test connects anywhere.)
- [ ] **Step 5:** commit `"Add the paper-book sync entrypoint behind a DB lockfile"`.

**Acceptance:** entrypoint refuses non-paper settings/account before any DB write; lock excludes concurrent writers. **Regress note:** `read_only=True` adapter means `place_order` raises `VenueReadOnlyError` — the sync path is structurally incapable of ordering; keep it that way.

### Task 1.7: Placement idempotence + persistent exposure seeding (C2)

**Files:** Modify `scripts/run_ibkr_paper.py:108-148`, `src/trading/risk_manager.py:62-84,211-217`; Test `tests/test_trading/test_placement_idempotence.py` (new).
**Interfaces:** Produces, in `src/trading/reconcile.py`: `load_open_commitments(db_path) -> dict` with keys `{"total_usd": float, "by_market": dict[str, float], "by_group": dict[str, float]}` (rows with `status IN ('placed','partially_filled')` plus open positions; group key per Q5) and `drop_already_committed(intents, venue_open_orders, commitments) -> list[TradeIntent]` (drops any intent whose bought-leg con_id has a resting venue order or a DB commitment, logging each skip with its reason). Produces `RiskManager.seed_exposure(total_usd: float)`.

- [ ] **Step 1: failing tests:**

```python
def test_rerun_with_resting_orders_places_nothing(tmp_path):
    db = str(tmp_path / "p.db")
    venue = FakeSyncVenue(read_only=False)            # from test_reconcile.py (Task 1.5)
    # Simulate the prior run: place once, so the DB holds a 'placed' row for
    # con 712856754 and the fake venue shows the same order resting.
    ex1 = TradingExecutor(venue=venue, bankroll=100.0, db_path=db, simulate=False)
    ex1.execute_intents([TradeIntent(edge=_edge(), market=_ibkr_market())], mode="auto")
    n_requests_after_first = len(venue.requests)
    # Fresh process: new executor, same DB, same venue state, same intent.
    ex2 = TradingExecutor(venue=venue, bankroll=100.0, db_path=db, simulate=False)
    intents = [TradeIntent(edge=_edge(), market=_ibkr_market())]
    kept = drop_already_committed(intents, venue.get_all_open_orders(),
                                  load_open_commitments(db))
    ex2.risk_manager.seed_exposure(load_open_commitments(db)["total_usd"])
    rep = ex2.execute_intents(kept, mode="auto")
    assert kept == [] and rep.trades_executed == 0
    assert len(venue.requests) == n_requests_after_first

def test_exposure_seeded_from_db(tmp_path):
    rm = RiskManager(limits=RiskLimits(max_total_exposure_usd=50.0), bankroll=100.0)
    rm.seed_exposure(29.64)
    res = rm.check_trade(edge=_edge(), proposed_size_usd=30.0)
    assert res.adjusted_size_usd <= 50.0 - 29.64 + 1e-9
```

- [ ] **Step 2:** FAIL. **Step 3: implement** — in `run_ibkr_paper.py` after connect and before `execute_intents`: `resting = {(o.request.market_id.con_id, o.request.side) for o in adapter.get_all_open_orders()}`; `committed = load_open_commitments(db_path)`; drop every intent whose `(market.market_id.con_id, "BUY")` (or NO-leg con_id) is in `resting` **or** whose market_id appears in `committed["by_market"]` — log each skip with the reason; `executor.risk_manager.seed_exposure(committed["total_usd"])`. Wrap placement+sync in `acquire_db_lock`.
- [ ] **Step 4:** PASS + full `tests/test_trading` green. **Step 5:** commit `"Refuse duplicate paper orders and seed risk exposure from the book"`.

**Acceptance:** success criterion (b): re-run with unchanged signals → 0 new orders, loud per-intent skip lines. **Regress note:** dedup must key on con_id of the LEG being bought; never auto-cancel anything it finds (ground rule 1). The `--simulate` path skips venue dedup (read-only adapter has no orders) but still seeds from its own sim DB.

### Task 1.8: Scheduled sync task registration script

**Files:** Modify `scripts/scheduling/register_ibkr_tasks.ps1` (append an "IBKR Paper Sync" daily task, pattern-copied from the health task: Interactive principal, `-MultipleInstances IgnoreNew`, `ExecutionTimeLimit` 10 min, daily at 17:30 local — after the 16:30 label collector; PowerShell 5.1 syntax, `;` only); Create nothing else.

- [ ] **Step 1:** add the task block (registration-only script; no test — validated by `[OPERATOR]` in 1.9). Use `powershell.exe -File scripts\scheduling\run_ibkr_sync.ps1` launcher (new, modeled on `run_ibkr_recorder.ps1`: sets `IBKR_PORT=4002`/`IBKR_TRADING_MODE=paper` only-if-unset, logs to `logs/ibkr_sync_task.log`, propagates exit code).
- [ ] **Step 2:** commit `"Register a daily paper-book sync task"`.

**Acceptance:** script parses under PowerShell 5.1 (`powershell -NoProfile -Command "Get-Command -Syntax"` dry parse is enough); S4U absent. **Regress note:** do not touch the recorder/health task definitions in the same edit beyond appending.

### Task 1.9 `[OPERATOR]`: Apply migration to the real DB; first sync run; register the task

- [ ] **Step 1 `[OPERATOR]`:** Back up: `copy ~\quant_data\ibkr_ticks\paper\ibkr_paper.db ibkr_paper.db.bak-20260825`
- [ ] **Step 2 `[OPERATOR]`:** `python scripts\migrations\migrate_ibkr_paper_v2.py --db <real path> --permid-map ~\quant_data\ibkr_ticks\paper\permid_map.json` — expect `permids_backfilled: 3`.
- [ ] **Step 3 `[OPERATOR]`:** With the paper Gateway up: `set IBKR_PORT=4002` etc., `python scripts\sync_ibkr_paper.py` — expect a report with `examined=3, unchanged=3` (or fills, if any landed — in which case verify `avg_price` against the statement and record the commission fact per M9).
- [ ] **Step 4 `[OPERATOR]`:** Elevated PowerShell: `.\scripts\scheduling\register_ibkr_tasks.ps1` (re-registers all, adds the sync task).

**Acceptance:** success criterion (a) analog on real data: the resting book reconciles with zero mutations; scheduled task registered. **Regress note:** if the report shows anything but 3 examined rows, STOP and diagnose with the operator before Phase 2.

### CHECKPOINT 1 — review reconcile report + dedup behavior with the operator. Phase 1 must be merged-ready before any fill lands.

---

## Phase 2 — P1: unattended safety (H2, H3+M6, M5, H6, M9)

### Task 2.1: Staleness gates (H2)

**Files:** Modify `climate/paper_criteria.py` (add `StalenessGates` + `check_staleness(...)`), `climate/paper.py` (payload gains `sweep_ts_utc`, `inits`, via new `freshest_init_by_target(frame)`), `scripts/run_gtm_paper.py` (refuse stale sweep), `scripts/run_ibkr_paper.py` (refuse stale signals / stale init); Test `tests/test_climate/test_staleness_gates.py` (new).
**Interfaces:**

```python
@dataclass(frozen=True)
class StalenessGates:
    max_sweep_age_min: float = 40.0
    max_signal_age_min: float = 90.0
    require_fresh_init: bool = True

def expected_latest_init(today: date) -> str      # 'YYYY-MM'; day>=7 -> this month else previous
def check_staleness(*, sweep_ts, signal_ts, inits_by_target, now, gates) -> list[str]  # empty == fresh
```

- [ ] **Step 1: failing tests:** sweep 41 min old → reason; signal 91 min old → reason; init '2026-07' when expected '2026-08' (now=2026-08-25) → reason naming the target; all fresh → empty; boundary day 6 vs day 7 for `expected_latest_init`.
- [ ] **Step 2:** FAIL. **Step 3: implement**; entrypoints exit 1 printing every reason (loud refusal, no override flag). `freshest_init_by_target` returns `{target_month: init 'YYYY-MM'}` from the same shortest-lead selection `predictive_by_target_month` uses (do NOT change that function's 2-tuple return — `ladder_table` unpacks it).
- [ ] **Step 4:** PASS + `tests/test_climate` green. **Step 5:** commit `"Refuse stale sweeps, stale signals, and stale SEAS5 inits before ordering"`.

**Acceptance:** success criterion (c). **Regress note:** thresholds only in `StalenessGates` defaults; do not add CLI loosening flags; existing edge-gate behavior byte-identical for fresh inputs.

### Task 2.2: Alerts reach a durable channel (H3) + data-flow-aware probe (M6)

**Files:** Modify `scripts/check_ibkr_recorder.py` (append every non-OK result to `logs/ibkr_health.log` with UTC ts, independent of logging config; add DEGRADED status when `rows < 0.5 * universe_size` on a fresh heartbeat; add SYNC_STALE status when the last `"ok": true` line of `ibkr_sync_runs.jsonl` is older than `--max-sync-age-hours` default **26**, or the file is missing/has no successful line — path derived from the same data root as the state file: `<root>/paper/ibkr_sync_runs.jsonl`), `monitoring/heartbeat.py` (delete the Polymarket-era checks in `run_heartbeat`/`main` — keep `send_alert` and its import path intact); Create `scripts/scheduling/run_ibkr_health.ps1` (launcher: run probe, tee stdout to `logs/ibkr_health_task.log`, propagate exit code) and point the health task's action at it in `register_ibkr_tasks.ps1`; Test `tests/test_monitoring/test_health_sink.py` (new).

**Why the sync check (operator-approved 2026-08-25):** the 17:30 sync task dies with exit 1 when the Gateway is logged out, and nothing catches that today — a silent sync outage recreates C1 (fills landing unrecorded) for as long as it lasts. 26h = one daily cadence + 2h slack.

- [ ] **Step 1: failing tests:** STALE result appends one line to a tmp `ibkr_health.log`; OK appends nothing; fresh heartbeat with `rows=10, universe_size=2900` → `("DEGRADED", ...)` exit-1 path; tmp `ibkr_sync_runs.jsonl` whose last `"ok": true` line is 27h old → `("SYNC_STALE", ...)` exit-1; 25h old → healthy on that axis; file with only `"ok": false` lines → SYNC_STALE; recorder-heartbeat statuses take precedence in the message but any failing axis exits 1; `send_alert` still never logs the token (assert the token string absent from log output with telegram misconfigured).
- [ ] **Step 2:** FAIL. **Step 3: implement.** **Step 4:** PASS (existing probe tests must stay green — extend, don't rewrite). **Step 5:** commit `"Give health alerts a durable sink and a data-flow check"`.
- [ ] **Step 6 `[OPERATOR]`:** Set user-level env vars (HKCU; visible to scheduled tasks; never in repo):
  `[Environment]::SetEnvironmentVariable('ALERT_METHOD','telegram','User')` ; same for `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Re-register tasks (elevated). Then a dry-run: temporarily rename the heartbeat file? **No — never touch quant_data state**; instead run `python scripts\check_ibkr_recorder.py --state-path C:\nonexistent.json` and confirm the CRITICAL alert arrives on Telegram AND lands in `logs/ibkr_health.log`.

**Acceptance:** success criterion (d): a forced non-OK reaches both Telegram and the file. **Regress note:** `check_ibkr_recorder` must keep exit codes 0/1 exactly (Task Scheduler semantics); `send_alert` import path unchanged (probe imports it).

### Task 2.3: `ClientIdInUseError` fails loud in GatewaySession (M5)

**Files:** Modify `src/venues/ibkr/gateway.py:225-241`; Test extend `tests/test_venues/test_ibkr_gateway.py` (or the existing gateway test module).

- [ ] **Step 1: failing test:** fake `connect` raising `ClientIdInUseError` → `ensure_connected()` raises it after exactly 1 attempt (no sleep called).
- [ ] **Step 2:** FAIL. **Step 3:** in `_reconnect_loop`, before the generic `except Exception`: `except ClientIdInUseError: raise` (import from `src.venues.ibkr.clients`), mirroring the `connect_validator` config-error philosophy documented at `gateway.py:117-119`.
- [ ] **Step 4:** PASS + existing backoff tests green (do-not-break: jittered 1s→60s behavior pinned). **Step 5:** commit `"Treat a client-id collision as a config error, not a retriable outage"`.

**Regress note:** KeyboardInterrupt handling and backoff constants untouched.

### Task 2.4: Universe snapshot aging made visible (H6)

**Files:** Modify `src/venues/ibkr/recorder.py:174-183` (log WARNING when the snapshot file mtime is older than 35 days at seed time), `scripts/check_ibkr_recorder.py` (append `snapshot_age_days` to the OK message when readable); Test extend recorder/probe tests.

- [ ] **Step 1: failing test:** seeding from a snapshot with mtime 40 days ago emits a WARNING containing "snapshot"; probe OK line includes `snapshot_age_days=`.
- [ ] **Step 2:** FAIL. **Step 3: implement.** **Step 4:** PASS. **Step 5:** commit `"Warn when the contract universe snapshot goes stale"`.
- [ ] **Step 6 `[OPERATOR]` (recurring, monthly, with the M9 ritual):** with the Gateway up: `python scripts\verify_ibkr_entitlements.py --port 4002 --snapshot data\ibkr_universe_live.json`, review the diff, commit the refreshed snapshot. First run due **before 2026-09-20** (new GTM expiries are already listing).

**Acceptance:** decay is loud within one month of becoming real. **Regress note:** `universe()`'s seed-when-empty behavior unchanged; no live rediscovery added (stays a documented Stage-5 follow-up — YAGNI).

### Task 2.5: Monthly rituals into the runbook (M9 + H6 + Telegram test)

**Files:** Modify `docs/ibkr_runbook.md` — new section "Monthly rituals" (fee re-verify + `.env` date bump before the 30-day `StaleFeeError` cliff, with the exact whatIf/statement procedure; universe snapshot refresh per Task 2.4; a monthly Telegram test alert). This lands here (not Phase 3) because of the **2026-09-24 hard date**.

- [ ] **Step 1:** write the section (claim/evidence style the runbook uses). **Step 2:** commit `"Add the monthly fee, universe, and alert rituals to the runbook"`.

**Acceptance:** an operator following the runbook cannot hit the 09-24 cliff unwarned. **Regress note:** none (doc only). Full runbook rewrite is Task 3.7; keep this section additive so it survives it.

### CHECKPOINT 2 — operator confirms: Telegram fires, sync task runs daily, staleness gates behave on real (fresh) data.

---

## Phase 3 — P2: measurement integrity and hygiene

### Task 3.1: Model fingerprint through the chain (M2a)

**Files:** Modify `climate/fit.py` (sha256 of the canonical `to_json` into `GtmFitResult.to_dict` and into the model JSON as `fingerprint`), `climate/paper.py` (payload gains `model_fingerprint`), `scripts/run_ibkr_paper.py` (echo into run-ledger records); Test `tests/test_climate/test_model_fingerprint.py` (new).

- [ ] **Step 1: failing test:** fingerprint stable across load/save round-trip; changes when any LeadParams coefficient changes; appears in `run_paper` payload.
- [ ] **Step 2:** FAIL. **Step 3: implement** (`hashlib.sha256(model.to_json(indent=0).encode()).hexdigest()[:16]`; exclude the fingerprint field itself from the hashed payload). **Step 4:** PASS. **Step 5:** commit `"Fingerprint the fitted model through signals and order records"`.

**Regress note:** `EmosModel.from_json` must accept files without the field (older artifacts).

### Task 3.2: Gate inputs computed from the ledgers (M2b) — folds L4

**Files:** Create `climate/paper_progress.py`; Modify `scripts/run_gtm_paper.py:55-56,90-99`, `climate/paper_criteria.py:118-127` (NaN coverage now appends a reason — a strengthening; fix the `:53` comment to "any 3 leads"); Test `tests/test_climate/test_paper_progress.py` (new).
**Interfaces:** `compute_paper_progress(runs_jsonl: Path, labels_root: Path | None, now) -> PaperProgress(paper_days: int, resolved_months: int, first_order_ts: str | None)` — `paper_days` from the first `simulated=false` line of `ibkr_paper_runs.jsonl`; `resolved_months` = distinct GTM target months that (i) appear in any run's orders and (ii) have a first-print label in the ledger.

- [ ] **Step 1: failing tests:** synthetic runs-jsonl + label frame → expected numbers; missing files → `(0, 0, None)`; `run_gtm_paper` defaults use computed values, and passing `--paper-days` now requires `--allow-manual-gate-inputs` (refuses loudly otherwise); NaN coverage row → gate fails with a reason (was: silent pass).
- [ ] **Step 2:** FAIL. **Step 3: implement.** **Step 4:** PASS. **Step 5:** commit `"Compute go-live gate inputs from the ledgers instead of trusting the CLI"`.

**Regress note:** `evaluate_hindcast_go_live`'s passing behavior on finite, in-band inputs must be byte-identical (pinned by existing tests); only NaN handling strengthens.

### Task 3.3: Bridge robustness study (H4) — decisions Q6

**Files:** Create `scripts/study_bridge_window.py`, `docs/gtm_bridge_robustness.md` (written by the run); Test `tests/test_climate/test_bridge_window_study.py` (synthetic frame, small windows).

- [ ] **Step 1: failing test:** on a synthetic frame where the bridge relation drifts over time, the in-window study produces per-window bridge params that differ across windows, and the output table contains both `skill_vs_trend_leaky` and `skill_vs_trend_honest` columns.
- [ ] **Step 2:** FAIL. **Step 3: implement:** for each test year Y: fit `Bridge` on ERA5/NOAA months with target year < Y (reuse `Bridge.fit(start=None, end=...)` bounds), rebuild `fc_mean_C/fc_sd_C` for train+test rows of that window via `build_frame_from_inputs(..., bridge=window_bridge)`, then reuse `expanding_window_scores` machinery for that single year; aggregate; run `evaluate_hindcast_go_live` on the honest summary; write markdown + JSON to `--out` paths.
- [ ] **Step 4:** PASS. **Step 5:** run it for real (local parquet only — no network, no Gateway; reads `~/quant_data/climate_data`, writes only the two `--out` files into the repo): `python scripts/study_bridge_window.py --out docs/gtm_bridge_robustness.md --json-out docs/gtm_bridge_robustness.json`. **Step 6:** commit code + report: `"Measure the hindcast skill with the bridge fit inside each window"`.

**Acceptance:** report states pass/fail of the honest-bridge gate and per-lead skill deltas. If it FAILS the gate: STOP, surface to operator; do not touch the gate. **Regress note:** must not modify `climate/fit.py`'s production path; study is additive.

### Task 3.4: Polymarket runtime gates on the three stragglers (M3)

**Files:** Modify `scripts/backfill_resolutions.py` (top of `main()`), `collectors/collect_llm_scores.py` (top of `main()`), `collectors/weather/parse_markets.py` (top of its CLI main); Test extend `tests/test_retired_polymarket.py`.

- [ ] **Step 1: failing test:** subprocess-run each entrypoint with `POLYMARKET_RUNTIME` unset and a `--help`-adjacent minimal argv → exit code 2 before any network import executes (pattern-match the existing gate tests in `tests/test_retired_polymarket.py`).
- [ ] **Step 2:** FAIL. **Step 3:** add `refuse_polymarket_runtime("scripts/backfill_resolutions.py")` etc. (signature verified at `src/retired_polymarket.py:32`).
- [ ] **Step 4:** PASS. **Step 5:** commit `"Gate the last three Polymarket network entrypoints"`.

**Regress note:** library imports of these modules must stay importable (gate only in `main()`).

### Task 3.5: Backfill idempotence at day granularity (M4)

**Files:** Modify `src/venues/ibkr/backfill.py:110-137` (`missing_dates` also subtracts `backfilled_dates(data_root)` — dates with any parquet under `quotes_hourly/date=*/`; document the day-granularity contract in both docstrings); Test extend `tests/test_venues/test_ibkr_backfill.py`.

- [ ] **Step 1: failing test:** tmp tree where `quotes/` lacks day D but `quotes_hourly/date=D/` has a file → D not in `missing_dates`.
- [ ] **Step 2:** FAIL. **Step 3: implement.** **Step 4:** PASS. **Step 5:** commit `"Skip already-backfilled days when planning a backfill"`.

**Regress note:** `HISTORY_STARTS` clipping and whole-contract trimming behavior unchanged (pinned).

### Task 3.6: Correlated-group concentration cap (H5) — decisions Q5

**Files:** Modify `src/trading/risk_manager.py` (`RiskLimits.max_group_usd: float = 15.0`; `check_trade(..., group_key: str = "", ...)` new check + `add_exposure(amount, group_key="")`; `seed_group_exposure(dict)`), `src/trading/executor.py` (derive `group_key = edge.question_id.rsplit(":", 1)[0]` for `gtm:*` ids; thread through check + add), `scripts/run_ibkr_paper.py` (seed groups from `load_open_commitments`), plus the run-report `summary()` prints per-group committed totals; Test `tests/test_trading/test_group_cap.py` (new).

- [ ] **Step 1: failing tests:** two $10 intents in group `gtm:2026-12` with `max_group_usd=15` → second gets `adjusted_size_usd <= 5`; a third → rejected with a reason naming the group; seeded group at $29.64 → any further `gtm:2026-12` intent rejected; different group unaffected.
- [ ] **Step 2:** FAIL. **Step 3: implement.** **Step 4:** PASS + all trading tests green. **Step 5:** commit `"Cap committed notional per correlated print"`.

**Acceptance:** the measurement record's report now shows `gtm:2026-12 = $29.64 (3 orders, 1 correlated bet)`. **Known limitation (operator-approved 2026-08-25; record it in the RiskLimits docstring):** the group key is per (symbol-lane, target month) — CROSS-SYMBOL correlation is NOT grouped: GT (annual) vs GTM (monthly) vs GTTA/GTTM (threshold) all settle off the same NOAA series and would ride the same print, yet each would get its own group today. Revisit the grouping key before opening a second symbol lane. **Regress note:** per-position $10 cap and total-exposure logic byte-identical when `group_key` empty; the three resting orders are never cancelled to satisfy the cap — the cap only blocks new placement.

### Task 3.7: Runbook + docs rewrite (H7, M10, L6, L9)

**Files:** Modify `docs/ibkr_runbook.md` (quick-facts: paper session, Read-Only API cleared, `_connect_with_backoff` → GatewaySession; §2 client map gains `paper_sync=4` and drops "clientId 1 never connected"; §3 rewritten around the Interactive principal actually registered; §4 re-login procedure: live username + **Paper toggle**; add sync-task and health-launcher rows), `README.md` (venue paragraph → IBKR ForecastEx climate lane, Polymarket research-archival; weather-root paragraph → `~/quant_data`), `.env.example` (data-root guidance → `~/quant_data`, never %LOCALAPPDATA%; add `IBKR_RESTART_WINDOW_LOCAL`, `CLIMATE_DATA_ROOT`), `docs/MIGRATION_CHECKPOINT.md:335` area (fix the stale `%LOCALAPPDATA%` example in "How to run things"), `weather/paths.py:24-29` (default → `Path.home()/"quant_data"/"polymarket_weather"`; if the old `%LOCALAPPDATA%` dir exists and the new one is empty, log a WARNING naming both paths — never auto-move data), `src/config.py:31-32` (delete `polymarket_private_key`/funder fields after `git grep` proves no reader outside legacy/), `.gitignore` (add `logs/`, `.agents/`, `*.zip`, `bist_brief/preopen_*`, `bist_brief/news_*`, `bist_brief/inject_news*`); Test: extend a weather test asserting the new default (with `tmp_path` monkeypatched HOME).

- [ ] **Step 1: failing test** for `weather/paths.py` default; **Step 2:** FAIL; **Step 3:** implement code bits; **Step 4:** PASS; **Step 5:** docs edits; **Step 6:** `python -m ruff check .` on touched files clean; **Step 7:** commit `"Rewrite the runbook and docs to match the deployed paper configuration"`.

**Acceptance:** grep for `%LOCALAPPDATA%` over README/.env.example/runbook returns only historical-incident references clearly marked as such; runbook §4 sends the operator to the paper toggle. **Regress note:** `docs/MIGRATION_CHECKPOINT.md` is a historical record — fix forward-facing instructions only, do not rewrite past-tense history. `[OPERATOR]` note: after `weather/paths.py` lands, either set `WEATHER_DATA_ROOT` or move the old weather data if that lane is ever used again.

### Task 3.8: Test hygiene (M11) + latent NO-leg guard (M8) + L bundle

**Files:** Modify `tests/test_weather/test_labels_and_license.py:48-55` (pass `tmp_path` override to `resolve_weather_data_root`), the three `tests/test_features/*` DB-reading tests (repoint to a session-scoped synthetic sqlite fixture with the minimal `snapshots` schema; if schema proves too entangled, mark them `skip` with an archival note — surface the tradeoff in the PR, do not silently keep reading the repo DB), `src/trading/order_manager.py:139-144` (IBKR market with `no_id is None` → `leg_id = ""` so the existing `:260-264` "no market id for the leg" rejection fires; keep the Polymarket single-contract fallback), `src/models/prediction.py:33` + `src/trading/executor.py:245-251` (L1: `_evaluate` gates on `abs(edge.edge_pct) >= self._risk_manager.limits.min_edge_pct` and `expected_value > 0` instead of the hardcoded `has_edge`; default `RiskLimits.min_edge_pct=0.05` keeps behavior identical), `scripts/run_gtm_paper.py` (L8: append the payload as one JSON line to `paper/gtm_signals_history.jsonl` after the atomic write of the latest file); Tests for each in their home modules.

- [ ] **Step 1: failing tests:** (M8) NO intent on an IBKR market with `no_id=None` → order REJECTED, venue receives nothing; (L1) `RiskLimits(min_edge_pct=0.03)` + edge 0.04 → proposed (was: silently blocked); default limits + edge 0.04 → still blocked (parity); (M11) weather-root test touches only `tmp_path`; (L8) two runs → 2 history lines, latest file still valid JSON.
- [ ] **Step 2:** FAIL. **Step 3: implement.** **Step 4:** PASS + **full suite**. **Step 5:** commit `"Close the latent NO-leg trap and the test-hygiene gaps"`.

**Regress note:** the `has_edge` property itself stays (other callers in the research pipeline use it); only the executor's gate re-sources its threshold. Snap-to-tick behavior untouched (L2 is a decision item, below).

### CHECKPOINT 3 — full suite + coverage table review with the operator; then (future run) `finishing-a-development-branch` for the merge decision.

---

## Decision-only items (no code in this plan; operator opts in)

| Item | Decision to make |
|---|---|
| M7 | OneDrive strategy: set `git config gc.auto 0` + manual gc with sync paused; move `.env` out of the synced tree (path via env); or accept (everything is pushed). |
| L2 | `snap_to_tick` sub-tick UP-snap: keep (unreachable at band 0.05) or change to reject sub-tick prices. Changing it breaks a pinning test intentionally — needs an explicit decision. |
| L5 | Unused `kelly_cap` + per-run loss breakers: wire to DB-seeded state or delete the dead field. |
| L7 | `ruff check .` 118 style errors: optional cleanup commit at branch end. |
| Repo visibility | Live account number in committed docs (`U0000000`) if the repo ever goes public. |

## Finding → task coverage

| Finding | Task(s) | Status |
|---|---|---|
| C1 fills never recorded | 1.2–1.6, 1.9 | covered |
| C2 no dedup / per-process risk state | 1.7 | covered |
| H1 readonly blind sessions | 1.1, 1.2 | covered |
| H2 staleness | 2.1 | covered |
| H3 alert void | 2.2 | covered |
| H4 bridge leak | 3.3 | covered (study; gate untouched) |
| H5 concentration | 3.6 | covered |
| H6 universe decay | 2.4 (+ ritual 2.5) | covered (visibility + ritual; live rediscovery deferred as documented Stage-5 follow-up) |
| H7 runbook | 2.5, 3.7 | covered |
| M1 partial fills | 1.4, 1.5 | covered |
| M2 self-trusting measurement | 3.1, 3.2 | covered |
| M3 runtime gates | 3.4 | covered |
| M4 backfill dedup | 3.5 | covered (day granularity; row-level dedup deferred — no consumer reads quotes_hourly yet) |
| M5 326 silent retry | 2.3 | covered |
| M6 heartbeat vs data flow | 2.2 | covered |
| M7 OneDrive | decision-only | deferred (explicit) |
| M8 NO-leg trap | 3.8 | covered |
| M9 fee cliff | 0.3, 2.5 | covered (ritual; fail-loud kept by design) |
| M10 doc traps | 3.7 | covered |
| M11 test hygiene | 3.8 | covered |
| L1/L4/L6/L8/L9/L10 | folded (3.8 / 3.2 / 3.7 / 3.8 / 3.7 / 1.3) | covered |
| L2/L5/L7 | decision-only | deferred (explicit) |

## Success criteria

- (a) A simulated fill injected through fakes lands in a tmp `ibkr_paper.db` via `reconcile_paper_book` — and survives a process restart (Task 1.5 tests).
- (b) Re-running placement with unchanged signals places 0 new orders (Task 1.7 test).
- (c) A stale sweep, stale signal file, or stale SEAS5 init refuses placement loudly (Task 2.1 tests).
- (d) A forced probe failure reaches Telegram AND `logs/ibkr_health.log` in the operator dry run (Task 2.2).
- (e) `python -m pytest -q` green (≥ 694 passing baseline plus every new test).
- (f) Every do-not-break invariant below still has its pinning test passing, unmodified except where a task explicitly strengthens it (L1 parity, L4 NaN).

## Do-not-break (verified clean 2026-08-25; regression contract)

Mode-safety triple layer (`settings.py` required port/mode; DU/DF cross-check; paper-only `_place_order`) — pinned in `tests/test_venues`. Tick snap DOWN via Decimal, post-snap sizing, floored rounding, $10 cap arithmetic — `tests/test_trading/test_executor_venue.py`. EV/Kelly closed forms + staleness-refusing fee/carry models — `tests/test_math`, `tests/test_domain`. Signal gates (band, min_edge floor, spread, suspect) + strictly-greater ladder — `tests/test_climate`. First-print ledger — `tests/test_climate` storage tests. TID251 + AST architecture tests — `tests/test_architecture`. GatewaySession heartbeat/backoff/window + atomic `write_state` — `tests/test_venues` gateway/recorder tests. `--simulate` separation (own DB, read-only adapter) — `tests/test_trading` + `run_ibkr_paper` defaults.
