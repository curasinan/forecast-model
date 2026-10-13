"""Go-live gate inputs computed from the run/label ledgers (audit finding M2b)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from climate.storage import load_ledger

_GTM_MONTH_RE = re.compile(r"^gtm:(\d{4}-\d{2})(?::|$)")

_COUNTED_ORDER_STATUSES = frozenset({"placed", "filled"})


@dataclass(frozen=True)
class PaperProgress:
    """Computed go-live gate inputs (see module docstring)."""

    paper_days: int
    resolved_months: int
    first_order_ts: str | None


def _parse_ts(text: str) -> datetime | None:
    """Best-effort ISO-8601 parse."""
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
    """Best-effort 'YYYY-MM' extraction via ``_GTM_MONTH_RE``."""
    if not isinstance(text, str):
        return None
    match = _GTM_MONTH_RE.match(text)
    return match.group(1) if match else None


def _iter_run_records(runs_jsonl: Path):
    """Yield parsed dict records from an ``ibkr_paper_runs.jsonl`` file."""
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
    """Compute go-live gate inputs from the run and label ledgers (M2b)."""
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
