"""Row-by-row leakage check: every input must be public by the row's decision time."""

from __future__ import annotations

import numpy as np
import pandas as pd

_MAX_ROWS_LISTED = 5


class LeakageError(RuntimeError):
    """A row used an input that was not public at its decision time."""


def leakage_violations(
    frame: pd.DataFrame, decision_col: str, availability_cols: list[str]
) -> pd.DataFrame:
    """Rows with an input public after the decision time or missing, with a reason column."""
    decision = frame[decision_col]
    checks = [(f"{decision_col} missing", decision.isna())]
    for col in availability_cols:
        checks.append((f"{col} missing", frame[col].isna()))
        checks.append((col, frame[col] > decision))
    hits = np.column_stack([mask.to_numpy(dtype=bool) for _, mask in checks])
    rows = np.flatnonzero(hits.any(axis=1))
    bad = frame.iloc[rows].copy()
    bad["reason"] = [
        ", ".join(text for (text, _), hit in zip(checks, hits[row], strict=True) if hit)
        for row in rows
    ]
    return bad


def assert_no_leakage(frame: pd.DataFrame, decision_col: str, availability_cols: list[str]) -> None:
    """Raise LeakageError listing up to five offending rows; return None for a clean frame."""
    bad = leakage_violations(frame, decision_col, availability_cols)
    if bad.empty:
        return
    listed = "; ".join(
        f"row {index}: {reason}" for index, reason in bad["reason"].head(_MAX_ROWS_LISTED).items()
    )
    raise LeakageError(f"{len(bad)} row(s) with an input not public at {decision_col}: {listed}")
