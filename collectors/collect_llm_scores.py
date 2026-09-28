"""LLM market scoring — independent probability estimates for political markets.

For every fresh, active political market this collector asks Claude
(``claude-sonnet-4-6``) for an independent probability that the market
resolves YES, a category classification, and 2-4 news-search keywords.

CRITICAL DESIGN CONSTRAINT: the prompt receives ONLY the market's question
text, description/resolution criteria, and end date. It never sees the
market price, odds, volume, or any other Polymarket trading data — the whole
point is to measure whether a price-blinded estimate carries signal the
price lacks. The prompt-builder function only accepts those three fields,
so the constraint holds by construction.

Runs:
    python -m collectors.collect_llm_scores [--limit N] [--force]
    python -m collectors.collect_llm_scores --retrospective   # one-time pilot

The retrospective pilot scores already-resolved markets with the identical
prompt and immediately reports an edge check vs the 48h-before market price.
Its output is CONTAMINATED (the model may know these outcomes from training)
and is labeled as directional-only everywhere it appears.

Env: ANTHROPIC_API_KEY (loaded from .env via python-dotenv). Never logged.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "llm_scores.log"
BASE_RATES_PATH = REPO_ROOT / "config" / "base_rates.json"
PILOT_RESULTS_PATH = REPO_ROOT / "analysis" / "results" / "llm_pilot.json"

MODEL_NAME = "claude-sonnet-4-6"
PROMPT_VERSION = "v1"
MAX_OUTPUT_TOKENS = 1024
RESCORE_AFTER_DAYS = 7
SLEEP_BETWEEN_CALLS_S = 0.2
# Sonnet 4.6 pricing per 1M tokens (for the cost log line).
PRICE_IN_PER_MTOK = 3.00
PRICE_OUT_PER_MTOK = 15.00
PRICE_CACHE_READ_PER_MTOK = 0.30

CATEGORIES: tuple[str, ...] = (
    "legislation", "executive_action", "nomination_confirmation",
    "diplomatic_summit", "election_incumbent", "election_open",
    "judicial", "government_operations", "leadership_stability", "other",
)
CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")

GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/{}"

log = logging.getLogger("collect_llm_scores")


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
# Schema
# --------------------------------------------------------------------------- #


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_scores (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id          TEXT NOT NULL,
            scored_at          TEXT NOT NULL,
            model_name         TEXT NOT NULL,
            probability        REAL,
            confidence         TEXT,
            reasoning          TEXT,
            recognized_outcome INTEGER,
            category           TEXT,
            search_keywords    TEXT,
            prompt_version     TEXT NOT NULL,
            retrospective      INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_scores_market ON llm_scores (market_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_scores_scored_at ON llm_scores (scored_at)"
    )
    # Lazily-populated market description cache (Gamma resolution criteria).
    existing = {row[1] for row in conn.execute("PRAGMA table_info(markets)")}
    if "description" not in existing:
        conn.execute("ALTER TABLE markets ADD COLUMN description TEXT")
    conn.commit()


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

# Stable system prompt — keep byte-identical across calls so prompt caching
# can engage (volatile market text goes in the user turn).
SYSTEM_PROMPT = """You are a careful superforecaster estimating probabilities \
for prediction-market questions about politics and geopolitics.

You are NOT given the market's trading price, odds, volume, or any other \
market data — only the question text itself. Do not attempt to guess or \
anchor on what the market price might be. Reason independently from:
- historical base rates for this type of event
- publicly known context as of your training data
- structural factors (deadlines, institutional procedure, incentives)

Definitions for the category field (choose exactly one):
- legislation: a bill/law passing a legislature by a date
- executive_action: an executive order, announcement, or unilateral government action
- nomination_confirmation: a nominee being confirmed/appointed to a post
- diplomatic_summit: a meeting, summit, or negotiation occurring
- election_incumbent: an election where an incumbent seeks re-election
- election_open: an election with no incumbent running
- judicial: a court ruling or judicial outcome
- government_operations: shutdowns, budgets, debt ceilings, procedural operations
- leadership_stability: a leader remaining in / leaving office outside elections
- other: anything that fits none of the above

Also produce 2-4 short news-search keywords (proper nouns and specific terms
from the question, not generic words like "politics" or "will").

