"""Go-live gate inputs computed from the run/label ledgers (audit finding M2b).

``evaluate_hindcast_go_live``'s ``paper_days``/``resolved_months`` inputs
used to be whatever an operator typed on the CLI
(``scripts/run_gtm_paper.py --paper-days``/``--resolved-months``). Nothing
stopped that number from being wrong -- accidentally or otherwise -- which
means the 365-day/12-month anti-optional-stopping bar
(``climate/paper_criteria.py``'s module docstring) could be defeated by a
single flag. :func:`compute_paper_progress` derives both numbers instead,
from the two ledgers that actually record what happened:

- ``ibkr_paper_runs.jsonl`` (``scripts/run_ibkr_paper.py``'s append site) --
  one line per paper run, each carrying ``ts_utc``, ``simulated``, an
  ``orders`` list (each order carrying its ``question_id``), and an
  ``open_groups`` list of the book's already-resting contract groups.
- the climate label ledger (``climate/storage.py``,
  ``<labels_root>/labels/labels_ledger.parquet``) -- the first-print NOAA
  values ``scripts/collect_climate_labels.py`` appends.

``scripts/run_gtm_paper.py`` now refuses a manually-typed
``--paper-days``/``--resolved-months`` unless the operator also passes
``--allow-manual-gate-inputs`` -- an explicit, loud opt-out for the rare
case (e.g. a documented backfill) where trusting the CLI is intentional.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from climate.storage import load_ledger

#: Pulls the 'YYYY-MM' target month out of a "gtm:<YYYY-MM>[:...]" id --
#: matches both a full order question_id ("gtm:2026-12:0.8", from
#: EdgeSignal.question_id -- see scripts/run_ibkr_paper.py's load_intents)
#: and an already-grouped id ("gtm:2026-12", from
#: src.trading.reconcile._group_key / load_open_commitments' by_group).
_GTM_MONTH_RE = re.compile(r"^gtm:(\d{4}-\d{2})(?::|$)")

#: Fix round 2 (audit finding M2b, controller ruling R14): the only
#: src.trading.order_manager.OrderStatus values that represent a contract
#: actually confirmed at the venue. TradingExecutor attaches
#: TradeDecision.order as soon as the risk check passes -- BEFORE operator
#: approval and before venue placement can fail -- so "pending" (built,
#: never approved/placed) and "rejected" (operator declined or venue
#: refused) both appear in a real run's orders[] without representing an
#: actual bet. "cancelled" is excluded for the same reason. "simulated"
#: never needs to appear here: whole simulated=true records are dropped
#: before this set is even consulted (see compute_paper_progress).
_COUNTED_ORDER_STATUSES = frozenset({"placed", "filled"})


@dataclass(frozen=True)
class PaperProgress:
    """Computed go-live gate inputs (see module docstring)."""

    paper_days: int
    resolved_months: int
    first_order_ts: str | None


def _parse_ts(text: str) -> datetime | None:
    """Best-effort ISO-8601 parse. Returns None (never raises) on anything
    that doesn't parse -- a malformed ``ts_utc`` must not take down the
    go-live gate, just be skipped like a malformed line.

    Note: this is NOT ``climate.paper.parse_ts_utc`` -- that helper expects
    the strict ``gtm_signals.json`` payload format
    (``%Y-%m-%dT%H:%M:%SZ``). ``ibkr_paper_runs.jsonl``'s ``ts_utc`` is
    written with ``datetime.now(timezone.utc).isoformat()``
    (``scripts/run_ibkr_paper.py``), which includes microseconds and a
    ``+00:00`` offset rather than a trailing ``Z``.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _extract_gtm_month(text: object) -> str | None:
    """Best-effort 'YYYY-MM' extraction via ``_GTM_MONTH_RE``. Returns None
    (never raises) for anything that isn't a matching string -- a
    non-string, a non-``gtm`` question_id, or a malformed id must not take
    down the go-live gate, just contribute no evidence.
    """
    if not isinstance(text, str):
        return None
    match = _GTM_MONTH_RE.match(text)
    return match.group(1) if match else None


