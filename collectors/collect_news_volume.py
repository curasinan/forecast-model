"""News-volume collector — GDELT 2.0 DOC API article counts per market."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "news_volume.log"

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SLEEP_BETWEEN_REQUESTS_S = 5.5   # GDELT enforces ~1 req / 5 s
MAX_RETRIES = 3
RETRY_DELAY_S = 30
REQUEST_TIMEOUT_S = 30

log = logging.getLogger("collect_news_volume")


# Logging / schema


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


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_volume (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id    TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            articles_72h INTEGER,
            articles_30d INTEGER,
            spike_ratio  REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_volume_market ON news_volume (market_id)"
    )
    conn.commit()


# Math helpers (unit-tested)


def compute_spike_ratio(articles_72h: int | None, articles_30d: int | None) -> float | None:
    """72h count divided by the 30-day DAILY average. Null-safe."""
    if articles_72h is None or articles_30d is None or articles_30d <= 0:
        return None
    daily_avg = articles_30d / 30.0
    return articles_72h / daily_avg


def sum_timeline_counts(
    payload: dict,
    now: datetime,
    trailing_hours: int = 72,
) -> tuple[int, int] | None:
    """From a ``timelinevolraw`` JSON payload, return (articles_72h, articles_30d)."""
    timeline = payload.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return None
    data = timeline[0].get("data")
    if not isinstance(data, list):
        return None

    cutoff = now - timedelta(hours=trailing_hours)
    total_30d = 0
    total_72h = 0
    for point in data:
        try:
            value = int(point.get("value", 0))
            ts_raw = str(point.get("date", ""))
            ts = datetime.strptime(ts_raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, AttributeError):
            continue
        total_30d += value
        if ts >= cutoff:
            total_72h += value
    return total_72h, total_30d


def build_query(keywords: list[str]) -> str:
    """GDELT query string: quoted multi-word phrases OR'd together."""
    terms = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        terms.append(f'"{kw}"' if " " in kw else kw)
    if len(terms) > 1:
        return "(" + " OR ".join(terms) + ")"
    return terms[0] if terms else ""


# GDELT client


def _gdelt_get(session: requests.Session, params: dict) -> dict | None:
    """GET with retries. Non-JSON responses (429 plain text) are retryable."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(GDELT_URL, params=params, timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    # Rate-limit text or HTML error page under HTTP 200.
                    log.warning(
                        "GDELT non-JSON response (attempt %d/%d): %r",
                        attempt, MAX_RETRIES, resp.text[:100],
                    )
            elif resp.status_code == 429 or resp.status_code >= 500:
                log.warning(
                    "GDELT HTTP %d (attempt %d/%d)", resp.status_code, attempt, MAX_RETRIES,
                )
            else:
                log.warning("GDELT HTTP %d — not retrying", resp.status_code)
                return None
        except requests.RequestException as exc:
            log.warning("GDELT request error (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_S)
    return None


def fetch_article_counts(
    session: requests.Session,
    keywords: list[str],
    now: datetime,
) -> tuple[int | None, int | None]:
    """(articles_72h, articles_30d) via one timelinevolraw call; artlist fallback."""
    query = build_query(keywords)
    if not query:
        return None, None

    payload = _gdelt_get(session, {
        "query": query, "mode": "timelinevolraw", "format": "json", "timespan": "30d",
    })
    if payload is not None:
        sums = sum_timeline_counts(payload, now)
        if sums is not None:
            return sums

    # Fallback: two artlist calls; counts cap at 250 records each.
    log.info("timelinevolraw unavailable for %r — falling back to artlist (capped)", query)
    counts: list[int | None] = []
    for timespan in ("72h", "30d"):
        time.sleep(SLEEP_BETWEEN_REQUESTS_S)
        payload = _gdelt_get(session, {
            "query": query, "mode": "artlist", "format": "json",
            "timespan": timespan, "maxrecords": 250,
        })
        if payload is None or not isinstance(payload.get("articles"), list):
            counts.append(None)
        else:
            counts.append(len(payload["articles"]))
    return counts[0], counts[1]


# Main


def load_candidates(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """(market_id, keywords) for active markets with a latest LLM score."""
    rows = conn.execute(
        """
        SELECT s.market_id, s.search_keywords
          FROM llm_scores s
          JOIN (SELECT market_id, MAX(scored_at) AS ts FROM llm_scores
                 WHERE retrospective = 0 GROUP BY market_id) latest
            ON latest.market_id = s.market_id AND latest.ts = s.scored_at
          JOIN markets m ON m.id = s.market_id
         WHERE s.retrospective = 0 AND m.status = 'active'
        """
    ).fetchall()
    out: list[tuple[str, list[str]]] = []
    for mid, kw_json in rows:
        try:
            keywords = json.loads(kw_json) if kw_json else []
        except json.JSONDecodeError:
            keywords = []
        if keywords:
            out.append((str(mid), [str(k) for k in keywords]))
    return out


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="GDELT news-volume collector.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=15000")
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-trader/0.1 (+collectors.collect_news_volume)"

    n_stored = n_failed = 0
    try:
        init_db(conn)
        candidates = load_candidates(conn)
        if args.limit is not None:
            candidates = candidates[: args.limit]
        log.info("News volume: %d markets with keywords (est %.0f min at %.1fs/req)",
                 len(candidates), len(candidates) * SLEEP_BETWEEN_REQUESTS_S / 60,
                 SLEEP_BETWEEN_REQUESTS_S)

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        for i, (mid, keywords) in enumerate(candidates, start=1):
            try:
                a72, a30 = fetch_article_counts(session, keywords, now)
            except Exception as exc:  # noqa: BLE001 — never crash on one market
                log.error("Market %s: unexpected error: %s", mid, exc)
                n_failed += 1
                time.sleep(SLEEP_BETWEEN_REQUESTS_S)
                continue

            if a72 is None and a30 is None:
                log.warning("Market %s: GDELT gave no usable counts (kw=%s)", mid, keywords)
                n_failed += 1
            else:
                conn.execute(
                    "INSERT INTO news_volume (market_id, collected_at, articles_72h, "
                    "articles_30d, spike_ratio) VALUES (?, ?, ?, ?, ?)",
                    (mid, now_iso, a72, a30, compute_spike_ratio(a72, a30)),
                )
                conn.commit()
                n_stored += 1

            if i % 25 == 0:
                log.info("Progress: %d/%d (stored=%d failed=%d)",
                         i, len(candidates), n_stored, n_failed)
            time.sleep(SLEEP_BETWEEN_REQUESTS_S)
    finally:
        conn.close()

    log.info("News volume done: stored=%d failed=%d", n_stored, n_failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
