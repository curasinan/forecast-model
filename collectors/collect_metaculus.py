"""Metaculus question collector + Polymarket auto-matcher."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

METACULUS_BASE_URL = "https://www.metaculus.com/api2"
QUESTIONS_ENDPOINT = f"{METACULUS_BASE_URL}/questions/"

PAGE_SIZE = 100
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 30

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "metaculus_collector.log"
MANUAL_MATCHES_PATH = REPO_ROOT / "config" / "metaculus_matches_manual.json"

# Categories the spec asks us to keep.
TARGET_CATEGORY_SLUGS = frozenset({"geopolitics", "politics"})

# Auto-match threshold and stop words from the spec.
MATCH_THRESHOLD = 0.4
STOP_WORDS = frozenset({
    "the", "a", "an", "will", "by", "before", "in", "of", "and", "or",
    "to", "for", "at", "on", "is", "be", "that", "this", "it", "as", "with",
})
TOP_MATCHES_TO_LOG = 5

log = logging.getLogger("collect_metaculus")


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


# Database


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metaculus_questions (
            id                       INTEGER PRIMARY KEY,
            title                    TEXT,
            short_title              TEXT,
            url                      TEXT,
            status                   TEXT,
            scheduled_close_time     TEXT,
            scheduled_resolve_time   TEXT,
            forecasts_count          INTEGER,
            community_probability    REAL,
            last_updated             TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metaculus_polymarket_matches (
            metaculus_id     INTEGER NOT NULL,
            polymarket_id    TEXT    NOT NULL,
            match_method     TEXT    NOT NULL,
            similarity_score REAL,
            verified         INTEGER NOT NULL DEFAULT 0,
            UNIQUE (metaculus_id, polymarket_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mpm_metaculus_id "
        "ON metaculus_polymarket_matches (metaculus_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mpm_polymarket_id "
        "ON metaculus_polymarket_matches (polymarket_id)"
    )
    conn.commit()


# Metaculus API client


def _read_token() -> str:
    token = os.environ.get("METACULUS_API_TOKEN", "").strip()
    if not token:
        log.critical(
            "METACULUS_API_TOKEN env var is missing or empty. "
            "Cannot authenticate to Metaculus API. Aborting run."
        )
        raise RuntimeError("METACULUS_API_TOKEN is not set")
    return token


def fetch_questions_page(
    session: requests.Session,
    offset: int,
) -> dict[str, Any]:
    """Fetch one page of Metaculus questions, retrying transient failures."""
    params = {
        "status": "open",
        "type": "forecast",
        "limit": PAGE_SIZE,
        "offset": offset,
        "order_by": "-activity",
    }
    last_error: BaseException | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(
                QUESTIONS_ENDPOINT,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            status = resp.status_code
            if status == 200:
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"Expected JSON object from /questions/, got {type(payload).__name__}"
                    )
                return payload
            if status == 429 or 500 <= status < 600:
                log.warning(
                    "Metaculus /questions/ returned %s on attempt %s/%s "
                    "(offset=%s); retrying in %ss",
                    status, attempt, MAX_RETRIES, offset, RETRY_DELAY_SECONDS,
                )
                last_error = RuntimeError(f"HTTP {status}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)
                continue
            # Non-retryable HTTP error.
            resp.raise_for_status()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            log.warning(
                "Metaculus request failed on attempt %s/%s (offset=%s): %s",
                attempt, MAX_RETRIES, offset, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Failed to fetch /questions/ offset={offset} after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


# Question parsing


def _categories(question: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the projects.category list (empty if missing)."""
    projects = question.get("projects") or {}
    if not isinstance(projects, dict):
        return []
    cats = projects.get("category") or []
    return [c for c in cats if isinstance(c, dict)]


def _is_target_category(question: dict[str, Any]) -> bool:
    for cat in _categories(question):
        slug = (cat.get("slug") or "").strip().lower()
        if slug in TARGET_CATEGORY_SLUGS:
            return True
    return False


def _community_probability(question: dict[str, Any]) -> float | None:
    """Pull the recency-weighted community probability, defensively."""
    inner = question.get("question") if isinstance(question.get("question"), dict) else None
    aggs = (inner or question).get("aggregations")
    if not isinstance(aggs, dict):
        return None
    rw = aggs.get("recency_weighted")
    if not isinstance(rw, dict):
        return None
    latest = rw.get("latest")
    if latest is None:
        return None
    if isinstance(latest, (int, float)):
        return float(latest)
    if isinstance(latest, dict):
        centers = latest.get("centers")
        if isinstance(centers, list) and centers:
            try:
                return float(centers[0])
            except (TypeError, ValueError):
                return None
        # Fallback for older shapes that exposed a "center" scalar
        ctr = latest.get("center")
        if isinstance(ctr, (int, float)):
            return float(ctr)
    return None