def _iter_run_records(runs_jsonl: Path):
    """Yield parsed dict records from an ``ibkr_paper_runs.jsonl`` file.

    Blank lines and lines that fail to parse as a JSON object are skipped
    rather than raising: a partially-written trailing line (e.g. from a
    crash mid-append) must not take down the go-live gate.
    """
    if not runs_jsonl.exists():
        return
    with runs_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict):
                yield record


def compute_paper_progress(
    runs_jsonl: Path,
    labels_root: Path | None,
    now: datetime,
) -> PaperProgress:
    """Compute go-live gate inputs from the run and label ledgers (M2b).

    ``paper_days``: whole calendar days between ``now`` and the ``ts_utc``
    of the FIRST ``simulated=false`` line in ``runs_jsonl`` (file order,
    which is append order). A ``--simulate`` rehearsal run never starts
    the clock. Rounding is FLOORED -- ``(now - first_ts).days`` -- so a
    run started at 23:59 contributes 0 whole paper-days until the
    following day; this is an anti-inflation gate, and crediting a
    partial day as a full one is exactly the one-sided rounding the audit
    finding is about. Clamped to >= 0 so a clock skewed behind the first
    order's own timestamp can't produce a negative day count. Empty
    ``runs_jsonl`` or no ``simulated=false`` line yields ``paper_days=0``
    and ``first_order_ts=None``.

    ``resolved_months``: a traded month is read off EVIDENCE the run
    ledger carries about the actual contract traded, never off the run's
    own ``ts_utc`` -- see "fix round 1" below for why that proxy was
    rejected. Two additive fields (``scripts/run_ibkr_paper.py``'s append
    site, audit finding M2b fix round 1 / controller ruling R13) carry
    that evidence:

    - each order's ``question_id`` (shaped ``"gtm:<YYYY-MM>:<strike>"`` --
      ``EdgeSignal.question_id``, threaded through ``TradeDecision`` and
      into the order record unchanged);
    - the run-level ``open_groups`` list (the sorted ``by_group`` keys
      from the ``load_open_commitments`` call the entrypoint already
      makes, e.g. ``["gtm:2026-12"]``) -- this covers a month whose order
      was placed in an EARLIER run and is still resting: placement
      idempotence (``drop_already_committed``) means it correctly never
      re-appears in a LATER run's own ``orders``, so without
      ``open_groups`` that month's evidence would never surface in any
      run record at all.

    Both are parsed with the same ``"gtm:<YYYY-MM>"`` pattern
    (:func:`_extract_gtm_month`). Fix round 2 (audit finding M2b,
    controller ruling R14 -- extends R13) applies two FURTHER filters on
    top of that evidence, each delay-only (they can only shrink the
    traded-month set, never grow it):

    - ONLY records with ``simulated`` exactly ``False`` contribute
      evidence at all -- neither their ``orders`` nor their
      ``open_groups``. Fix round 1 argued a simulated-only month
      couldn't inflate the gate because ``paper_days`` stays 0 without a
      real run; that argument is FALSE once ANY real run has ever
      happened -- ``paper_days`` then reflects that real run's own start
      date, and every ``--simulate`` rehearsal run since then could
      still add months for free. Rehearsal fills must never land in the
      measured book (the same invariant already upheld for
      ``trade_log``/``positions``), so rehearsal evidence is excluded
      here too, root and branch, regardless of whether a real run has
      started the ``paper_days`` clock.
    - Within a real (``simulated=False``) record, only an order whose
      ``status`` is ``"placed"`` or ``"filled"`` counts
      (:data:`_COUNTED_ORDER_STATUSES`). ``TradingExecutor`` attaches
      ``TradeDecision.order`` as soon as the risk check passes -- BEFORE
      operator approval and before venue placement can fail -- so an
      order the operator declined (``"pending"``, never approved) or the
      venue rejected (``"rejected"``) still appears in ``orders[]`` (the
      writer's own filter is only ``d.order is not None``) but
      represents no actual bet and must not credit its month.

    A traded month is RESOLVED once the label ledger carries a
    first-print (``is_revision=False``) row for ``series == "gtm"`` at
    that exact ``"YYYY-MM"`` period (:func:`climate.storage.load_ledger`).
    ``labels_root=None`` or a ledger file that doesn't exist yet yields
    ``resolved_months=0`` -- it never falls back to the real data root
    (:func:`climate.paths.data_root`'s default), which would defeat
    ``tmp_path``-only tests.

    A LEGACY run record written before fix round 1 has neither
    ``question_id`` on its orders nor an ``open_groups`` key, so it
    contributes NOTHING to ``resolved_months`` regardless of the R14
    filters above. Every one of these gaps -- legacy shape, a simulated
    record, an unplaced/unfilled order -- is intentionally asymmetric:
    missing or filtered evidence can only ever DELAY go-live
    (undercounting resolved months), never hasten it. Contrast with the
    rejected calendar-month proxy this function used before fix round 1,
    which read the traded month off the RUN record's own ``ts_utc`` --
    wrong because a paper run trades whatever month's contract is
    currently on the ladder, not necessarily the run's own calendar month
    (a September run can trade the December contract, ``gtm:2026-12``);
    that proxy counted 2026-09 as traded while the actual bet, 2026-12,
    hadn't even happened yet, and 2026-09's own label prints in
    mid-October -- crediting a resolved month before any real bet on it
    had resolved. That is anti-conservative (it weakens a go-live gate)
    and was rejected in review.

    Missing/empty ``runs_jsonl`` yields ``PaperProgress(0, 0, None)``.
    Malformed JSON lines in ``runs_jsonl`` are skipped, not fatal.

    Everything time-derived comes from the ``now`` PARAMETER -- never
    ``datetime.now()``/``date.today()`` inside this function -- so a
    frozen-clock test stays valid forever.
    """
    records = list(_iter_run_records(Path(runs_jsonl)))

    first_order_ts: str | None = None
    for record in records:
        if record.get("simulated") is False:
            ts = record.get("ts_utc")
            if isinstance(ts, str) and _parse_ts(ts) is not None:
                first_order_ts = ts
                break

    paper_days = 0
    if first_order_ts is not None:
        first_ts = _parse_ts(first_order_ts)
        if first_ts is not None:
            paper_days = max(0, (now - first_ts).days)

    traded_months: set[str] = set()
    for record in records:
        # R14: a simulated (rehearsal) record contributes NO evidence at
        # all -- not its orders, not its open_groups. Mirrors the strict
        # identity check paper_days already uses above.
        if record.get("simulated") is not False:
            continue
        for order in record.get("orders") or []:
            if not isinstance(order, dict):
                continue
            if order.get("status") not in _COUNTED_ORDER_STATUSES:
                continue  # R14: pending/rejected/cancelled never traded
            month = _extract_gtm_month(order.get("question_id"))
            if month is not None:
                traded_months.add(month)
        for group in record.get("open_groups") or []:
            month = _extract_gtm_month(group)
            if month is not None:
                traded_months.add(month)

    resolved_months = 0
    if traded_months and labels_root is not None:
        labels_root = Path(labels_root)
        ledger_file = labels_root / "labels" / "labels_ledger.parquet"
        if ledger_file.exists():
            ledger = load_ledger(labels_root)
            gtm_first_print = set(
                ledger.loc[(ledger["series"] == "gtm") & (~ledger["is_revision"]), "period"]
            )
            resolved_months = len(traded_months & gtm_first_print)

    return PaperProgress(
        paper_days=paper_days,
        resolved_months=resolved_months,
        first_order_ts=first_order_ts,
    )


__all__ = ["PaperProgress", "compute_paper_progress"]
