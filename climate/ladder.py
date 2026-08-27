"""Strike-ladder probabilities for GTM and comparison with recorder quotes.

Research-only: turns a predictive N(mu, sigma) for one target month into
P(YES) per listed strike, joins the latest ForecastEx delayed-quote sweep,
and reports edge / expected value after the flat commission. Nothing here
places orders or imports a venue SDK.

Strike semantics (docs/venue_semantics.md, GTM row): YES iff the NOAA
monthly anomaly is STRICTLY GREATER than the strike. Under a continuous
normal the tie {X == strike} has measure zero, so
    p_yes = P(X > k) = 1 - Phi((k - mu) / sigma)
is exact for the model. The settled NOAA value, however, is printed to
0.01 degC and strikes sit on a 0.1 (or 0.5) grid, so a printed tie
(value 1.10 vs strike 1.1 -> NO) is NOT measure-zero in reality. The
optional ``resolution`` argument shifts the threshold by half a print
step (P(X > k + resolution/2)), which is the first-order rounding
correction; the default keeps the pure continuous form per the task
contract.

Contract -> target-month mapping (empirical, from
data/ibkr_universe_live.json and the recorder's contracts_cache.db,
inspected 2026-08-23):

    localSymbol          lastTradeDate  contractMonth  realExpiration
    GTM_0726_0.6_YES     20260811       202607         (rolled off)
    GTM_0826_-1.5_YES    20260910       202608
    GTM_0926_-1.5_YES    20261009       202609
    GTM_1026_0_YES       20261112       202610
    GTM_1226_0.5_YES     20270212       202612         2027-02-12 10:00 CT

The MMYY inside the localSymbol equals contractMonth (YYYYMM) and is the
TARGET month: NOAA publishes month M in the first half of M+1 (10:00 CT),
and for 0726/0826/0926/1026 the expiry falls 9-12 days after the end of
the labelled month, i.e. on the publication day of that month's value.
July-2026 settles at 1.18 degC (first seen in the ledger 2026-08-17)
against a 0.6..1.4 ladder, consistent with July being the target.

The expiry ALONE is ambiguous: GTM_1226 expires 2027-02-12, ~6 weeks
after December ends, which is also where a hypothetical GTM_0127 would
expire (January publishes ~Feb 12). The "previous month of the expiry"
heuristic therefore maps 20270212 to 2027-01, contradicting the label.
The label (localSymbol / contractMonth) is authoritative; the expiry
heuristic is a fallback only and is marked as such in the output.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd
from scipy.stats import norm

from src.domain.fees import FeeModel, FlatCommissionFeeModel

# Verified 2026-08-16 via whatIf probes (docs/venue_semantics.md): flat
# $0.01/contract commission, YES+NO mids sum to ~1.00. Research default
# only -- no staleness guard, so anything trade-adjacent must pass a
# FeeModel built from runtime-verified values instead.
DEFAULT_COMMISSION_PER_CONTRACT = 0.01

# A two-sided spread wider than this is not a usable mid on a 1-cent tick.
MAX_SANE_SPREAD = 0.10

YES_RIGHT = "C"
NO_RIGHT = "P"
GTM_SYMBOL = "GTM"

SOURCE_LABEL = "label"
SOURCE_EXPIRY_HEURISTIC = "expiry_heuristic"


# --------------------------------------------------------------------------- #
# Probability
# --------------------------------------------------------------------------- #


def p_yes(strike, mu: float, sigma: float, resolution: float | None = None):
    """P(anomaly > strike) under N(mu, sigma); scalar or array over strikes.

    Strict '>' per the T&C. With ``resolution`` (e.g. 0.01 for NOAA's
    two-decimal print) the threshold becomes strike + resolution/2 to
    approximate rounding of the settled value; default None is the pure
    continuous form, where ties are measure-zero.
    """
    if not np.isfinite(sigma) or sigma <= 0:
        raise ValueError(f"sigma must be finite and > 0, got {sigma!r}")
    if not np.isfinite(mu):
        raise ValueError(f"mu must be finite, got {mu!r}")
    k = np.asarray(strike, dtype=float)
    if resolution is not None:
        if resolution < 0:
            raise ValueError("resolution cannot be negative")
        k = k + resolution / 2.0
    out = norm.sf((k - mu) / sigma)  # 1 - Phi, numerically stable in the tail
    if np.ndim(out) == 0:
        return float(out)
    return out


# --------------------------------------------------------------------------- #
# Contract -> target month
# --------------------------------------------------------------------------- #


def target_month_from_local_symbol(local_symbol: str) -> str | None:
    """'GTM_0726_0.6_YES' -> '2026-07' (MMYY label = target month).

    Returns None when the string does not carry an MMYY label (e.g. the
    bare underlying 'GTM').
    """
    parts = str(local_symbol).split("_")
    if len(parts) < 2:
        return None
    label = parts[1]
    if len(label) != 4 or not label.isdigit():
        return None
    mm, yy = int(label[:2]), int(label[2:])
    if not 1 <= mm <= 12:
        return None
    return f"20{yy:02d}-{mm:02d}"


def target_month_from_contract_month(contract_month: str) -> str | None:
    """'202607' -> '2026-07' (IBKR contractMonth is the target month)."""
    s = str(contract_month).strip()
    if len(s) != 6 or not s.isdigit():
        return None
    yyyy, mm = int(s[:4]), int(s[4:])
    if not 1 <= mm <= 12:
        return None
    return f"{yyyy:04d}-{mm:02d}"


def target_month_from_expiry(expiry: str) -> str | None:
    """Fallback: 'YYYYMMDD' expiry -> the calendar month before it.

    Correct for the 0726/0826/0926/1026 listings (publication ~9-12 days
    into M+1) but WRONG for GTM_1226, which expires 2027-02-12 (maps to
    2027-01 here, label says 2026-12). Use only when no label is known;
    callers see ``target_month_source == 'expiry_heuristic'``.
    """
    s = str(expiry).strip()
    if len(s) < 6 or not s[:6].isdigit():
        return None
    yyyy, mm = int(s[:4]), int(s[4:6])
    if not 1 <= mm <= 12:
        return None
    mm -= 1
    if mm == 0:
        mm, yyyy = 12, yyyy - 1
    return f"{yyyy:04d}-{mm:02d}"


def target_month_for_contract(
    local_symbol: str | None = None,
    contract_month: str | None = None,
    expiry: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve (target_month, source) preferring label > contractMonth > expiry."""
    if local_symbol:
        tm = target_month_from_local_symbol(local_symbol)
        if tm:
            return tm, SOURCE_LABEL
    if contract_month:
        tm = target_month_from_contract_month(contract_month)
        if tm:
            return tm, SOURCE_LABEL
    if expiry:
        tm = target_month_from_expiry(expiry)
        if tm:
            return tm, SOURCE_EXPIRY_HEURISTIC
    return None, None


