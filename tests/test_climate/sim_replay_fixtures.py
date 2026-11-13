"""Recorder trees and a stub forecaster for the replay tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from climate.sim.forecast import Forecast

YES = {1.2: 801, 1.3: 802, 1.4: 803}
NO = {k: v + 100 for k, v in YES.items()}
DEC_EXPIRY = "20270114"
SEP_EXPIRY = "20261010"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def times(start: datetime, end: datetime, minutes: int = 20) -> list[datetime]:
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=minutes)
    return out


def write_sweep(ticks_root: Path, ts: datetime, quotes: dict, expiry: str, *,
                with_sizes: bool = True, yes_ids: dict | None = None) -> Path:
    """One recorder sweep: YES and NO rows per strike; quotes[k] = (bid, ask, ask_size).

    `yes_ids` maps strike to the YES conId (default YES); each NO leg is its YES conId + 100.
    """
    yes_ids = yes_ids or YES
    rows = []
    for k, (bid, ask, size) in quotes.items():
        con_yes = yes_ids[k]
        for con, right, b, a in ((con_yes, "C", bid, ask), (con_yes + 100, "P", 1 - ask, 1 - bid)):
            row = {"ts_utc": ts.isoformat(), "con_id": con, "symbol": "GTM", "expiry": expiry,
                   "strike": k, "right": right, "bid": round(b, 2), "ask": round(a, 2),
                   "last": None, "close": None, "data_type": "delayed"}
            if with_sizes:
                row.update({"bid_size": 50.0, "ask_size": float(size)})
            rows.append(row)
    path = ticks_root / "quotes" / f"date={ts:%Y-%m-%d}" / f"quotes-{ts:%H%M%S}Z.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def write_ladder(ticks_root: Path, stamps: list[datetime], quotes_at: Callable[[datetime], dict],
                 expiry: str) -> None:
    for ts in stamps:
        write_sweep(ticks_root, ts, quotes_at(ts), expiry)


DEC_QUOTES = {1.2: (0.62, 0.65, 50), 1.3: (0.41, 0.44, 50), 1.4: (0.28, 0.31, 50)}


class StubForecaster:
    """The same forecast at every moment."""

    def __init__(self, predictive: dict[str, tuple[float, float]], init: str = "2026-10"):
        self.predictive = predictive
        self.init = init

    def at(self, t) -> Forecast:
        return Forecast(dict(self.predictive), dict.fromkeys(self.predictive, self.init), "2026-09")
