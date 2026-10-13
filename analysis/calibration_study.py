"""Favorite-longshot calibration study on all resolved markets."""

from __future__ import annotations

import csv
import json
import logging
import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from model.train import POLITICAL_REGEX
from src.utils.dates import parse_iso8601

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "calibration_study.log"
RESULTS_DIR = REPO_ROOT / "analysis" / "results"
JSON_PATH = RESULTS_DIR / "calibration_study.json"
CSV_PATH = RESULTS_DIR / "calibration_bins.csv"

REFERENCE_HOURS_BEFORE = 24
STALENESS_CAP_HOURS = 24

BIN_EDGES: tuple[float, ...] = (
    0.00, 0.02, 0.05, 0.10, 0.20, 0.40, 0.60, 0.80, 0.90, 0.95, 0.98, 1.00,
)

# Time-to-scheduled-resolution buckets at the reference point.
TTE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<48h", float("-inf"), 48.0),
    ("48h-7d", 48.0, 168.0),
    (">7d", 168.0, float("inf")),
)

MAKER_FEE = 0.0  # maker orders are fee-free on Polymarket

log = logging.getLogger("analysis.calibration_study")


# Logging


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


# Dataset assembly


def extract_reference_rows(
    db_path: Path | str = DB_PATH,
    reference_hours: int = REFERENCE_HOURS_BEFORE,
    staleness_cap_hours: float = STALENESS_CAP_HOURS,
) -> tuple[list[dict], dict[str, int]]:
    """One row per resolved market with a usable 24h-before reference price."""
    conn = sqlite3.connect(db_path)
    try:
        cohort = conn.execute(
            """
            SELECT m.id, m.question, m.resolution, m.end_date,
                   MIN(s.timestamp), MAX(s.timestamp)
              FROM markets m JOIN snapshots s ON s.market_id = m.id
             WHERE m.status = 'resolved_or_removed'
               AND m.resolution IS NOT NULL
             GROUP BY m.id
            """
        ).fetchall()

        rows: list[dict] = []
        n_short_span = 0
        n_no_midpoint = 0
        n_stale_ref = 0
        n_bad_label = 0

        for mid, question, resolution, end_date, first_str, last_str in cohort:
            outcome_str = str(resolution).strip()
            if outcome_str not in ("0", "1"):
                n_bad_label += 1
                continue
            first = parse_iso8601(first_str)
            last = parse_iso8601(last_str)
            if first is None or last is None:
                n_short_span += 1
                continue
            t_ref = last - timedelta(hours=reference_hours)
            if first > t_ref:
                n_short_span += 1
                continue

            ref = conn.execute(
                """
                SELECT midpoint, spread, timestamp FROM snapshots
                 WHERE market_id = ? AND timestamp <= ? AND midpoint IS NOT NULL
                 ORDER BY timestamp DESC LIMIT 1
                """,
                (str(mid), t_ref.isoformat()),
            ).fetchone()
            if ref is None:
                n_no_midpoint += 1
                continue
            midpoint, spread, ref_ts_str = ref
            ref_ts = parse_iso8601(ref_ts_str)
            if ref_ts is None or (t_ref - ref_ts) > timedelta(hours=staleness_cap_hours):
                n_stale_ref += 1
                continue

            end_dt = parse_iso8601(end_date) if end_date else None
            tte_hours = (
                (end_dt - t_ref).total_seconds() / 3600.0 if end_dt else None
            )
            rows.append({
                "market_id": str(mid),
                "price": float(midpoint),
                "spread": float(spread) if spread is not None else None,
                "outcome": int(outcome_str),
                "political": bool(question and POLITICAL_REGEX.search(question)),
                "tte_hours": tte_hours,
            })
    finally:
        conn.close()

    funnel = {
        "n_cohort": len(cohort),
        "n_dropped_bad_label": n_bad_label,
        "n_dropped_short_span": n_short_span,
        "n_dropped_no_midpoint": n_no_midpoint,
        "n_dropped_stale_reference": n_stale_ref,
        "n_final": len(rows),
    }
    return rows, funnel