# --------------------------------------------------------------------------- #
# Recorder data discovery
# --------------------------------------------------------------------------- #


def list_sweep_files(ticks_root: str | Path) -> list[Path]:
    """All sweep parquets under quotes/date=YYYY-MM-DD/, oldest first.

    The recorder names files quotes-HHMMSSZ.parquet inside a UTC date
    partition, so (partition, filename) sorts chronologically.
    """
    pattern = os.path.join(str(ticks_root), "quotes", "date=*", "*.parquet")
    return [Path(p) for p in sorted(glob.glob(pattern))]


def latest_sweep_path(ticks_root: str | Path) -> Path:
    files = list_sweep_files(ticks_root)
    if not files:
        raise FileNotFoundError(f"no sweep parquets under {ticks_root}/quotes/date=*/")
    return files[-1]


_SWEEP_PATH_RE = re.compile(
    r"date=(\d{4}-\d{2}-\d{2})[\\/]+quotes-(\d{2})(\d{2})(\d{2})Z\.parquet$"
)


def sweep_ts_from_path(sweep_path: str | Path | None) -> datetime | None:
    """UTC timestamp encoded in a recorder sweep path's partition + filename.

    e.g. '.../quotes/date=2026-08-27/quotes-204638Z.parquet' ->
    2026-08-27T20:46:38Z (staleness gate, audit finding H2). Returns None
    for an empty/None path -- ``latest_gtm_quotes`` leaves ``sweep_path``
    as ``""`` when its caller injects ``sweep_df`` directly (tests, or
    ``climate.paper.run_paper``'s ``quotes_df`` bypass) -- or for a path
    that does not match the recorder's own naming from
    :func:`list_sweep_files`.
    """
    if not sweep_path:
        return None
    m = _SWEEP_PATH_RE.search(str(sweep_path).replace("\\", "/"))
    if not m:
        return None
    date_s, hh, mm, ss = m.groups()
    year, month, day = (int(part) for part in date_s.split("-"))
    return datetime(year, month, day, int(hh), int(mm), int(ss), tzinfo=timezone.utc)