def _question_url(question: dict[str, Any]) -> str | None:
    raw = question.get("url")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    qid = question.get("id")
    if qid is None:
        return None
    return f"https://www.metaculus.com/questions/{qid}/"


def _coerce_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def upsert_metaculus_question(
    conn: sqlite3.Connection,
    question: dict[str, Any],
    now_iso: str,
) -> str:
    """Insert or update a metaculus_questions row. Returns 'inserted'/'updated'."""
    qid = _coerce_int(question.get("id"))
    if qid is None:
        raise ValueError("question missing 'id' field")

    title = question.get("title")
    short_title = question.get("short_title")
    url = _question_url(question)
    status = question.get("status")
    scheduled_close = question.get("scheduled_close_time")
    scheduled_resolve = question.get("scheduled_resolve_time")
    forecasts_count = _coerce_int(question.get("forecasts_count"))
    community = _community_probability(question)

    cursor = conn.execute(
        "SELECT 1 FROM metaculus_questions WHERE id = ?", (qid,)
    )
    if cursor.fetchone() is not None:
        conn.execute(
            """
            UPDATE metaculus_questions
               SET title                  = ?,
                   short_title            = ?,
                   url                    = ?,
                   status                 = ?,
                   scheduled_close_time   = ?,
                   scheduled_resolve_time = ?,
                   forecasts_count        = ?,
                   community_probability  = ?,
                   last_updated           = ?
             WHERE id = ?
            """,
            (title, short_title, url, status, scheduled_close, scheduled_resolve,
             forecasts_count, community, now_iso, qid),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO metaculus_questions
          (id, title, short_title, url, status,
           scheduled_close_time, scheduled_resolve_time,
           forecasts_count, community_probability, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (qid, title, short_title, url, status,
         scheduled_close, scheduled_resolve,
         forecasts_count, community, now_iso),
    )
    return "inserted"


# Auto-matching


_PUNCT_RE = re.compile(r"[^\w\s]+")


def _tokenize_title(title: str | None) -> set[str]:
    """Lowercase, strip punctuation, drop stop words. Returns the token set."""
    if not title:
        return set()
    cleaned = _PUNCT_RE.sub(" ", title.lower())
    tokens = {tok for tok in cleaned.split() if tok and tok not in STOP_WORDS}
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    """Token overlap = |a ∩ b| / |a ∪ b|. Returns 0.0 if either side empty."""
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def find_auto_matches(
    conn: sqlite3.Connection,
    threshold: float = MATCH_THRESHOLD,
) -> list[tuple[int, str, float, str, str]]:
    """Best Polymarket match by token overlap for each unmatched Metaculus question."""
    # Polymarket candidate pool, pre-tokenized once.
    pm_rows = conn.execute(
        """
        SELECT id, question
          FROM markets
         WHERE status = 'active'
           AND question IS NOT NULL
           AND question != ''
        """
    ).fetchall()
    pm_tokens: list[tuple[str, str, set[str]]] = [
        (str(pid), str(pq), _tokenize_title(str(pq))) for pid, pq in pm_rows
    ]

    # Metaculus questions not yet matched.
    mq_rows = conn.execute(
        """
        SELECT mq.id, mq.title
          FROM metaculus_questions mq
          LEFT JOIN metaculus_polymarket_matches mpm
                 ON mpm.metaculus_id = mq.id
         WHERE mpm.metaculus_id IS NULL
           AND mq.title IS NOT NULL
        """
    ).fetchall()

    matches: list[tuple[int, str, float, str, str]] = []
    for mid, mtitle in mq_rows:
        m_tokens = _tokenize_title(str(mtitle))
        if not m_tokens:
            continue
        best_score = 0.0
        best_pid: str | None = None
        best_ptitle: str | None = None
        for pid, ptitle, p_tokens in pm_tokens:
            score = _jaccard(m_tokens, p_tokens)
            if score > best_score:
                best_score = score
                best_pid = pid
                best_ptitle = ptitle
        if best_pid is not None and best_score >= threshold:
            matches.append((int(mid), best_pid, best_score, str(mtitle), best_ptitle or ""))
    return matches


def upsert_match(
    conn: sqlite3.Connection,
    metaculus_id: int,
    polymarket_id: str,
    match_method: str,
    similarity_score: float | None,
    verified: int,
) -> str:
    """Upsert a single match row. Returns 'inserted' / 'updated'."""
    cursor = conn.execute(
        "SELECT 1 FROM metaculus_polymarket_matches "
        "WHERE metaculus_id = ? AND polymarket_id = ?",
        (metaculus_id, polymarket_id),
    )
    if cursor.fetchone() is not None:
        conn.execute(
            """
            UPDATE metaculus_polymarket_matches
               SET match_method     = ?,
                   similarity_score = ?,
                   verified         = ?
             WHERE metaculus_id = ? AND polymarket_id = ?
            """,
            (match_method, similarity_score, verified, metaculus_id, polymarket_id),
        )
        return "updated"
    conn.execute(
        """
        INSERT INTO metaculus_polymarket_matches
          (metaculus_id, polymarket_id, match_method, similarity_score, verified)
        VALUES (?, ?, ?, ?, ?)
        """,
        (metaculus_id, polymarket_id, match_method, similarity_score, verified),
    )
    return "inserted"


# Manual matches


def load_manual_matches(path: Path = MANUAL_MATCHES_PATH) -> list[dict[str, Any]]:
    """Read the manual-matches JSON file. Create an empty one if missing."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]\n", encoding="utf-8")
        log.info("Created empty manual-matches file at %s", path)
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        log.error("Manual matches file is not valid JSON: %s", exc)
        return []
    if not isinstance(data, list):
        log.error("Manual matches file must contain a JSON array, got %s",
                  type(data).__name__)
        return []
    return [d for d in data if isinstance(d, dict)]


# Feature helper (for downstream use)


def get_metaculus_divergence(
    market_ids: Iterable[str],
    db_path: Path | str = DB_PATH,
):
    """Community probability minus the latest snapshot midpoint, per market_id."""
    import pandas as pd  # local import keeps the CLI light

    ids = [str(m) for m in market_ids]
    if not ids:
        return pd.DataFrame(columns=["market_id", "metaculus_divergence"])

    placeholders = ",".join("?" * len(ids))
    conn = sqlite3.connect(db_path)
    try:
        match_rows = conn.execute(
            f"""
            SELECT polymarket_id, metaculus_id, verified, similarity_score
              FROM metaculus_polymarket_matches
             WHERE polymarket_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        best: dict[str, tuple[int, int, float]] = {}
        for pid, mid, verified, score in match_rows:
            score_val = -1.0 if score is None else float(score)
            verified_val = int(verified or 0)
            key = (verified_val, score_val)
            existing = best.get(pid)
            if existing is None or key > (existing[1], existing[2]):
                best[pid] = (int(mid), verified_val, score_val)

        # Pull community probabilities for the selected metaculus_ids.
        m_ids = sorted({v[0] for v in best.values()})
        community_by_mid: dict[int, float | None] = {}
        if m_ids:
            mph = ",".join("?" * len(m_ids))
            for mid, prob in conn.execute(
                f"SELECT id, community_probability FROM metaculus_questions "
                f"WHERE id IN ({mph})",
                m_ids,
            ).fetchall():
                community_by_mid[int(mid)] = (
                    float(prob) if prob is not None else None
                )

        # Latest midpoint per polymarket_id.
        midpoint_by_pid: dict[str, float | None] = {}
        for pid, mid_val in conn.execute(
            f"""
            SELECT s.market_id, s.midpoint
              FROM snapshots s
              JOIN (
                  SELECT market_id, MAX(timestamp) AS ts
                    FROM snapshots
                   WHERE market_id IN ({placeholders})
                   GROUP BY market_id
              ) latest
                ON latest.market_id = s.market_id AND latest.ts = s.timestamp
            """,
            ids,
        ).fetchall():
            midpoint_by_pid[str(pid)] = (
                float(mid_val) if mid_val is not None else None
            )
    finally:
        conn.close()

    rows = []
    for pid in ids:
        mid_info = best.get(pid)
        if mid_info is None:
            divergence: float | None = None
        else:
            community = community_by_mid.get(mid_info[0])
            midpoint = midpoint_by_pid.get(pid)
            if community is None or midpoint is None:
                divergence = None
            else:
                divergence = community - midpoint
        rows.append({"market_id": pid, "metaculus_divergence": divergence})
    return pd.DataFrame(rows, columns=["market_id", "metaculus_divergence"])


# Main


def run() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)

        token = _read_token()
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "User-Agent": "polymarket-trader/0.1 (+collectors.collect_metaculus)",
        })

        now_iso = datetime.now(timezone.utc).isoformat()
        offset = 0
        page = 0
        total_seen = 0
        target_kept = 0
        with_community = 0
        inserted = 0
        updated = 0

        while True:
            page += 1
            try:
                payload = fetch_questions_page(session, offset)
            except RuntimeError as exc:
                log.critical("Metaculus API unreachable; aborting run: %s", exc)
                return 1

            results = payload.get("results")
            if not isinstance(results, list):
                log.error(
                    "Page %s: expected 'results' list, got %s",
                    page, type(results).__name__
                )
                break

            log.info(
                "Page %s: fetched %s questions (offset=%s)",
                page, len(results), offset,
            )

            for question in results:
                if not isinstance(question, dict):
                    continue
                total_seen += 1
                try:
                    if not _is_target_category(question):
                        continue
                    target_kept += 1
                    if _community_probability(question) is not None:
                        with_community += 1
                    result = upsert_metaculus_question(conn, question, now_iso)
                    if result == "inserted":
                        inserted += 1
                    else:
                        updated += 1
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "Failed to process Metaculus question id=%s: %s",
                        question.get("id"), exc,
                    )
                    continue
            conn.commit()

            if len(results) < PAGE_SIZE:
                log.info(
                    "Page %s returned fewer than %s; pagination complete",
                    page, PAGE_SIZE,
                )
                break
            if not payload.get("next"):
                log.info("Page %s has no 'next' link; pagination complete", page)
                break
            offset += PAGE_SIZE

        log.info(
            "Step 1 complete: total_seen=%s, target_category_kept=%s "
            "(inserted=%s, updated=%s), with_community_probability=%s",
            total_seen, target_kept, inserted, updated, with_community,
        )

        # Step 2: auto-matching --------------------------------------------- #
        new_matches = find_auto_matches(conn)
        for mid, pid, score, mq_title, pm_title in new_matches:
            try:
                upsert_match(
                    conn,
                    metaculus_id=mid,
                    polymarket_id=pid,
                    match_method="auto",
                    similarity_score=float(score),
                    verified=0,
                )
                log.info(
                    "Auto-match score=%.3f  Metaculus[%s]='%s'  <->  Polymarket[%s]='%s'",
                    score, mid,
                    (mq_title[:60] + "...") if len(mq_title) > 60 else mq_title,
                    pid,
                    (pm_title[:60] + "...") if len(pm_title) > 60 else pm_title,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to insert auto-match (%s, %s): %s", mid, pid, exc)
        conn.commit()
        log.info("Step 2 complete: %s new auto-matches found this run", len(new_matches))

        # Step 3: manual overrides ------------------------------------------ #
        manual = load_manual_matches()
        manual_inserted = manual_updated = 0
        for entry in manual:
            mid_raw = entry.get("metaculus_id")
            pid_raw = entry.get("polymarket_id")
            mid = _coerce_int(mid_raw)
            if mid is None or pid_raw is None:
                log.warning("Skipping malformed manual match entry: %r", entry)
                continue
            try:
                result = upsert_match(
                    conn,
                    metaculus_id=mid,
                    polymarket_id=str(pid_raw),
                    match_method="manual",
                    similarity_score=None,
                    verified=1,
                )
                if result == "inserted":
                    manual_inserted += 1
                else:
                    manual_updated += 1
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to upsert manual match %r: %s", entry, exc)
        conn.commit()
        log.info(
            "Step 3 complete: manual matches upserted (inserted=%s, updated=%s)",
            manual_inserted, manual_updated,
        )

        # Step 4: summary --------------------------------------------------- #
        total_questions = conn.execute(
            "SELECT COUNT(*) FROM metaculus_questions"
        ).fetchone()[0]
        with_prob = conn.execute(
            "SELECT COUNT(*) FROM metaculus_questions "
            "WHERE community_probability IS NOT NULL"
        ).fetchone()[0]
        total_matches = conn.execute(
            "SELECT COUNT(*) FROM metaculus_polymarket_matches"
        ).fetchone()[0]

        log.info("=" * 60)
        log.info("Run summary:")
        log.info("  total Metaculus questions in DB:        %s", total_questions)
        log.info("  with non-null community_probability:    %s", with_prob)
        log.info("  new auto-matches this run:              %s", len(new_matches))
        log.info("  total matches in DB:                    %s", total_matches)
        log.info("=" * 60)

        if new_matches:
            top = sorted(new_matches, key=lambda r: r[2], reverse=True)[:TOP_MATCHES_TO_LOG]
            log.info("Top %s auto-matches by similarity:", len(top))
            for mid, pid, score, mq_title, pm_title in top:
                log.info("  score=%.3f", score)
                log.info("    Metaculus[%s]: %s", mid, mq_title)
                log.info("    Polymarket[%s]: %s", pid, pm_title)

        return 0
    finally:
        conn.close()


def main() -> int:
    setup_logging()
    log.info("Starting Metaculus collection run")
    try:
        exit_code = run()
    except RuntimeError as exc:
        # _read_token() raises RuntimeError after logging critical; honor exit 1.
        log.critical("Run aborted: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.critical("Unhandled error during run: %s", exc, exc_info=True)
        return 1
    log.info("Collection run finished with exit code %s", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