# Binning + calibration math


def assign_bin(price: float, edges: tuple[float, ...] = BIN_EDGES) -> int:
    """Bin index for a price. Bins are [lo, hi); the last bin includes 1.0."""
    for i in range(len(edges) - 1):
        if edges[i] <= price < edges[i + 1]:
            return i
    if price >= edges[-1]:
        return len(edges) - 2
    return 0


def bin_table(
    rows: list[dict],
    edges: tuple[float, ...] = BIN_EDGES,
) -> list[dict]:
    """Per-bin calibration stats: n, avg_price, actual_yes_rate, gap, se, spread."""
    buckets: list[list[dict]] = [[] for _ in range(len(edges) - 1)]
    for row in rows:
        buckets[assign_bin(row["price"], edges)].append(row)

    out: list[dict] = []
    for i, bucket in enumerate(buckets):
        lo, hi = edges[i], edges[i + 1]
        label = f"{lo:.2f}-{hi:.2f}"
        n = len(bucket)
        if n == 0:
            out.append({
                "bin": label, "lo": lo, "hi": hi, "n": 0,
                "avg_price": None, "actual_yes_rate": None,
                "gap": None, "se": None, "avg_spread": None,
            })
            continue
        avg_price = sum(r["price"] for r in bucket) / n
        yes_rate = sum(r["outcome"] for r in bucket) / n
        se = math.sqrt(yes_rate * (1 - yes_rate) / n) if n else None
        spreads = [r["spread"] for r in bucket if r.get("spread") is not None]
        avg_spread = sum(spreads) / len(spreads) if spreads else None
        out.append({
            "bin": label, "lo": lo, "hi": hi, "n": n,
            "avg_price": avg_price,
            "actual_yes_rate": yes_rate,
            "gap": yes_rate - avg_price,
            "se": se,
            "avg_spread": avg_spread,
        })
    return out


def tradeability_table(bins: list[dict], fee: float = MAKER_FEE) -> list[dict]:
    """Per bin: which side (if any) has an edge that survives spread + fee."""
    out: list[dict] = []
    for b in bins:
        if not b["n"] or b["avg_price"] is None:
            continue
        edge_yes = b["actual_yes_rate"] - b["avg_price"]
        edge_no = -edge_yes
        direction = "buy_yes" if edge_yes >= edge_no else "buy_no"
        gross = max(edge_yes, edge_no)
        cost = (b["avg_spread"] or 0.0) + fee
        net = gross - cost
        out.append({
            "bin": b["bin"],
            "n": b["n"],
            "direction": direction,
            "gross_edge": gross,
            "avg_spread": b["avg_spread"],
            "net_edge": net,
            "tradeable": net > 0,
            "ceiling_per_contract_units": b["n"] * net if net > 0 else 0.0,
        })
    return out


def tte_bucket_label(tte_hours: float | None) -> str:
    if tte_hours is None:
        return "unknown"
    for label, lo, hi in TTE_BUCKETS:
        if lo <= tte_hours < hi:
            return label
    return "unknown"


# Main study