def load_contract_labels(
    ticks_root: str | Path | None = None,
    universe_json: str | Path | None = None,
    symbol: str = GTM_SYMBOL,
) -> pd.DataFrame:
    """con_id -> local_symbol (and expiry) for one symbol.

    Sources, merged in order: the recorder's contracts_cache.db under
    ``ticks_root`` (read-only), then a Stage-0 universe snapshot JSON
    (data/ibkr_universe_live.json shape) if given. Either may be absent;
    the result is then empty and callers fall back to the expiry
    heuristic.
    """
    frames: list[pd.DataFrame] = []
    if ticks_root is not None:
        db = Path(ticks_root) / "contracts_cache.db"
        if db.exists():
            uri = f"file:{db.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                rows = conn.execute(
                    'SELECT con_id, local_symbol, expiry FROM contracts WHERE symbol = ?',
                    (symbol,),
                ).fetchall()
            frames.append(pd.DataFrame(rows, columns=["con_id", "local_symbol", "expiry"]))
    if universe_json is not None and Path(universe_json).exists():
        import json

        data = json.loads(Path(universe_json).read_text(encoding="utf-8"))
        contracts = data.get("contracts", {})
        values = contracts.values() if isinstance(contracts, dict) else contracts
        recs = [
            {
                "con_id": int(c["conId"]),
                "local_symbol": c.get("localSymbol") or "",
                "expiry": c.get("lastTradeDateOrContractMonth") or "",
            }
            for c in values
            if c.get("symbol") == symbol and c.get("conId")
        ]
        frames.append(pd.DataFrame(recs, columns=["con_id", "local_symbol", "expiry"]))
    if not frames:
        return pd.DataFrame(columns=["con_id", "local_symbol", "expiry"])
    out = pd.concat(frames, ignore_index=True)
    out = out[out["local_symbol"].astype(str).str.contains("_")]
    out = out.drop_duplicates("con_id", keep="first").reset_index(drop=True)
    out["con_id"] = out["con_id"].astype("int64")
    return out


def _clean_price(s: pd.Series) -> pd.Series:
    """IB reports missing quotes as nan or -1 (recorded raw); both -> NaN."""
    s = pd.to_numeric(s, errors="coerce")
    return s.where(s >= 0)


def _mid(bid: pd.Series, ask: pd.Series) -> pd.Series:
    return (bid + ask) / 2.0


def latest_gtm_quotes(
    ticks_root: str | Path,
    sweep_df: pd.DataFrame | None = None,
    labels: pd.DataFrame | None = None,
    universe_json: str | Path | None = None,
) -> pd.DataFrame:
    """YES legs (right=='C') of symbol GTM from the latest sweep, with NO mids.

    One row per YES contract. Columns: the sweep's own (ts_utc, con_id,
    symbol, expiry, strike, right, bid, ask, last, close, bid_size,
    ask_size, data_type) with bid/ask sentinel-cleaned, plus yes_mid,
    no_con_id, no_bid, no_ask, no_mid (same expiry+strike, right=='P';
    NaN when absent), yes_no_mid_sum, spread, local_symbol,
    target_month, target_month_source, sweep_path.

    ``sweep_df`` bypasses disk (tests); ``labels`` overrides the
    con_id -> local_symbol lookup (default: contracts_cache.db under
    ticks_root plus ``universe_json`` if given).
    """
    if sweep_df is None:
        path = latest_sweep_path(ticks_root)
        sweep_df = pd.read_parquet(path)
        sweep_path = str(path)
    else:
        sweep_path = ""
    df = sweep_df[sweep_df["symbol"] == GTM_SYMBOL].copy()
    for col in ("bid", "ask", "last", "close"):
        if col in df.columns:
            df[col] = _clean_price(df[col])
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["expiry"] = df["expiry"].astype(str)
    df["con_id"] = df["con_id"].astype("int64")

    yes = df[df["right"] == YES_RIGHT].copy()
    no = df[df["right"] == NO_RIGHT][["con_id", "expiry", "strike", "bid", "ask"]].rename(
        columns={"con_id": "no_con_id", "bid": "no_bid", "ask": "no_ask"}
    )
    # Strikes are floats from the venue; round to kill 0.30000000000000004-style
    # mismatches between the two legs of one market.
    yes["_k"] = yes["strike"].round(6)
    no["_k"] = no["strike"].round(6)
    no = no.drop_duplicates(["expiry", "_k"], keep="last")
    out = yes.merge(no.drop(columns=["strike"]), on=["expiry", "_k"], how="left")
    out = out.drop(columns=["_k"])

    out["yes_mid"] = _mid(out["bid"], out["ask"])
    out["no_mid"] = _mid(out["no_bid"], out["no_ask"])
    out["yes_no_mid_sum"] = out["yes_mid"] + out["no_mid"]
    out["spread"] = out["ask"] - out["bid"]
    out["no_con_id"] = out["no_con_id"].astype("Int64")

    if labels is None:
        labels = load_contract_labels(ticks_root, universe_json=universe_json)
    lookup = {}
    if labels is not None and len(labels):
        lookup = dict(zip(labels["con_id"].astype("int64"), labels["local_symbol"]))
    out["local_symbol"] = out["con_id"].map(lookup)
    resolved = [
        target_month_for_contract(local_symbol=ls if isinstance(ls, str) else None, expiry=ex)
        for ls, ex in zip(out["local_symbol"], out["expiry"])
    ]
    out["target_month"] = [tm for tm, _ in resolved]
    out["target_month_source"] = [src for _, src in resolved]
    out["sweep_path"] = sweep_path
    out = out.sort_values(["target_month", "expiry", "strike"]).reset_index(drop=True)
    return out