Respond with a single JSON object:
{
  "probability": <float 0-1, probability the market resolves YES>,
  "confidence": "low" | "medium" | "high",
  "reasoning": "<two sentences maximum>",
  "recognized_outcome": <true if you believe you already know this market's \
actual resolution from your training data, else false>,
  "category": "<one of the ten categories>",
  "search_keywords": ["<2-4 keywords>"]
}"""

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "probability": {"type": "number"},
        "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
        "reasoning": {"type": "string"},
        "recognized_outcome": {"type": "boolean"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "search_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "probability", "confidence", "reasoning",
        "recognized_outcome", "category", "search_keywords",
    ],
    "additionalProperties": False,
}


def build_user_prompt(question: str, description: str | None, end_date: str | None) -> str:
    """The ONLY market fields the model ever sees. No price/volume/odds."""
    parts = [f"Market question: {question}"]
    if description:
        parts.append(f"Description / resolution criteria:\n{description[:4000]}")
    if end_date:
        parts.append(f"Scheduled resolution date: {end_date[:10]}")
    parts.append("Estimate the probability this market resolves YES.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Response validation
# --------------------------------------------------------------------------- #


def _validate_score_payload(raw: Any) -> dict | None:
    """Normalize + validate a parsed LLM payload. Returns None on garbage."""
    if isinstance(raw, str):
        # Tolerate code fences / stray text around the JSON object.
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.startswith("json"):
                s = s[4:]
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            raw = json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None

    try:
        probability = float(raw["probability"])
    except (KeyError, TypeError, ValueError):
        return None
    probability = min(1.0, max(0.0, probability))

    confidence = str(raw.get("confidence", "")).lower().strip()
    if confidence not in CONFIDENCE_LEVELS:
        return None

    category = str(raw.get("category", "")).lower().strip()
    if category not in CATEGORIES:
        return None

    reasoning = str(raw.get("reasoning", ""))[:600]
    recognized = bool(raw.get("recognized_outcome", False))

    kw_raw = raw.get("search_keywords")
    if not isinstance(kw_raw, list):
        return None
    keywords = [str(k).strip() for k in kw_raw if str(k).strip()][:4]
    if len(keywords) < 1:
        return None

    return {
        "probability": probability,
        "confidence": confidence,
        "reasoning": reasoning,
        "recognized_outcome": int(recognized),
        "category": category,
        "search_keywords": keywords,
    }


# --------------------------------------------------------------------------- #
# Market context (description lazy-fetch)
# --------------------------------------------------------------------------- #


def _fetch_description(session: requests.Session, market_id: str) -> str | None:
    """Fetch the Gamma description (resolution criteria) for one market."""
    try:
        resp = session.get(GAMMA_MARKET_URL.format(market_id), timeout=20)
        if resp.status_code != 200:
            return None
        data = resp.json()
        desc = data.get("description")
        return str(desc) if desc else None
    except (requests.RequestException, ValueError):
        return None


def get_market_context(
    conn: sqlite3.Connection,
    session: requests.Session,
    market_id: str,
) -> tuple[str, str | None, str | None] | None:
    """(question, description, end_date) — lazily caching description in the DB."""
    row = conn.execute(
        "SELECT question, description, end_date FROM markets WHERE id = ?",
        (market_id,),
    ).fetchone()
    if row is None or not row[0]:
        return None
    question, description, end_date = row
    if not description:
        description = _fetch_description(session, market_id)
        if description:
            conn.execute(
                "UPDATE markets SET description = ? WHERE id = ?",
                (description, market_id),
            )
            conn.commit()
    return question, description, end_date


# --------------------------------------------------------------------------- #
# Candidate selection
# --------------------------------------------------------------------------- #


def load_prospective_candidates(
    conn: sqlite3.Connection,
    db_path: Path | str,
    force: bool = False,
) -> list[str]:
    """Active political markets, fresh snapshots, not scored in the last 7 days."""
    from decision.signal_generator import _filter_fresh_markets
    from decision.risk_manager import load_risk_config
    from model.train import load_political_market_ids

    ids = load_political_market_ids(db_path)
    active = {
        str(r[0]) for r in conn.execute(
            f"SELECT id FROM markets WHERE status='active' AND id IN "
            f"({','.join('?' * len(ids))})", ids,
        ).fetchall()
    } if ids else set()
    ids = [m for m in ids if m in active]

    max_age = float(load_risk_config()["max_snapshot_age_minutes"])
    fresh, n_excluded, global_stale = _filter_fresh_markets(
        db_path, ids, max_age, datetime.now(timezone.utc),
    )
    if global_stale:
        log.critical("Newest snapshot in DB is stale — scores would be built on old data.")
    log.info("Candidates: %d political-active, %d fresh (excluded %d stale)",
             len(ids), len(fresh), n_excluded)

    if force:
        return fresh

    cutoff = (datetime.now(timezone.utc) - timedelta(days=RESCORE_AFTER_DAYS)).isoformat()
    recently_scored = {
        str(r[0]) for r in conn.execute(
            "SELECT DISTINCT market_id FROM llm_scores "
            "WHERE retrospective = 0 AND scored_at > ?", (cutoff,),
        ).fetchall()
    }
    remaining = [m for m in fresh if m not in recently_scored]
    log.info("Resumability: %d already scored within %dd, %d to score",
             len(fresh) - len(remaining), RESCORE_AFTER_DAYS, len(remaining))
    return remaining


def load_retrospective_candidates(db_path: Path | str) -> list[str]:
    """The resolved political cohort (same query as the outcome experiment)."""
    from model.train_outcome import load_candidate_market_ids
    return load_candidate_market_ids(db_path)


# --------------------------------------------------------------------------- #
# Scoring loop
# --------------------------------------------------------------------------- #


class UsageTotals:
    def __init__(self) -> None:
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0

    def add(self, usage: Any) -> None:
        self.input += getattr(usage, "input_tokens", 0) or 0
        self.output += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0

    @property
    def cost_usd(self) -> float:
        return (
            self.input * PRICE_IN_PER_MTOK
            + self.output * PRICE_OUT_PER_MTOK
            + self.cache_read * PRICE_CACHE_READ_PER_MTOK
            + self.cache_write * PRICE_IN_PER_MTOK * 1.25
        ) / 1_000_000


def score_market(
    client: Any,
    question: str,
    description: str | None,
    end_date: str | None,
    totals: UsageTotals,
    use_structured: bool,
) -> tuple[dict | None, bool]:
    """One API call. Returns (validated_payload | None, structured_ok).

    ``structured_ok`` turns False when the API rejects output_config so the
    caller can fall back to prompt-JSON for the remainder of the run.
    """
    import anthropic

    kwargs: dict = dict(
        model=MODEL_NAME,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": build_user_prompt(question, description, end_date),
        }],
    )
    if use_structured:
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        }

    try:
        response = client.messages.create(**kwargs)
    except anthropic.BadRequestError as exc:
        if use_structured and "output_config" in str(exc).lower():
            log.warning(
                "API rejected output_config on %s — falling back to prompt-JSON "
                "for the rest of the run (%s)", MODEL_NAME, exc,
            )
            return score_market(client, question, description, end_date,
                                totals, use_structured=False)[0], False
        raise

    totals.add(response.usage)
    text = next((b.text for b in response.content if b.type == "text"), "")
    return _validate_score_payload(text), use_structured


def run_scoring(
    market_ids: list[str],
    db_path: Path | str = DB_PATH,
    retrospective: bool = False,
) -> dict:
    import anthropic

    client = anthropic.Anthropic(max_retries=4)
    gamma_session = requests.Session()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=15000")
    totals = UsageTotals()
    use_structured = True
    n_scored = n_failed = 0

    try:
        init_db(conn)
        now_iso = datetime.now(timezone.utc).isoformat()
        for i, mid in enumerate(market_ids, start=1):
            ctx = get_market_context(conn, gamma_session, mid)
            if ctx is None:
                log.warning("Market %s: no question text — skipped", mid)
                n_failed += 1
                continue
            question, description, end_date = ctx
            try:
                payload, use_structured = score_market(
                    client, question, description, end_date, totals, use_structured,
                )
            except anthropic.APIConnectionError as exc:
                log.error("Market %s: connection error after retries: %s", mid, exc)
                n_failed += 1
                continue
            except anthropic.APIStatusError as exc:
                log.error("Market %s: API error %s: %s", mid, exc.status_code, exc.message)
                n_failed += 1
                continue

            if payload is None:
                log.warning("Market %s: unparseable/invalid LLM payload — skipped", mid)
                n_failed += 1
                continue

            conn.execute(
                """
                INSERT INTO llm_scores
                  (market_id, scored_at, model_name, probability, confidence,
                   reasoning, recognized_outcome, category, search_keywords,
                   prompt_version, retrospective)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mid, now_iso, MODEL_NAME, payload["probability"],
                 payload["confidence"], payload["reasoning"],
                 payload["recognized_outcome"], payload["category"],
                 json.dumps(payload["search_keywords"]),
                 PROMPT_VERSION, int(retrospective)),
            )
            conn.commit()
            n_scored += 1
            if i % 25 == 0:
                log.info("Progress: %d/%d scored (failed=%d, est cost $%.2f)",
                         i, len(market_ids), n_failed, totals.cost_usd)
            time.sleep(SLEEP_BETWEEN_CALLS_S)
    finally:
        conn.close()

    log.info(
        "Scoring done: scored=%d failed=%d | tokens in=%d out=%d cache_read=%d "
        "cache_write=%d | est cost $%.2f",
        n_scored, n_failed, totals.input, totals.output,
        totals.cache_read, totals.cache_write, totals.cost_usd,
    )
    return {
        "scored": n_scored, "failed": n_failed,
        "tokens_in": totals.input, "tokens_out": totals.output,
        "cache_read": totals.cache_read, "cache_write": totals.cache_write,
        "est_cost_usd": round(totals.cost_usd, 4),
    }