def run_study(
    db_path: Path | str = DB_PATH,
    json_path: Path | str = JSON_PATH,
    csv_path: Path | str = CSV_PATH,
) -> dict:
    rows, funnel = extract_reference_rows(db_path)
    log.info(
        "Funnel: cohort=%d -> bad_label=-%d short_span=-%d no_midpoint=-%d "
        "stale_ref=-%d -> final=%d",
        funnel["n_cohort"], funnel["n_dropped_bad_label"],
        funnel["n_dropped_short_span"], funnel["n_dropped_no_midpoint"],
        funnel["n_dropped_stale_reference"], funnel["n_final"],
    )
    if not rows:
        raise RuntimeError("no usable reference rows — is the DB populated?")

    n = len(rows)
    yes_rate_final = sum(r["outcome"] for r in rows) / n
    n_political = sum(1 for r in rows if r["political"])
    log.info(
        "Base rates: final cohort n=%d, YES rate=%.3f, political=%d non-political=%d",
        n, yes_rate_final, n_political, n - n_political,
    )

    overall = bin_table(rows)
    log.info("Overall calibration (avg_price vs actual_yes_rate):")
    for b in overall:
        if b["n"]:
            log.info(
                "  %s  n=%-4d avg_price=%.4f actual=%.4f gap=%+.4f (se=%.4f) spread=%.4f",
                b["bin"], b["n"], b["avg_price"], b["actual_yes_rate"],
                b["gap"], b["se"], b["avg_spread"] or 0.0,
            )

    pol_rows = [r for r in rows if r["political"]]
    nonpol_rows = [r for r in rows if not r["political"]]
    by_category = {
        "political": bin_table(pol_rows),
        "non_political": bin_table(nonpol_rows),
    }
    log.info("Category breakout: political n=%d, non-political n=%d "
             "(note: no tags column in markets — keyword filter only)",
             len(pol_rows), len(nonpol_rows))

    by_tte: dict[str, list[dict]] = {}
    for label, _, _ in TTE_BUCKETS:
        subset = [r for r in rows if tte_bucket_label(r["tte_hours"]) == label]
        by_tte[label] = bin_table(subset)
        log.info("TTE bucket %s: n=%d", label, len(subset))
    unknown_rows = [r for r in rows if tte_bucket_label(r["tte_hours"]) == "unknown"]
    by_tte["unknown"] = bin_table(unknown_rows)
    log.info("TTE bucket unknown: n=%d", len(unknown_rows))

    trade = tradeability_table(overall)
    tradeable_bins = [t for t in trade if t["tradeable"]]
    log.info("Tradeability (gross edge minus reference spread, maker fee=0):")
    for t in trade:
        log.info(
            "  %s  n=%-4d %s gross=%+.4f spread=%.4f net=%+.4f  %s",
            t["bin"], t["n"], t["direction"], t["gross_edge"],
            t["avg_spread"] or 0.0, t["net_edge"],
            "TRADEABLE" if t["tradeable"] else "-",
        )
    n_tradeable_markets = sum(t["n"] for t in tradeable_bins)
    ceiling = sum(t["ceiling_per_contract_units"] for t in tradeable_bins)
    log.info(
        "Tradeable bins: %d (%d markets); rough ceiling = %.2f per-contract units",
        len(tradeable_bins), n_tradeable_markets, ceiling,
    )

    results = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "config": {
            "reference_hours_before": REFERENCE_HOURS_BEFORE,
            "staleness_cap_hours": STALENESS_CAP_HOURS,
            "bin_edges": list(BIN_EDGES),
            "maker_fee": MAKER_FEE,
            "category_note": "no tags column in markets table; category = political keyword filter only",
        },
        "funnel": funnel,
        "base_rates": {
            "final_cohort_n": n,
            "final_cohort_yes_rate": yes_rate_final,
            "n_political": n_political,
            "n_non_political": n - n_political,
        },
        "overall_bins": overall,
        "by_category": by_category,
        "by_time_to_resolution": by_tte,
        "tradeability": {
            "bins": trade,
            "n_tradeable_bins": len(tradeable_bins),
            "n_markets_in_tradeable_bins": n_tradeable_markets,
            "rough_ceiling_per_contract_units": ceiling,
        },
    }

    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.info("Saved results -> %s", json_path)

    with Path(csv_path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "bin", "lo", "hi", "n", "avg_price", "actual_yes_rate",
            "gap", "se", "avg_spread",
        ])
        writer.writeheader()
        for b in overall:
            writer.writerow(b)
    log.info("Saved per-bin CSV -> %s", csv_path)

    return results


def main() -> int:
    setup_logging()
    try:
        run_study()
    except Exception as exc:  # noqa: BLE001
        log.critical("Calibration study FAILED: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