# --------------------------------------------------------------------------- #
# Ladder table
# --------------------------------------------------------------------------- #

LADDER_COLUMNS = [
    "con_id",
    "target_month",
    "strike",
    "p_model",
    "yes_mid",
    "no_mid",
    "yes_no_mid_sum",
    "edge",
    "ev_per_contract_after_fee",
    "suspect_quote",
    # Extras beyond the core contract: the NO side of the same market and
    # the inputs behind the flag, so a reader can audit a row.
    "edge_no",
    "ev_no_per_contract_after_fee",
    "bid",
    "ask",
    "spread",
    "target_month_source",
]


def _suspect(bid: pd.Series, ask: pd.Series, max_spread: float) -> pd.Series:
    spread = ask - bid
    missing = bid.isna() | ask.isna()
    wide = spread > max_spread
    crossed = spread < 0
    return (missing | wide | crossed).astype(bool)


def ladder_table(
    quotes_df: pd.DataFrame,
    predictive: Mapping[str, tuple[float, float]],
    fee_model: FeeModel | None = None,
    resolution: float | None = None,
    max_spread: float = MAX_SANE_SPREAD,
) -> pd.DataFrame:
    """Model vs market per YES contract.

    ``quotes_df`` is latest_gtm_quotes() output (needs con_id, target_month,
    strike, bid, ask, yes_mid, no_mid). ``predictive`` maps 'YYYY-MM' ->
    (mu, sigma) on the NOAA degC anomaly scale; rows whose target_month
    has no entry are dropped.

    edge = p_model - yes_mid (buy-YES view);
    ev_per_contract_after_fee = edge - fee_model.execution_cost(yes_mid, 1)
    for ONE contract paying $1.00 on YES. The NO-side twins use
    (1 - p_model) and no_mid. suspect_quote is True when bid or ask is
    missing (nan / IB -1 sentinel), the spread exceeds ``max_spread``, or
    the book is crossed; edge/EV on such rows are NaN by construction
    when a side is missing and should not be acted on otherwise.
    """
    fee_model = fee_model or FlatCommissionFeeModel(DEFAULT_COMMISSION_PER_CONTRACT)
    q = quotes_df.copy()
    q = q[q["target_month"].isin(list(predictive.keys()))].copy()
    if q.empty:
        return pd.DataFrame(columns=LADDER_COLUMNS)

    q["bid"] = _clean_price(q["bid"])
    q["ask"] = _clean_price(q["ask"])
    if "yes_mid" not in q.columns:
        q["yes_mid"] = _mid(q["bid"], q["ask"])
    if "no_mid" not in q.columns:
        q["no_mid"] = np.nan
    if "spread" not in q.columns:
        q["spread"] = q["ask"] - q["bid"]
    if "target_month_source" not in q.columns:
        q["target_month_source"] = None

    p = np.empty(len(q), dtype=float)
    for i, (tm, k) in enumerate(zip(q["target_month"], q["strike"])):
        mu, sigma = predictive[tm]
        p[i] = p_yes(float(k), float(mu), float(sigma), resolution=resolution)
    q["p_model"] = p
    q["yes_no_mid_sum"] = q["yes_mid"] + q["no_mid"]
    q["edge"] = q["p_model"] - q["yes_mid"]
    q["edge_no"] = (1.0 - q["p_model"]) - q["no_mid"]

    def _fee(price: float) -> float:
        if not np.isfinite(price):
            return np.nan
        return fee_model.execution_cost(float(price), 1)

    q["ev_per_contract_after_fee"] = q["edge"] - q["yes_mid"].map(_fee)
    q["ev_no_per_contract_after_fee"] = q["edge_no"] - q["no_mid"].map(_fee)
    q["suspect_quote"] = _suspect(q["bid"], q["ask"], max_spread)

    out = q[LADDER_COLUMNS].sort_values(["target_month", "strike"]).reset_index(drop=True)
    return out


__all__ = [
    "DEFAULT_COMMISSION_PER_CONTRACT",
    "MAX_SANE_SPREAD",
    "LADDER_COLUMNS",
    "p_yes",
    "target_month_from_local_symbol",
    "target_month_from_contract_month",
    "target_month_from_expiry",
    "target_month_for_contract",
    "list_sweep_files",
    "latest_sweep_path",
    "sweep_ts_from_path",
    "load_contract_labels",
    "latest_gtm_quotes",
    "ladder_table",
]