# --------------------------------------------------------------------------- #
# Base-rate divergence helper (Component 2)
# --------------------------------------------------------------------------- #


def get_base_rate_divergence(
    market_ids: Iterable[str],
    db_path: Path | str = DB_PATH,
    base_rates_path: Path | str = BASE_RATES_PATH,
):
    """DataFrame: market_id, category, base_rate, current_midpoint, divergence.

    divergence = current_midpoint - base_rate; null when the category has no
    base rate, the market has no LLM score, or no snapshot midpoint exists.
    """
    import pandas as pd

    ids = [str(m) for m in market_ids]
    cols = ["market_id", "category", "base_rate", "current_midpoint", "divergence"]
    if not ids:
        return pd.DataFrame(columns=cols)

    with open(base_rates_path, "r", encoding="utf-8") as f:
        base_rates: dict = json.load(f)

    conn = sqlite3.connect(db_path)
    try:
        ph = ",".join("?" * len(ids))
        # Latest non-retrospective category per market.
        cat_rows = conn.execute(
            f"""
            SELECT s.market_id, s.category FROM llm_scores s
              JOIN (SELECT market_id, MAX(scored_at) AS ts FROM llm_scores
                     WHERE retrospective = 0 AND market_id IN ({ph})
                     GROUP BY market_id) latest
                ON latest.market_id = s.market_id AND latest.ts = s.scored_at
             WHERE s.retrospective = 0
            """,
            ids,
        ).fetchall()
        category_by_id = {str(m): c for m, c in cat_rows}

        mid_rows = conn.execute(
            f"""
            SELECT s.market_id, s.midpoint FROM snapshots s
              JOIN (SELECT market_id, MAX(timestamp) AS ts FROM snapshots
                     WHERE market_id IN ({ph}) GROUP BY market_id) latest
                ON latest.market_id = s.market_id AND latest.ts = s.timestamp
            """,
            ids,
        ).fetchall()
        midpoint_by_id = {str(m): v for m, v in mid_rows}
    finally:
        conn.close()

    rows = []
    for mid in ids:
        category = category_by_id.get(mid)
        base_rate = base_rates.get(category) if category else None
        midpoint = midpoint_by_id.get(mid)
        divergence = (
            midpoint - base_rate
            if (base_rate is not None and midpoint is not None) else None
        )
        rows.append({
            "market_id": mid, "category": category, "base_rate": base_rate,
            "current_midpoint": midpoint, "divergence": divergence,
        })
    return pd.DataFrame(rows, columns=cols)


