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
  one line per paper run, each carrying ``ts_utc``, ``simulated``, and an
  ``orders`` list.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from climate.storage import load_ledger


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

    ``resolved_months``: the ``ibkr_paper_runs.jsonl`` schema
    (``scripts/run_ibkr_paper.py``'s append site) has no per-order
    target-month field -- only an opaque venue ``market_id`` -- so the
    traded month is read off each RUN record's own ``ts_utc`` (one paper
    run is one pass at the then-current, single live GTM ladder). A run's
    calendar month (``YYYY-MM`` of its ``ts_utc``) counts as "traded" when
    that run placed at least one order (non-empty ``orders``); simulated
    rehearsal runs count HERE too (unlike ``paper_days``) because the
    brief scopes this half of the definition to "any run's orders" -- a
    simulated-only month can't inflate the overall gate on its own since
    ``paper_days`` stays 0 without a real run, and
    ``evaluate_hindcast_go_live`` requires both thresholds to clear. A
    traded month is RESOLVED once the label ledger carries a first-print
    (``is_revision=False``) row for ``series == "gtm"`` at that exact
    ``"YYYY-MM"`` period (:func:`climate.storage.load_ledger`).
    ``labels_root=None`` or a ledger file that doesn't exist yet yields
    ``resolved_months=0`` -- it never falls back to the real data root
    (:func:`climate.paths.data_root`'s default), which would defeat
    ``tmp_path``-only tests.

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
        orders = record.get("orders")
        if not orders:
            continue
        parsed = _parse_ts(record.get("ts_utc"))
        if parsed is None:
            continue
        traded_months.add(f"{parsed.year:04d}-{parsed.month:02d}")

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
