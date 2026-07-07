"""Pre-registered evaluation harness for the prospective LLM scoring experiment.

Every metric, threshold, and subgroup below was frozen in code BEFORE any
prospectively-scored market resolved (2026-07-08: 144 markets scored, 0
resolved). Nothing computed later counts as post-hoc.

Modes:
    python -m analysis.prospective_evaluation             # STATUS (blinded)
    python -m analysis.prospective_evaluation --verdict   # pre-registered verdict
    python -m analysis.prospective_evaluation --smoke     # pilot-data code check

STATUS mode is deliberately blinded: it reports ONLY how many eligible
markets have accumulated (total and per ISO week) and never computes any
directional statistic. This prevents optional stopping — you cannot peek at
the answer while deciding whether to keep collecting.

VERDICT mode refuses to run below the pre-registered sample-size gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.utils.dates import parse_iso8601

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "prospective_evaluation.log"
BASE_RATES_PATH = REPO_ROOT / "config" / "base_rates.json"

VERDICT_JSON_PATH = REPO_ROOT / "analysis" / "results" / "prospective_verdict.json"
VERDICT_CSV_PATH = REPO_ROOT / "analysis" / "results" / "prospective_per_market.csv"
SMOKE_JSON_PATH = REPO_ROOT / "analysis" / "results" / "prospective_smoke.json"
SMOKE_CSV_PATH = REPO_ROOT / "analysis" / "results" / "prospective_smoke.csv"

# ============================================================================
# PRE-REGISTERED CONSTANTS — frozen 2026-07-08, before any prospective market
# resolved. DO NOT CHANGE after prospective data exists. Changing any of these
# invalidates the pre-registration.
# ============================================================================
MIN_RESOLVED_FOR_VERDICT = 40        # run gate: eligible resolved markets
DISAGREEMENT_THRESHOLD = 0.10        # |p_llm - price| to count a disagreement
WIN_RATE_GATE = 0.55                 # primary point estimate >= this -> open
SPIKE_RATIO_GATE = 2.0               # secondary (b) news-spike filter
SPIKE_WINDOW_HOURS = 24              # news_volume row within this of scored_at
BASE_RATE_DIVERGENCE_THRESHOLD = 0.15  # secondary (d) |price - base_rate|
WILSON_Z = 1.96                      # 95% interval
FLAGGED_CATEGORIES = ("legislation", "judicial", "leadership_stability")
# Tie-breaking: exactly equal distance to the outcome counts as a MARKET win
# (conservative against the LLM-edge hypothesis).
# Zero disagreements at verdict time -> politics_closed (no exploitable
# divergence is itself a negative result).
# ============================================================================

log = logging.getLogger("analysis.prospective_evaluation")


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
# Statistics
# --------------------------------------------------------------------------- #


def wilson_interval(k: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. (0,1) when n=0."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def edge_stats(rows: list[dict]) -> dict:
    """Disagreement edge check over eligible rows. Reused by primary + secondaries."""
    disagreements = [r for r in rows if r["disagreement"]]
    llm_wins = sum(1 for r in disagreements if r["winner"] == "llm")
    n_d = len(disagreements)
    win_rate = (llm_wins / n_d) if n_d else None
    lo, hi = wilson_interval(llm_wins, n_d)
    return {
        "n_markets": len(rows),
        "n_disagreements": n_d,
        "llm_wins": llm_wins,
        "llm_win_rate": win_rate,
        "wilson_95_lo": lo if n_d else None,
        "wilson_95_hi": hi if n_d else None,
    }


# --------------------------------------------------------------------------- #
# Eligible-row loader (shared by status counting and the verdict run-gate)
# --------------------------------------------------------------------------- #


def load_eligible_rows(
    db_path: Path | str = DB_PATH,
    retrospective: int = 0,
    skip_final_snapshot_guard: bool = False,
) -> list[dict]:
    """One row per eligible resolved market with its earliest surviving score.

    Contamination guards (pre-registered):
      (a) recognized_outcome=1 scores dropped BEFORE the earliest-score pick;
      (b) market excluded when its earliest surviving scored_at is later than
          its final snapshot (skippable only for --smoke, whose pilot rows
          were all scored post-resolution by construction).
    """
    conn = sqlite3.connect(db_path)
    try:
        score_rows = conn.execute(
            """
            SELECT s.market_id, MIN(s.scored_at), s.probability, s.confidence,
                   s.category, m.question, m.resolution, m.last_updated
              FROM llm_scores s
              JOIN markets m ON m.id = s.market_id
             WHERE s.retrospective = ?
               AND s.recognized_outcome = 0            -- guard (a)
               AND m.resolution IN ('0', '1')
             GROUP BY s.market_id
            """,
            (retrospective,),
        ).fetchall()

        rows: list[dict] = []
        for mid, scored_at, prob, confidence, category, question, res, lu in score_rows:
            scored_dt = parse_iso8601(scored_at)
            if scored_dt is None:
                continue

            last_snap = conn.execute(
                "SELECT MAX(timestamp) FROM snapshots WHERE market_id = ?", (mid,),
            ).fetchone()[0]
            if not skip_final_snapshot_guard:            # guard (b)
                last_dt = parse_iso8601(last_snap) if last_snap else None
                if last_dt is None or scored_dt > last_dt:
                    continue

            price_row = _nearest_snapshot_midpoint(conn, str(mid), scored_dt)
            if price_row is None:
                continue
            price, gap_minutes = price_row

            y = int(res)
            p = float(prob)
            disagreement = abs(p - price) > DISAGREEMENT_THRESHOLD
            if disagreement:
                # Ties (equal distance) go to the market — pre-registered.
                winner = "llm" if abs(p - y) < abs(price - y) else "market"
            else:
                winner = ""

            resolved_dt = parse_iso8601(lu)
            rows.append({
                "market_id": str(mid),
                "question": (question or "")[:100],
                "category": category,
                "confidence": confidence,
                "scored_at": scored_at,
                "llm_probability": p,
                "market_price_at_scoring": price,
                "price_snapshot_gap_minutes": gap_minutes,
                "resolution": y,
                "disagreement": disagreement,
                "winner": winner,
                "resolved_week": (
                    resolved_dt.strftime("%G-W%V") if resolved_dt else "unknown"
                ),
                "_scored_dt": scored_dt,
            })
    finally:
        conn.close()
    return rows


def _nearest_snapshot_midpoint(
    conn: sqlite3.Connection,
    market_id: str,
    at: datetime,
) -> tuple[float, float] | None:
    """(midpoint, |gap| minutes) of the snapshot nearest ``at`` in either direction."""
    at_iso = at.isoformat()
    before = conn.execute(
        "SELECT timestamp, midpoint FROM snapshots WHERE market_id = ? "
        "AND timestamp <= ? AND midpoint IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
        (market_id, at_iso),
    ).fetchone()
    after = conn.execute(
        "SELECT timestamp, midpoint FROM snapshots WHERE market_id = ? "
        "AND timestamp > ? AND midpoint IS NOT NULL ORDER BY timestamp ASC LIMIT 1",
        (market_id, at_iso),
    ).fetchone()

    best: tuple[float, float] | None = None
    for row in (before, after):
        if row is None:
            continue
        ts = parse_iso8601(row[0])
        if ts is None:
            continue
        gap = abs((ts - at).total_seconds()) / 60.0
        if best is None or gap < best[1]:
            best = (float(row[1]), gap)
    return best


# --------------------------------------------------------------------------- #
# STATUS mode (blinded — accumulation counts only)
# --------------------------------------------------------------------------- #


def run_status(db_path: Path | str = DB_PATH) -> dict:
    """Accumulation report only. Computes NO directional statistic."""
    rows = load_eligible_rows(db_path)
    by_week: dict[str, int] = {}
    for r in rows:
        by_week[r["resolved_week"]] = by_week.get(r["resolved_week"], 0) + 1

    log.info("Prospective evaluation — accumulation status (blinded)")
    log.info("Eligible resolved markets: %d of %d needed for the verdict run",
             len(rows), MIN_RESOLVED_FOR_VERDICT)
    if by_week:
        for week in sorted(by_week):
            log.info("  %s: %d", week, by_week[week])
    else:
        log.info("  (none resolved yet)")
    return {"eligible": len(rows), "by_week": by_week}


# --------------------------------------------------------------------------- #
# VERDICT mode
# --------------------------------------------------------------------------- #


def _spike_gated(rows: list[dict], db_path: Path | str) -> list[dict]:
    """Rows whose nearest news_volume within ±SPIKE_WINDOW_HOURS has ratio > gate."""
    conn = sqlite3.connect(db_path)
    kept: list[dict] = []
    try:
        for r in rows:
            window = timedelta(hours=SPIKE_WINDOW_HOURS)
            lo = (r["_scored_dt"] - window).isoformat()
            hi = (r["_scored_dt"] + window).isoformat()
            nv = conn.execute(
                "SELECT spike_ratio FROM news_volume WHERE market_id = ? "
                "AND collected_at BETWEEN ? AND ? AND spike_ratio IS NOT NULL",
                (r["market_id"], lo, hi),
            ).fetchall()
            if any(float(v[0]) > SPIKE_RATIO_GATE for v in nv):
                kept.append(r)
    finally:
        conn.close()
    return kept


def _base_rate_check(rows: list[dict]) -> dict:
    """Secondary (d): LLM-independent. Where price diverged from the category
    base rate by > threshold at scoring time, was the base rate closer to the
    resolution than the price?"""
    with open(BASE_RATES_PATH, "r", encoding="utf-8") as f:
        base_rates: dict = json.load(f)

    qualifying = []
    for r in rows:
        br = base_rates.get(r["category"])
        if br is None:
            continue
        if abs(r["market_price_at_scoring"] - br) > BASE_RATE_DIVERGENCE_THRESHOLD:
            qualifying.append((r, br))

    n = len(qualifying)
    base_wins = sum(
        1 for r, br in qualifying
        if abs(br - r["resolution"]) < abs(r["market_price_at_scoring"] - r["resolution"])
    )
    lo, hi = wilson_interval(base_wins, n)
    return {
        "label": "secondary",
        "name": "base_rate_divergence_check",
        "n_markets": n,
        "base_rate_wins": base_wins,
        "base_rate_win_rate": (base_wins / n) if n else None,
        "wilson_95_lo": lo if n else None,
        "wilson_95_hi": hi if n else None,
        "actual_resolution_rate": (
            sum(r["resolution"] for r, _ in qualifying) / n if n else None
        ),
        "mean_base_rate": (sum(br for _, br in qualifying) / n if n else None),
        "mean_price": (
            sum(r["market_price_at_scoring"] for r, _ in qualifying) / n if n else None
        ),
    }


def run_verdict(
    db_path: Path | str = DB_PATH,
    smoke: bool = False,
) -> int:
    if smoke:
        banner = ("CONTAMINATED PILOT — code-path verification only. Scores were "
                  "made AFTER resolution and prices join to the pinned final "
                  "snapshot; every number below is garbage by construction.")
        log.warning(banner)
        rows = load_eligible_rows(db_path, retrospective=1,
                                  skip_final_snapshot_guard=True)
        json_path, csv_path = SMOKE_JSON_PATH, SMOKE_CSV_PATH
    else:
        rows = load_eligible_rows(db_path)
        json_path, csv_path = VERDICT_JSON_PATH, VERDICT_CSV_PATH
        if len(rows) < MIN_RESOLVED_FOR_VERDICT:
            log.error(
                "REFUSING to run the verdict: only %d eligible resolved markets; "
                "the pre-registered gate requires %d. Run status mode to track "
                "accumulation.", len(rows), MIN_RESOLVED_FOR_VERDICT,
            )
            return 1

    # --- Primary (pre-registered) ----------------------------------------- #
    primary = edge_stats(rows)
    if primary["n_disagreements"] == 0:
        verdict = "politics_closed"
    else:
        verdict = (
            "politics_open"
            if primary["llm_win_rate"] >= WIN_RATE_GATE else "politics_closed"
        )

    log.info("PRIMARY: n=%d, disagreements=%d, LLM wins=%d, win rate=%s, "
             "Wilson95=[%s, %s]",
             primary["n_markets"], primary["n_disagreements"], primary["llm_wins"],
             f"{primary['llm_win_rate']:.3f}" if primary["llm_win_rate"] is not None else "n/a",
             f"{primary['wilson_95_lo']:.3f}" if primary["wilson_95_lo"] is not None else "n/a",
             f"{primary['wilson_95_hi']:.3f}" if primary["wilson_95_hi"] is not None else "n/a")
    log.info("%s: %s (gate: win rate >= %.2f at n >= %d)",
             "SMOKE VERDICT (not real)" if smoke else "PRE-REGISTERED VERDICT",
             verdict, WIN_RATE_GATE, MIN_RESOLVED_FOR_VERDICT)

    # --- Secondaries (labeled; never touch the verdict) -------------------- #
    sec_a = {"label": "secondary", "name": "confidence_gated_high",
             **edge_stats([r for r in rows if r["confidence"] == "high"])}
    sec_b = {"label": "secondary", "name": "spike_gated",
             **edge_stats(_spike_gated(rows, db_path))}
    sec_c: list[dict] = []
    for cat in sorted({r["category"] for r in rows if r["category"]}):
        stats = edge_stats([r for r in rows if r["category"] == cat])
        sec_c.append({
            "label": "secondary", "name": f"category:{cat}",
            "flagged_prior_binding": cat in FLAGGED_CATEGORIES, **stats,
        })
    sec_d = _base_rate_check(rows)

    for s in [sec_a, sec_b, *sec_c, sec_d]:
        detail = {k: v for k, v in s.items() if k not in ("label",)}
        log.info("SECONDARY %s", json.dumps(detail, default=str))

    # --- Outputs ------------------------------------------------------------ #
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "market_id", "question", "category", "scored_at", "llm_probability",
            "market_price_at_scoring", "resolution", "disagreement", "winner",
            "price_snapshot_gap_minutes",
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in writer.fieldnames})

    payload: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "smoke": smoke,
        "preregistered_config": {
            "min_resolved_for_verdict": MIN_RESOLVED_FOR_VERDICT,
            "disagreement_threshold": DISAGREEMENT_THRESHOLD,
            "win_rate_gate": WIN_RATE_GATE,
            "spike_ratio_gate": SPIKE_RATIO_GATE,
            "spike_window_hours": SPIKE_WINDOW_HOURS,
            "base_rate_divergence_threshold": BASE_RATE_DIVERGENCE_THRESHOLD,
            "wilson_z": WILSON_Z,
            "flagged_categories": list(FLAGGED_CATEGORIES),
            "tie_break": "market",
            "zero_disagreements_verdict": "politics_closed",
        },
        "primary": primary,
        ("smoke_verdict" if smoke else "verdict"): verdict,
        "secondaries": [sec_a, sec_b, *sec_c, sec_d],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log.info("Saved %s + %s", json_path.name, csv_path.name)

    if smoke:
        log.warning(banner)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Pre-registered prospective LLM-scoring evaluation.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--verdict", action="store_true",
                       help="Run the pre-registered verdict (refuses below the gate).")
    group.add_argument("--smoke", action="store_true",
                       help="Verify the code path on contaminated pilot data.")
    args = parser.parse_args(argv)

    if args.verdict:
        return run_verdict(DB_PATH, smoke=False)
    if args.smoke:
        return run_verdict(DB_PATH, smoke=True)
    run_status(DB_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
