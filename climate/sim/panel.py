"""A time-ordered panel of recorded GTM quotes: recorder sweeps, hourly bars in their gaps."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from climate.ladder import GTM_SYMBOL, list_sweep_files, sweep_ts_from_path

PANEL_COLUMNS = ["ts", "source", "con_id", "symbol", "expiry", "strike", "right",
                 "bid", "ask", "bid_size", "ask_size"]
SWEEP = "sweep"
HOURLY = "hourly"  # BID_ASK bar: bid = time-average bid, ask = time-average ask
_SWEEP_COLUMNS = ["con_id", "symbol", "expiry", "strike", "right", "bid", "ask",
                  "bid_size", "ask_size"]
_HOURLY_COLUMNS = ["ts_utc", "con_id", "symbol", "expiry", "strike", "right",
                   "what_to_show", "open", "close"]


def _read(path, wanted: list[str], filters) -> pd.DataFrame:
    """The `wanted` columns a file has, the rest as NaN (older files lack the size columns)."""
    names = set(pq.read_schema(path).names)
    table = pq.read_table(path, columns=[c for c in wanted if c in names], filters=filters)
    frame = table.to_pandas()
    for col in wanted:
        if col not in frame.columns:
            frame[col] = np.nan
    return frame[wanted]


def _partition_date(path: str | Path) -> date | None:
    text = str(path).replace("\\", "/")
    marker = text.find("date=")
    if marker < 0:
        return None
    try:
        return date.fromisoformat(text[marker + 5:marker + 15])
    except ValueError:
        return None


def _in_window(day: date | None, start: date | None, end: date | None) -> bool:
    if day is None:
        return False
    return (start is None or day >= start) and (end is None or day <= end)


def _clean(frame: pd.DataFrame, cols) -> pd.DataFrame:
    """IB reports a missing price or size as NaN or -1; both become NaN."""
    for col in cols:
        values = pd.to_numeric(frame[col], errors="coerce")
        frame[col] = values.where(values >= 0)
    return frame


def load_sweeps(ticks_root: str | Path, *, symbol: str = GTM_SYMBOL,
                start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Every recorder sweep row of `symbol`, stamped with its sweep time (the file's name)."""
    frames = []
    for path in list_sweep_files(ticks_root):
        if not _in_window(_partition_date(path), start, end):
            continue
        ts = sweep_ts_from_path(path)
        if ts is None:
            continue
        frame = _read(path, _SWEEP_COLUMNS, [("symbol", "==", symbol)])
        if frame.empty:
            continue
        frame.insert(0, "ts", pd.Timestamp(ts))
        frame.insert(1, "source", SWEEP)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=PANEL_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return _clean(out, ["bid", "ask", "bid_size", "ask_size"])[PANEL_COLUMNS]


def load_hourly(ticks_root: str | Path, *, symbol: str = GTM_SYMBOL,
                start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Hourly BID_ASK bars of `symbol` as quotes stamped at the bar's END (its start + 1 h)."""
    pattern = os.path.join(str(ticks_root), "quotes_hourly", "date=*", "*.parquet")
    frames = []
    for raw in sorted(glob.glob(pattern)):
        if not _in_window(_partition_date(raw), start, end):
            continue
        frame = _read(raw, _HOURLY_COLUMNS,
                      [("symbol", "==", symbol), ("what_to_show", "==", "BID_ASK")])
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=PANEL_COLUMNS)
    bars = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "ts": pd.to_datetime(bars["ts_utc"], utc=True) + pd.Timedelta(hours=1),
        "source": HOURLY,
        "con_id": bars["con_id"], "symbol": bars["symbol"], "expiry": bars["expiry"],
        "strike": bars["strike"], "right": bars["right"],
        "bid": bars["open"], "ask": bars["close"],
        "bid_size": np.nan, "ask_size": np.nan,
    })
    return _clean(out, ["bid", "ask"])[PANEL_COLUMNS]


def merge_sources(sweeps: pd.DataFrame, hourly: pd.DataFrame) -> pd.DataFrame:
    """Sweeps, plus each hourly snapshot whose hour holds no sweep at all."""
    frames = [f for f in (sweeps, hourly) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=PANEL_COLUMNS)
    if not hourly.empty and not sweeps.empty:
        sweep_times = np.sort(pd.to_datetime(sweeps["ts"], utc=True).unique())
        bar_ends = pd.to_datetime(hourly["ts"], utc=True)
        # a sweep in (end - 1 h, end] covers the bar
        first_after = np.searchsorted(sweep_times, (bar_ends - pd.Timedelta(hours=1)).to_numpy(),
                                      side="right")
        covered = (first_after < len(sweep_times))
        covered[covered] = sweep_times[first_after[covered]] <= bar_ends.to_numpy()[covered]
        frames = [sweeps, hourly[~covered]]
    out = pd.concat(frames, ignore_index=True)
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["con_id"] = out["con_id"].astype("int64")
    out["strike"] = pd.to_numeric(out["strike"], errors="coerce")
    out["expiry"] = out["expiry"].astype(str)
    out = out.drop_duplicates(["ts", "con_id"], keep="first")
    return out.sort_values(["ts", "con_id"]).reset_index(drop=True)


