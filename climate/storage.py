"""Append-only first-print label ledger (Rule 603(e) vintage discipline).

Settlement on ForecastEx climate contracts is irrevocable: the FIRST
published value governs (docs/venue_semantics.md, Rule 603(e)). The
ledger therefore never overwrites:

- A period's first row is its vintage: 'live_first_print' when captured
  by a live incremental run (within ~24h of publication, so effectively
  the first print), or 'backfill_current_vintage' when backfilled from
  history (which can only reflect the CURRENT revised values, not what
  was first published).
- A later fetch showing a DIFFERENT value for an already-seen period
  appends an is_revision=True row; the original row is never touched.

latest_values() resolves each period to its first-print row -- the
settlement-relevant value -- while revision_history() exposes source
drift separately.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from climate.noaa import LabelObservation
from climate.paths import data_root

VINTAGE_BACKFILL = "backfill_current_vintage"
VINTAGE_LIVE = "live_first_print"

LEDGER_COLUMNS = [
    "series",
    "period",
    "value",
    "first_seen_utc",
    "vintage",
    "is_revision",
    "source_url",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ledger_path(root: Path | None = None) -> Path:
    root = root or data_root()
    return root / "labels" / "labels_ledger.parquet"


def _empty_ledger() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in LEDGER_COLUMNS})
    df["value"] = df["value"].astype("float64")
    df["is_revision"] = df["is_revision"].astype("bool")
    return df


def load_ledger(root: Path | None = None) -> pd.DataFrame:
    """Load the full ledger (row order == append order); empty frame if absent."""
    path = ledger_path(root)
    if not path.exists():
        return _empty_ledger()
    return pd.read_parquet(path)


def _write_ledger(df: pd.DataFrame, root: Path | None = None) -> Path:
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)  # atomic on same volume
    return path


def append_observations(
    rows: Iterable[LabelObservation],
    backfill: bool,
    root: Path | None = None,
) -> dict[str, dict[str, int]]:
    """Append fetched observations under first-print discipline.

    Per (series, period):
      - unseen             -> append (vintage per backfill flag)
      - seen, same value   -> skip (idempotent re-runs)
      - seen, new value    -> append revision row; original untouched

    'Same value' compares against the most recently recorded value for
    the period (first print or latest revision), so an already-recorded
    revision is not re-appended on every run.

    Returns {series: {"new": n, "revisions": n, "skipped": n}}.
    """
    ledger = load_ledger(root)
    last_value: dict[tuple[str, str], float] = {
        (r.series, r.period): r.value for r in ledger.itertuples(index=False)
    }
    now = utc_now_iso()
    vintage = VINTAGE_BACKFILL if backfill else VINTAGE_LIVE
    new_rows: list[dict[str, object]] = []
    summary: dict[str, dict[str, int]] = {}
    for obs in rows:
        counts = summary.setdefault(obs.series, {"new": 0, "revisions": 0, "skipped": 0})
        key = (obs.series, obs.period)
        if key not in last_value:
            new_rows.append(
                {
                    "series": obs.series,
                    "period": obs.period,
                    "value": float(obs.value),
                    "first_seen_utc": now,
                    "vintage": vintage,
                    "is_revision": False,
                    "source_url": obs.source_url,
                }
            )
            counts["new"] += 1
        elif last_value[key] == float(obs.value):
            counts["skipped"] += 1
            continue
        else:
            new_rows.append(
                {
                    "series": obs.series,
                    "period": obs.period,
                    "value": float(obs.value),
                    "first_seen_utc": now,
                    "vintage": vintage,
                    "is_revision": True,
                    "source_url": obs.source_url,
                }
            )
            counts["revisions"] += 1
        last_value[key] = float(obs.value)
    if new_rows:
        addition = pd.DataFrame(new_rows, columns=LEDGER_COLUMNS)
        ledger = addition if ledger.empty else pd.concat([ledger, addition], ignore_index=True)
        _write_ledger(ledger, root)
    return summary


def latest_values(series: str, root: Path | None = None) -> dict[str, float]:
    """period -> settlement-relevant value: the FIRST-PRINT row wins.

    Rule 603(e): the first-published value governs settlement, so
    revisions never override it here. See revision_history() for drift.
    """
    ledger = load_ledger(root)
    sub = ledger[(ledger["series"] == series) & (~ledger["is_revision"])]
    return dict(zip(sub["period"], sub["value"]))


def revision_history(series: str, root: Path | None = None) -> pd.DataFrame:
    """All revision rows for a series, in append (observation) order."""
    ledger = load_ledger(root)
    sub = ledger[(ledger["series"] == series) & (ledger["is_revision"])]
    return sub.reset_index(drop=True)