# --------------------------------------------------------------------------- #
# Retrospective pilot edge check
# --------------------------------------------------------------------------- #

PILOT_LABEL = (
    "CONTAMINATED PILOT — the model may have seen these outcomes in training; "
    "results are DIRECTIONAL ONLY, not evidence of live edge."
)


def run_pilot_edge_check(db_path: Path | str = DB_PATH) -> dict:
    """Compare retrospective LLM scores vs the 48h-before market price."""
    from model.train_outcome import compute_reference_timestamps

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.market_id, s.probability, s.recognized_outcome, m.resolution
              FROM llm_scores s JOIN markets m ON m.id = s.market_id
             WHERE s.retrospective = 1 AND m.resolution IN ('0', '1')
             ORDER BY s.scored_at DESC
            """
        ).fetchall()
        # Latest retrospective row per market.
        seen: dict[str, tuple] = {}
        for mid, prob, recog, res in rows:
            if str(mid) not in seen:
                seen[str(mid)] = (float(prob), int(recog), int(res))

        ids = list(seen.keys())
        refs, _ = compute_reference_timestamps(db_path, ids, hours_before=48)

        n_recognized = sum(1 for _, r, _y in seen.values() if r)
        clean = []
        for mid, (prob, recog, y) in seen.items():
            if recog or mid not in refs:
                continue
            ref = conn.execute(
                "SELECT midpoint FROM snapshots WHERE market_id = ? AND timestamp <= ? "
                "AND midpoint IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
                (mid, refs[mid].isoformat()),
            ).fetchone()
            if ref is None:
                continue
            clean.append((mid, prob, float(ref[0]), y))
    finally:
        conn.close()

    disagreements = [(m, p, px, y) for m, p, px, y in clean if abs(p - px) > 0.10]
    llm_wins = sum(1 for _, p, px, y in disagreements if abs(p - y) < abs(px - y))

    result = {
        "label": PILOT_LABEL,
        "n_pilot_scores": len(seen),
        "n_recognized_outcome_true": n_recognized,
        "n_clean_with_reference_price": len(clean),
        "n_disagreements_gt_10pts": len(disagreements),
        "llm_wins_among_disagreements": llm_wins,
        "llm_win_rate": (llm_wins / len(disagreements)) if disagreements else None,
    }

    log.warning(PILOT_LABEL)
    log.info(
        "Pilot edge check: %d scored, %d flagged recognized_outcome (excluded), "
        "%d clean with 48h-before price",
        result["n_pilot_scores"], n_recognized, len(clean),
    )
    log.info(
        "  Disagreements >10pts: %d; LLM closer to truth on %d (%s)",
        len(disagreements), llm_wins,
        f"{result['llm_win_rate']:.1%}" if result["llm_win_rate"] is not None else "n/a",
    )
    log.warning(PILOT_LABEL)

    PILOT_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PILOT_RESULTS_PATH.write_text(
        json.dumps({"generated_at": datetime.now().astimezone().isoformat(),
                    "pilot_edge_check": result}, indent=2),
        encoding="utf-8",
    )
    log.info("Saved pilot results -> %s", PILOT_RESULTS_PATH)
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("collectors.collect_llm_scores")
    setup_logging()
    load_dotenv(REPO_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        log.critical("ANTHROPIC_API_KEY missing from environment/.env — aborting.")
        return 1

    parser = argparse.ArgumentParser(description="LLM market scoring (price-blinded).")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true",
                        help="Ignore the 7-day rescore window.")
    parser.add_argument("--retrospective", action="store_true",
                        help="One-time pilot on resolved markets + edge check.")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        init_db(conn)
        if args.retrospective:
            ids = load_retrospective_candidates(DB_PATH)
            log.info("RETROSPECTIVE PILOT: %d resolved political markets", len(ids))
        else:
            ids = load_prospective_candidates(conn, DB_PATH, force=args.force)
    finally:
        conn.close()

    if args.limit is not None:
        ids = ids[: args.limit]
    if not ids:
        log.info("Nothing to score.")
        return 0

    log.info("Scoring %d markets with %s (prompt %s)", len(ids), MODEL_NAME, PROMPT_VERSION)
    run_scoring(ids, DB_PATH, retrospective=args.retrospective)

    if args.retrospective:
        run_pilot_edge_check(DB_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