@dataclass(frozen=True)
class Contract:
    """What the panel knows about one leg."""

    con_id: int
    symbol: str
    expiry: str
    strike: float
    right: str  # C = YES, P = NO
    partner: int | None  # the other leg of the same (expiry, strike)


class QuotePanel:
    """Quotes by snapshot time, with fast lookups for the replay."""

    def __init__(self, frame: pd.DataFrame):
        frame = frame.sort_values(["ts", "con_id"]).reset_index(drop=True)
        self.frame = frame
        self.times = pd.DatetimeIndex(frame["ts"].unique()).sort_values()
        self._groups = {ts: g for ts, g in frame.groupby("ts", sort=True)}
        self._quotes: dict[pd.Timestamp, dict[int, tuple]] = {
            ts: {int(r.con_id): (r.bid, r.ask, r.ask_size, r.source)
                 for r in g.itertuples(index=False)}
            for ts, g in self._groups.items()
        }
        two_sided = frame[np.isfinite(frame["bid"].astype(float))
                          & np.isfinite(frame["ask"].astype(float))]
        self._by_con: dict[int, tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]] = {
            int(con): (pd.DatetimeIndex(g["ts"]), g["bid"].to_numpy(float), g["ask"].to_numpy(float))
            for con, g in two_sided.groupby("con_id", sort=False)
        }

    @property
    def empty(self) -> bool:
        return len(self.times) == 0

    def index_at_or_before(self, t: datetime) -> int:
        """Position of the latest snapshot at or before `t`, -1 when there is none."""
        return int(self.times.searchsorted(pd.Timestamp(t), side="right")) - 1

    def latest_at(self, t: datetime, max_age: timedelta) -> tuple[pd.Timestamp, pd.DataFrame] | None:
        """The latest snapshot at or before `t`, if it is at most `max_age` old."""
        i = self.index_at_or_before(t)
        if i < 0:
            return None
        ts = self.times[i]
        if pd.Timestamp(t) - ts > max_age:
            return None
        return ts, self._groups[ts]

    def quote(self, ts: pd.Timestamp, con_id: int) -> tuple | None:
        """(bid, ask, ask_size, source) of one leg at one snapshot."""
        return self._quotes.get(ts, {}).get(int(con_id))

    def mid_at_or_after(self, t: datetime, con_id: int, horizon: timedelta) -> float:
        """The leg's first two-sided mid at or after `t` and within `horizon`; NaN if none."""
        series = self._by_con.get(int(con_id))
        if series is None:
            return float("nan")
        times, bid, ask = series
        i = int(times.searchsorted(pd.Timestamp(t), side="left"))
        if i >= len(times) or times[i] > pd.Timestamp(t) + horizon:
            return float("nan")
        return float((bid[i] + ask[i]) / 2.0)

    def last_quote(self, con_id: int, until: datetime | None = None) -> tuple | None:
        """The leg's last two-sided quote at or before `until` (default: the end)."""
        series = self._by_con.get(int(con_id))
        if series is None:
            return None
        times, bid, ask = series
        i = len(times) - 1 if until is None else \
            int(times.searchsorted(pd.Timestamp(until), side="right")) - 1
        if i < 0:
            return None
        return float(bid[i]), float(ask[i]), times[i]

    def contracts(self) -> dict[int, Contract]:
        """Each leg's last-seen description, paired with its other leg."""
        last = self.frame.drop_duplicates("con_id", keep="last")
        key = {(r.expiry, round(float(r.strike), 6), r.right): int(r.con_id)
               for r in last.itertuples(index=False)}
        out = {}
        for r in last.itertuples(index=False):
            other = "P" if r.right == "C" else "C"
            out[int(r.con_id)] = Contract(
                con_id=int(r.con_id), symbol=str(r.symbol), expiry=str(r.expiry),
                strike=float(r.strike), right=str(r.right),
                partner=key.get((r.expiry, round(float(r.strike), 6), other)),
            )
        return out


def load_panel(ticks_root: str | Path, *, start: date | None = None, end: date | None = None,
               hourly: bool = True, symbol: str = GTM_SYMBOL) -> QuotePanel:
    """Sweeps (and hourly bars in their gaps) of `symbol` between `start` and `end`, inclusive."""
    sweeps = load_sweeps(ticks_root, symbol=symbol, start=start, end=end)
    bars = load_hourly(ticks_root, symbol=symbol, start=start, end=end) if hourly else \
        pd.DataFrame(columns=PANEL_COLUMNS)
    return QuotePanel(merge_sources(sweeps, bars))


__all__ = [
    "HOURLY",
    "PANEL_COLUMNS",
    "SWEEP",
    "Contract",
    "QuotePanel",
    "load_hourly",
    "load_panel",
    "load_sweeps",
    "merge_sources",
]
