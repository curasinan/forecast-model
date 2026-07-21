"""Maker-fill simulator — did resting limit orders actually fill, and at what cost?

A **measurement tool, not a trading system**. It replays synthetic resting limit
orders against our recorded order-book snapshots and answers one question per price
offset: when we post a limit order at a price and time, does the market subsequently
trade through that price, and what is the adverse-selection cost of the fills we get?
The verdict it produces tells us whether Phase-4's 0.005 half-spread buffer
(``decision/edge_calculator.py``) was optimistic, realistic, or conservative.

Method (frozen; see the plan for the full rationale):

* An order is ``(market_id, side, limit_price P, post_time, 6h window)``.
  ``side_sign = +1`` for BUY, ``-1`` for SELL. ``P = mid_0 - side_sign * delta``
  (a buy rests *below* mid, a sell *above*), where ``mid_0`` is the midpoint at
  ``post_time`` and ``delta`` the offset (0.01 or 0.02).
* **Fill** (conservative, from the book): a BUY fills at the first snapshot strictly
  after ``post_time`` within the window where ``best_ask <= P``; a SELL where
  ``best_bid >= P``. The fill is booked at **P** (the limit) — a resting order filled
  by incoming flow trades at your posted price under price-time priority, never at the
  better touch.
* **Adverse selection**, single consistent decomposition anchored at ``mid_0``
  (exactly observed, immune to fill-instant sampling lag)::

      capture      = delta                              # the offset posted
      drift        = side_sign * (mid_end - mid_0)      # negative = moved against you
      adverse_cost = -drift                             # positive = adverse, in cents
      net_oneleg   = capture + drift = side_sign*(mid_end - P)   # entry-only mark

  ``net_roundtrip = net_oneleg - 0.5*spread_end`` adds an exit half-spread (a real
  unwind is not free). The *test* for systematic adverse selection compares drift
  among FILLED orders to the unconditional drift of all orders at that offset.
* **Data-quality gating** (our snapshots are 15-min-median but 13% of gaps exceed
  60 min): ``mid_end`` is the two-sided midpoint nearest ``post_time+6h`` within
  ±30 min (else the order has no mark-out and is excluded from cost stats); a window
  is ``dense`` iff every gap inside it is ≤60 min. Fill rate is reported over all
  orders (a lower bound — gaps can hide through-trades) and over the dense subset.

Read-only against the DB except for writing results.

Run:
    python -m analysis.fill_simulator [--limit N] [--min-snapshots N]

Outputs:
    analysis/results/fill_simulation.json          (config, funnel, per-offset blocks)
    analysis/results/fill_simulation_by_offset.csv (one row per offset)
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from model.train import load_political_market_ids
from src.utils.dates import parse_iso8601

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "fill_simulator.log"
RESULTS_DIR = REPO_ROOT / "analysis" / "results"
JSON_PATH = RESULTS_DIR / "fill_simulation.json"
CSV_PATH = RESULTS_DIR / "fill_simulation_by_offset.csv"

# --------------------------------------------------------------------------- #
# Frozen parameters
# --------------------------------------------------------------------------- #

HOLDING_WINDOW_HOURS = 6
POST_SPACING_HOURS = 12
OFFSETS: tuple[float, ...] = (0.01, 0.02)   # each posted as a BUY and a SELL
MID_RANGE: tuple[float, float] = (0.05, 0.95)  # excludes pinned books; no clamping
MARKOUT_TOLERANCE_MIN = 30
MAX_INTRA_WINDOW_GAP_MIN = 60
PHASE4_BUFFER = 0.005
REALISTIC_BAND = 0.0005     # |adverse_cost - buffer| within this reads as "realistic"
MIN_SNAPSHOTS = 200

log = logging.getLogger("analysis.fill_simulator")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    log.setLevel(logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    log.addHandler(console)


# --------------------------------------------------------------------------- #
# Pure core (DB-free — tested on synthetic sequences)
#
# A "snapshot" is a dict: {"ts": tz-aware datetime, "best_bid", "best_ask",
# "midpoint", "spread"} (any book field may be None). Lists are time-sorted asc.
# --------------------------------------------------------------------------- #


def _nearest_midpoint(
    snapshots: list[dict],
    ts_list: list[datetime],
    target: datetime,
    tol_seconds: float,
) -> tuple[float | None, float | None]:
    """(midpoint, spread) of the two-sided snapshot nearest ``target`` within tol."""
    lo = bisect.bisect_left(ts_list, target)
    best_gap: float | None = None
    mid: float | None = None
    spr: float | None = None
    # forward from lo, then backward from lo-1; stop each side once outside tol.
    i = lo
    while i < len(ts_list):
        gap = (ts_list[i] - target).total_seconds()
        if gap > tol_seconds:
            break
        if snapshots[i]["midpoint"] is not None and (best_gap is None or gap < best_gap):
            best_gap, mid, spr = gap, snapshots[i]["midpoint"], snapshots[i]["spread"]
        i += 1
    j = lo - 1
    while j >= 0:
        gap = (target - ts_list[j]).total_seconds()
        if gap > tol_seconds:
            break
        if snapshots[j]["midpoint"] is not None and (best_gap is None or gap < best_gap):
            best_gap, mid, spr = gap, snapshots[j]["midpoint"], snapshots[j]["spread"]
        j -= 1
    return mid, spr


def simulate_order(
    snapshots: list[dict],
    post_idx: int,
    side_sign: int,
    delta: float,
    ts_list: list[datetime] | None = None,
    window_hours: int = HOLDING_WINDOW_HOURS,
    markout_tolerance_min: int = MARKOUT_TOLERANCE_MIN,
    max_gap_min: int = MAX_INTRA_WINDOW_GAP_MIN,
) -> dict:
    """Simulate one resting limit order posted at ``snapshots[post_idx]``.

    Returns a flat dict with fill outcome, mark-out drift, and net figures. All
    prices are booked conservatively; see the module docstring for the sign math.
    """
    if ts_list is None:
        ts_list = [s["ts"] for s in snapshots]

    post = snapshots[post_idx]
    post_ts: datetime = post["ts"]
    mid_0: float = post["midpoint"]
    limit_price = mid_0 - side_sign * delta
    window_end = post_ts + timedelta(hours=window_hours)

    # ---- forward scan: fill detection + window density ------------------- #
    filled = False
    time_to_fill_min: float | None = None
    fill_price: float | None = None
    in_window_ts: list[datetime] = []
    for i in range(post_idx + 1, len(snapshots)):
        s = snapshots[i]
        ts = s["ts"]
        if ts <= post_ts:
            continue
        if ts > window_end:
            break
        bid, ask = s["best_bid"], s["best_ask"]
        if bid is not None and ask is not None and ask < bid:
            continue  # crossed book — data error, ignore
        in_window_ts.append(ts)
        if not filled:
            if side_sign > 0:  # BUY fills as the ask trades down to the limit
                if ask is not None and ask <= limit_price:
                    filled = True
                    time_to_fill_min = (ts - post_ts).total_seconds() / 60.0
                    fill_price = limit_price
            else:              # SELL fills as the bid trades up to the limit
                if bid is not None and bid >= limit_price:
                    filled = True
                    time_to_fill_min = (ts - post_ts).total_seconds() / 60.0
                    fill_price = limit_price

    has_forward = bool(in_window_ts)
    if not has_forward:
        dense = False
    else:
        times = [post_ts, *in_window_ts, window_end]
        max_gap = max((b - a).total_seconds() / 60.0 for a, b in zip(times, times[1:]))
        dense = max_gap <= max_gap_min

    # ---- mark-out at window end ------------------------------------------ #
    mid_end, spread_end = _nearest_midpoint(
        snapshots, ts_list, window_end, markout_tolerance_min * 60.0
    )
    has_markout = mid_end is not None
    drift = side_sign * (mid_end - mid_0) if has_markout else None
    net_oneleg = side_sign * (mid_end - limit_price) if has_markout else None
    net_roundtrip = (
        net_oneleg - 0.5 * spread_end
        if (net_oneleg is not None and spread_end is not None)
        else None
    )

    return {
        "side_sign": side_sign,
        "delta": delta,
        "mid_0": mid_0,
        "limit_price": limit_price,
        "filled": filled,
        "time_to_fill_min": time_to_fill_min,
        "fill_price": fill_price,
        "has_forward": has_forward,
        "dense": dense,
        "mid_end": mid_end,
        "has_markout": has_markout,
        "drift": drift,                                     # signed; neg = adverse
        "adverse_cost": (-drift) if drift is not None else None,
        "capture": delta,
        "net_oneleg": net_oneleg,
        "net_roundtrip": net_roundtrip,
    }


def offset_key(side_sign: int, delta: float) -> str:
    """Stable per-offset label, e.g. ``buy_-0.01`` / ``sell_+0.02``."""
    return f"buy_-{delta:.2f}" if side_sign > 0 else f"sell_+{delta:.2f}"


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def buffer_verdict(avg_adverse_cost: float | None) -> str:
    """How the realized adverse cost compares to the frozen 0.005 buffer."""
    if avg_adverse_cost is None:
        return "no_data"
    if avg_adverse_cost > PHASE4_BUFFER + REALISTIC_BAND:
        return "optimistic"     # real adverse cost exceeds the buffer — too thin
    if avg_adverse_cost < PHASE4_BUFFER - REALISTIC_BAND:
        return "conservative"   # buffer more than covers the adverse cost
    return "realistic"


def aggregate_offset(orders: list[dict]) -> dict:
    """Per-offset summary over one bucket of orders (same side + delta)."""
    total = len(orders)
    dense = [o for o in orders if o["dense"]]
    filled = [o for o in orders if o["filled"]]
    dense_filled = [o for o in dense if o["filled"]]
    markout_all = [o for o in orders if o["has_markout"]]
    filled_markout = [o for o in filled if o["has_markout"]]

    avg_adverse_cost = _mean([o["adverse_cost"] for o in filled_markout])
    unconditional_drift = _mean([o["drift"] for o in markout_all])
    filled_drift = _mean([o["drift"] for o in filled_markout])
    adverse_selection = (
        filled_drift - unconditional_drift
        if (filled_drift is not None and unconditional_drift is not None)
        else None
    )
    return {
        "n_orders": total,
        "n_dense": len(dense),
        "n_filled": len(filled),
        "n_filled_markout": len(filled_markout),
        "fill_rate": (len(filled) / total) if total else None,
        "dense_fill_rate": (len(dense_filled) / len(dense)) if dense else None,
        "avg_time_to_fill_min": _mean([o["time_to_fill_min"] for o in filled]),
        "avg_adverse_cost": avg_adverse_cost,
        "avg_net_oneleg": _mean([o["net_oneleg"] for o in filled_markout]),
        "avg_net_roundtrip": _mean([o["net_roundtrip"] for o in filled_markout]),
        "unconditional_drift": unconditional_drift,
        "filled_drift": filled_drift,
        "adverse_selection": adverse_selection,
        "buffer_verdict": buffer_verdict(avg_adverse_cost),
    }


# --------------------------------------------------------------------------- #
# DB layer + order generation
# --------------------------------------------------------------------------- #


def load_market_snapshots(conn: sqlite3.Connection, market_id: str) -> list[dict]:
    """Time-sorted snapshot dicts for one market (books may be one-sided/None)."""
    rows = conn.execute(
        """
        SELECT timestamp, best_bid, best_ask, midpoint, spread
          FROM snapshots WHERE market_id = ? ORDER BY timestamp
        """,
        (str(market_id),),
    ).fetchall()
    snaps: list[dict] = []
    for ts_str, bid, ask, mid, spr in rows:
        ts = parse_iso8601(ts_str)
        if ts is None:
            continue
        snaps.append({
            "ts": ts,
            "best_bid": float(bid) if bid is not None else None,
            "best_ask": float(ask) if ask is not None else None,
            "midpoint": float(mid) if mid is not None else None,
            "spread": float(spr) if spr is not None else None,
        })
    return snaps


def generate_orders_for_market(snaps: list[dict]) -> tuple[list[dict], dict]:
    """Post an order-set every 12h at two-sided, unpinned snapshots with forward data.

    Returns (orders, stats) where stats counts post-times kept and dropped so the
    funnel can make survivorship visible rather than silent.
    """
    ts_list = [s["ts"] for s in snaps]
    orders: list[dict] = []
    lo, hi = MID_RANGE
    spacing = timedelta(hours=POST_SPACING_HOURS)
    window = timedelta(hours=HOLDING_WINDOW_HOURS)
    last_post: datetime | None = None
    n_posttimes = 0
    dropped_pin = 0
    dropped_no_forward = 0

    for i, s in enumerate(snaps):
        mid = s["midpoint"]
        if mid is None or s["best_bid"] is None or s["best_ask"] is None:
            continue  # need a two-sided book to define mid_0
        if last_post is not None and (s["ts"] - last_post) < spacing:
            continue  # honor 12h spacing between post-times
        if not (lo <= mid <= hi):
            dropped_pin += 1
            continue  # pinned / near-resolved — dead or one-sided books
        # require at least one forward snapshot inside the window
        j = bisect.bisect_right(ts_list, s["ts"])
        if not (j < len(ts_list) and ts_list[j] <= s["ts"] + window):
            dropped_no_forward += 1
            continue

        last_post = s["ts"]
        n_posttimes += 1
        for delta in OFFSETS:
            for side_sign in (+1, -1):
                orders.append(simulate_order(snaps, i, side_sign, delta, ts_list=ts_list))

    stats = {
        "posttimes": n_posttimes,
        "dropped_pin": dropped_pin,
        "dropped_no_forward": dropped_no_forward,
    }
    return orders, stats


# --------------------------------------------------------------------------- #
# Study
# --------------------------------------------------------------------------- #


def _fmt(value: float | None, spec: str = "+.4f") -> str:
    return format(value, spec) if value is not None else "  n/a "


def run_simulation(
    db_path: Path | str = DB_PATH,
    json_path: Path | str = JSON_PATH,
    csv_path: Path | str = CSV_PATH,
    min_snapshots: int = MIN_SNAPSHOTS,
    market_limit: int | None = None,
) -> dict:
    market_ids = load_political_market_ids(db_path, min_snapshots=min_snapshots)
    if market_limit is not None:
        market_ids = market_ids[:market_limit]
    log.info("Cohort: %d political markets (>=%d snapshots)", len(market_ids), min_snapshots)
    if not market_ids:
        raise RuntimeError("no political markets in cohort — is the DB populated?")

    all_orders: list[dict] = []
    n_snapshots = n_posttimes = n_drop_pin = n_drop_no_forward = 0
    conn = sqlite3.connect(db_path)
    try:
        for k, mid in enumerate(market_ids, start=1):
            snaps = load_market_snapshots(conn, mid)
            n_snapshots += len(snaps)
            orders, mstats = generate_orders_for_market(snaps)
            all_orders.extend(orders)
            n_posttimes += mstats["posttimes"]
            n_drop_pin += mstats["dropped_pin"]
            n_drop_no_forward += mstats["dropped_no_forward"]
            if k % 50 == 0:
                log.info("  ... %d/%d markets scanned, %d orders", k, len(market_ids), len(all_orders))
    finally:
        conn.close()

    funnel = {
        "n_markets": len(market_ids),
        "n_snapshots_scanned": n_snapshots,
        "n_posttimes": n_posttimes,
        "n_dropped_pin_posttimes": n_drop_pin,
        "n_dropped_no_forward_posttimes": n_drop_no_forward,
        "n_orders": len(all_orders),
        "n_orders_dense": sum(1 for o in all_orders if o["dense"]),
        "n_orders_filled": sum(1 for o in all_orders if o["filled"]),
        "n_orders_with_markout": sum(1 for o in all_orders if o["has_markout"]),
    }
    log.info(
        "Funnel: markets=%d snapshots=%d posttimes=%d (dropped pin=%d, no_forward=%d) "
        "-> orders=%d (dense=%d filled=%d markout=%d)",
        funnel["n_markets"], funnel["n_snapshots_scanned"], funnel["n_posttimes"],
        funnel["n_dropped_pin_posttimes"], funnel["n_dropped_no_forward_posttimes"],
        funnel["n_orders"], funnel["n_orders_dense"], funnel["n_orders_filled"],
        funnel["n_orders_with_markout"],
    )
    if not all_orders:
        raise RuntimeError("no orders generated — cohort had no usable post-times")

    # ---- per-offset aggregation ------------------------------------------ #
    by_offset: list[dict] = []
    log.info("Per-offset results (fill rate | time-to-fill | adverse cost vs buffer %.3f):",
             PHASE4_BUFFER)
    for delta in OFFSETS:
        for side_sign in (+1, -1):
            key = offset_key(side_sign, delta)
            bucket = [o for o in all_orders if o["side_sign"] == side_sign and o["delta"] == delta]
            agg = aggregate_offset(bucket)
            row = {
                "offset": key,
                "side": "buy" if side_sign > 0 else "sell",
                "signed_offset": -delta if side_sign > 0 else delta,
                "delta": delta,
                **agg,
            }
            by_offset.append(row)
            log.info(
                "  %-11s n=%-6d fill=%s dense_fill=%s ttf=%smin  "
                "adverse_cost=%s net1=%s advsel=%s  [%s]",
                key, row["n_orders"],
                _fmt(row["fill_rate"], ".3f"), _fmt(row["dense_fill_rate"], ".3f"),
                _fmt(row["avg_time_to_fill_min"], ".0f"),
                _fmt(row["avg_adverse_cost"]), _fmt(row["avg_net_oneleg"]),
                _fmt(row["adverse_selection"]), row["buffer_verdict"],
            )

    # ---- headline verdict ------------------------------------------------- #
    costed = [r for r in by_offset if r["avg_adverse_cost"] is not None]
    worst = max(costed, key=lambda r: r["avg_adverse_cost"]) if costed else None
    any_favorable = any(
        r["avg_net_oneleg"] is not None and r["avg_net_oneleg"] > 0 for r in by_offset
    )
    overall = {
        "any_offset_net_favorable_oneleg": any_favorable,
        "buffer_assessment": worst["buffer_verdict"] if worst else "no_data",
        "worst_offset": worst["offset"] if worst else None,
        "worst_offset_adverse_cost": worst["avg_adverse_cost"] if worst else None,
        "note": (
            "buffer_assessment reflects the offset with the HIGHEST realized adverse "
            "cost (the binding case for a maker); net_oneleg is an entry-only mark — "
            "see avg_net_roundtrip for a figure net of an exit half-spread."
        ),
    }
    log.info(
        "Verdict: any net-favorable offset (one-leg)=%s; buffer 0.005 is %s "
        "(worst offset %s at adverse cost %s).",
        any_favorable, overall["buffer_assessment"], overall["worst_offset"],
        _fmt(overall["worst_offset_adverse_cost"]),
    )

    results = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "config": {
            "holding_window_hours": HOLDING_WINDOW_HOURS,
            "post_spacing_hours": POST_SPACING_HOURS,
            "offsets": list(OFFSETS),
            "mid_range": list(MID_RANGE),
            "markout_tolerance_min": MARKOUT_TOLERANCE_MIN,
            "max_intra_window_gap_min": MAX_INTRA_WINDOW_GAP_MIN,
            "phase4_buffer": PHASE4_BUFFER,
            "min_snapshots": min_snapshots,
            "cohort": "political keyword filter (model.train.POLITICAL_REGEX)",
        },
        "funnel": funnel,
        "overall": overall,
        "by_offset": by_offset,
    }

    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.info("Saved results -> %s", json_path)

    fieldnames = [
        "offset", "side", "signed_offset", "delta",
        "n_orders", "n_dense", "n_filled", "n_filled_markout",
        "fill_rate", "dense_fill_rate", "avg_time_to_fill_min",
        "avg_adverse_cost", "avg_net_oneleg", "avg_net_roundtrip",
        "unconditional_drift", "filled_drift", "adverse_selection", "buffer_verdict",
    ]
    with Path(csv_path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in by_offset:
            writer.writerow({k: row[k] for k in fieldnames})
    log.info("Saved per-offset CSV -> %s", csv_path)

    return results


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Maker-fill simulator (measurement only).")
    parser.add_argument("--limit", type=int, default=None, help="max markets (testing)")
    parser.add_argument("--min-snapshots", type=int, default=MIN_SNAPSHOTS)
    args = parser.parse_args(argv)
    try:
        run_simulation(min_snapshots=args.min_snapshots, market_limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        log.critical("Fill simulation FAILED: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
