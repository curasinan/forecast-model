"""Polymarket market data collector.

Fetches active events (and their nested markets) from the Gamma API and
upserts them into a local SQLite database. Markets previously marked
'active' but not seen in this run are flagged 'resolved_or_removed'.

Run from the repo root:
    python -m collectors.collect_markets
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from collectors._lock import LockHeld, PidLock
from src.utils.dates import parse_iso8601

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
EVENTS_ENDPOINT = f"{GAMMA_BASE_URL}/events"
MARKETS_ENDPOINT = f"{GAMMA_BASE_URL}/markets"
PAGE_SIZE = 100
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 30

# Max Gamma single-market lookups per collector run for the resolution pass.
# The collector fires every 6h -> up to 400 resolutions/day, well ahead of the
# observed resolution rate, while keeping each run fast.
RESOLUTION_PASS_LIMIT = 100

# Max Gamma lookups per run for the scored-market probe. Since the 422
# pagination ceiling forces the stale sweep to be skipped on every run,
# NOTHING else flips status='active' markets to resolved_or_removed anymore —
# so this pass probes prospective-scored, still-'active' markets directly
# against Gamma's `closed` flag, independent of the sweep. At ~305 scored
# markets and 4 runs/day the whole cohort is re-checked about every 18h.
SCORED_PROBE_LIMIT = 100

# A latest midpoint at/beyond these bounds is the local signature of a market
# that has de facto resolved (price pinned) — those candidates are probed first.
PINNED_MIDPOINT_LOW = 0.02
PINNED_MIDPOINT_HIGH = 0.98


class PaginationLimitReached(Exception):
    """Raised when the Gamma API signals the offset pagination ceiling (HTTP 422).

    As of 2026-06 the Gamma /events endpoint rejects offset >= 2100 with a 422.
    This is a normal end-of-pagination signal, not a transient error — the
    caller stops paginating cleanly rather than retrying.
    """

    def __init__(self, offset: int):
        self.offset = offset
        super().__init__(f"pagination limit reached at offset={offset}")

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "polymarket.db"
LOG_PATH = REPO_ROOT / "logs" / "market_collector.log"

log = logging.getLogger("collect_markets")


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    log.setLevel(logging.INFO)
    log.propagate = False
    # Avoid duplicate handlers if setup_logging() is called twice.
    for handler in list(log.handlers):
        log.removeHandler(handler)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS markets (
            id                TEXT PRIMARY KEY,
            question          TEXT,
            condition_id      TEXT,
            slug              TEXT,
            clob_token_ids    TEXT,
            end_date          TEXT,
            volume            REAL,
            volume_24h        REAL,
            liquidity         REAL,
            status            TEXT NOT NULL DEFAULT 'active',
            resolution        TEXT,
            created_at        TEXT,
            last_updated      TEXT,
            orderbook_missing INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Additive migration for databases created before orderbook_missing existed.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(markets)")}
    if "orderbook_missing" not in existing_columns:
        conn.execute(
            "ALTER TABLE markets "
            "ADD COLUMN orderbook_missing INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def fetch_events_page(offset: int) -> list[dict[str, Any]]:
    """Fetch a single page of events, retrying transient failures.

    Raises RuntimeError if all retries are exhausted so the caller can
    decide whether the failure should abort the run.
    """
    params = {
        "closed": "false",
        "order": "id",
        "ascending": "false",
        "limit": PAGE_SIZE,
        "offset": offset,
    }

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                EVENTS_ENDPOINT,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            status = response.status_code
            if status == 200:
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError(
                        f"Expected JSON array from /events, got {type(payload).__name__}"
                    )
                return payload
            if status == 422:
                # Gamma's offset-pagination ceiling. Not retryable, not an
                # error — signal clean end-of-pagination to the caller.
                raise PaginationLimitReached(offset)
            if status == 429 or 500 <= status < 600:
                log.warning(
                    "Gamma /events returned %s on attempt %s/%s (offset=%s); "
                    "retrying in %ss",
                    status,
                    attempt,
                    MAX_RETRIES,
                    offset,
                    RETRY_DELAY_SECONDS,
                )
                last_error = RuntimeError(f"HTTP {status}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)
                continue
            # Non-retryable HTTP error.
            response.raise_for_status()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            log.warning(
                "Gamma /events request failed on attempt %s/%s (offset=%s): %s",
                attempt,
                MAX_RETRIES,
                offset,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Failed to fetch /events offset={offset} after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


def iter_markets(events: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for event in events:
        markets = event.get("markets") or []
        if not isinstance(markets, list):
            log.warning(
                "Event %s has non-list markets field; skipping",
                event.get("id"),
            )
            continue
        for market in markets:
            if isinstance(market, dict):
                yield market


def _normalize_clob_token_ids(value: Any) -> str | None:
    """Gamma returns clobTokenIds as either a list or a JSON-encoded string.

    Normalize to a JSON array string for storage.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        return json.dumps(parsed)
    if isinstance(value, list):
        return json.dumps(value)
    return json.dumps(value)


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def upsert_market(
    conn: sqlite3.Connection,
    market: dict[str, Any],
    now_iso: str,
) -> str:
    """Insert or update a market row. Returns 'inserted' or 'updated'."""
    market_id = market.get("id")
    if market_id is None:
        raise ValueError("market missing 'id' field")
    market_id = str(market_id)

    question = market.get("question")
    condition_id = market.get("conditionId")
    slug = market.get("slug")
    clob_token_ids = _normalize_clob_token_ids(market.get("clobTokenIds"))
    end_date = market.get("endDate")
    volume = _coerce_float(market.get("volume"))
    volume_24h = _coerce_float(market.get("volume24hr"))
    liquidity = _coerce_float(market.get("liquidity"))

    cursor = conn.execute("SELECT 1 FROM markets WHERE id = ?", (market_id,))
    exists = cursor.fetchone() is not None

    if exists:
        conn.execute(
            """
            UPDATE markets
               SET volume            = ?,
                   volume_24h        = ?,
                   liquidity         = ?,
                   status            = 'active',
                   last_updated      = ?,
                   orderbook_missing = 0
             WHERE id = ?
            """,
            (volume, volume_24h, liquidity, now_iso, market_id),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO markets (
            id, question, condition_id, slug, clob_token_ids, end_date,
            volume, volume_24h, liquidity, status, resolution,
            created_at, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
        """,
        (
            market_id,
            question,
            condition_id,
            slug,
            clob_token_ids,
            end_date,
            volume,
            volume_24h,
            liquidity,
            now_iso,
            now_iso,
        ),
    )
    return "inserted"


def mark_stale(
    conn: sqlite3.Connection,
    seen_ids: set[str],
    now_iso: str,
) -> int:
    cursor = conn.execute("SELECT id FROM markets WHERE status = 'active'")
    active_ids = {row[0] for row in cursor.fetchall()}
    stale_ids = active_ids - seen_ids
    if not stale_ids:
        return 0

    conn.executemany(
        """
        UPDATE markets
           SET status = 'resolved_or_removed',
               last_updated = ?
         WHERE id = ?
        """,
        [(now_iso, mid) for mid in stale_ids],
    )
    return len(stale_ids)


# --------------------------------------------------------------------------- #
# Resolution capture (Gamma outcomePrices -> markets.resolution)
# --------------------------------------------------------------------------- #


def _parse_outcome_prices(raw: Any) -> int | None:
    """Normalize Gamma's ``outcomePrices`` field to a binary resolution.

    Gamma returns it as a JSON-encoded string like ``'["0", "1"]'`` (YES price
    first), but is handled defensively as a real list too — the API has
    changed encodings before (cf. ``clobTokenIds``). A YES price pinned to 1
    means resolved YES, pinned to 0 means resolved NO. Anything interior
    (e.g. ``["0.56", "0.44"]`` on a removed/unresolved market) is not a
    definitive outcome -> None. Never raises.
    """
    if raw is None:
        return None
    prices = raw
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if not isinstance(prices, list) or len(prices) != 2:
        return None
    try:
        yes_price = float(prices[0])
    except (TypeError, ValueError):
        return None
    if yes_price >= 0.999:
        return 1
    if yes_price <= 0.001:
        return 0
    return None


def fetch_market_status(
    session: requests.Session,
    market_id: str,
) -> tuple[str, int | None]:
    """Fetch one market from Gamma; return (state, resolution).

    States:
    - ``"closed"``  — Gamma says the market is closed; resolution is 1/0 when
      ``outcomePrices`` is definitive, else None (still closed, outcome pending).
    - ``"open"``    — market exists and is not closed.
    - ``"gone"``    — 404 (removed from Gamma).
    - ``"error"``   — retries exhausted / unexpected response; caller should
      leave the row untouched and retry on a later run.
    """
    url = f"{MARKETS_ENDPOINT}/{market_id}"
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            status = response.status_code
            if status == 200:
                market = response.json()
                if not isinstance(market, dict):
                    return "error", None
                if not market.get("closed"):
                    # Not actually closed on Gamma's side; no trustworthy outcome.
                    return "open", None
                return "closed", _parse_outcome_prices(market.get("outcomePrices"))
            if status == 404:
                return "gone", None
            if status == 429 or 500 <= status < 600:
                log.warning(
                    "Gamma /markets/%s returned %s on attempt %s/%s; retrying in %ss",
                    market_id, status, attempt, MAX_RETRIES, RETRY_DELAY_SECONDS,
                )
                last_error = RuntimeError(f"HTTP {status}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)
                continue
            log.warning("Gamma /markets/%s returned unexpected HTTP %s", market_id, status)
            return "error", None
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            log.warning(
                "Gamma /markets/%s request failed on attempt %s/%s: %s",
                market_id, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    log.error(
        "Gamma /markets/%s: retries exhausted (%s); leaving resolution NULL",
        market_id, last_error,
    )
    return "error", None


def fetch_market_resolution(
    session: requests.Session,
    market_id: str,
) -> int | None:
    """Fetch one market from Gamma and return its resolution (1/0) or None.

    None means: not found (404), not closed yet, no definitive outcome, or
    retries exhausted on transient errors. Callers leave the DB row NULL in
    all of those cases.
    """
    state, resolution = fetch_market_status(session, market_id)
    return resolution if state == "closed" else None


def fetch_missing_resolutions(
    conn: sqlite3.Connection,
    session: requests.Session | None = None,
    limit: int = RESOLUTION_PASS_LIMIT,
    fetcher=fetch_market_resolution,
) -> int:
    """Populate ``markets.resolution`` for snapshot-backed resolved markets.

    Picks up to ``limit`` markets with status='resolved_or_removed', NULL
    resolution, and at least one snapshot (only snapshot-backed markets can
    ever become training rows), newest last_updated first so freshly-flipped
    markets are captured promptly. Returns the number of rows updated.
    Per-market failures are logged and skipped; this never aborts the run.
    """
    rows = conn.execute(
        """
        SELECT m.id FROM markets m
         WHERE m.status = 'resolved_or_removed'
           AND m.resolution IS NULL
           AND EXISTS (SELECT 1 FROM snapshots s WHERE s.market_id = m.id)
         ORDER BY m.last_updated DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return 0

    if session is None:
        session = requests.Session()

    updated = 0
    for (market_id,) in rows:
        market_id = str(market_id)
        try:
            resolution = fetcher(session, market_id)
        except Exception as exc:  # noqa: BLE001 — one bad market must not kill the run
            log.error("Resolution fetch failed for market %s: %s", market_id, exc)
            continue
        if resolution is None:
            continue
        conn.execute(
            "UPDATE markets SET resolution = ? WHERE id = ?",
            (resolution, market_id),
        )
        updated += 1
    conn.commit()
    return updated


def probe_scored_unresolved(
    conn: sqlite3.Connection,
    session: requests.Session | None = None,
    limit: int = SCORED_PROBE_LIMIT,
    fetcher=fetch_market_status,
    now_iso: str | None = None,
) -> tuple[int, int]:
    """Probe prospective-scored, still-'active' markets directly against Gamma.

    The stale sweep is skipped on every run (422 pagination truncation), so no
    market ever flips to resolved_or_removed and the resolution pass starves.
    This pass restores the bookkeeping independent of the sweep: any market we
    have a prospective LLM score for, still marked 'active' with NULL
    resolution, is checked against Gamma's ``closed`` flag. Closed (or 404)
    markets get status='resolved_or_removed' + last_updated=now, and the
    resolution when ``outcomePrices`` is definitive; ambiguous outcomes leave
    resolution NULL for ``fetch_missing_resolutions`` to retry next run.

    Candidates are probed pinned-midpoint-first (latest midpoint at/beyond
    0.02/0.98 — the local signature of a de facto resolved market), so likely
    resolutions are captured earliest. Empirically end_date is useless as a
    trigger here: these markets close far before their formal end date.

    Returns (n_flipped, n_resolutions_written). Never raises on a per-market
    failure. Missing llm_scores table (scorer never ran) -> (0, 0).
    """
    try:
        rows = conn.execute(
            """
            SELECT m.id,
                   (SELECT sn.midpoint FROM snapshots sn
                     WHERE sn.market_id = m.id
                     ORDER BY sn.timestamp DESC LIMIT 1) AS last_mid
              FROM markets m
             WHERE m.status = 'active'
               AND m.resolution IS NULL
               AND EXISTS (SELECT 1 FROM llm_scores ls
                            WHERE ls.market_id = m.id AND ls.retrospective = 0)
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # llm_scores doesn't exist until the scorer has run at least once.
        log.info("Scored-market probe skipped: %s", exc)
        return 0, 0
    if not rows:
        return 0, 0

    def _pinned(mid_val) -> bool:
        return mid_val is not None and (
            mid_val <= PINNED_MIDPOINT_LOW or mid_val >= PINNED_MIDPOINT_HIGH
        )

    candidates = sorted(rows, key=lambda r: (0 if _pinned(r[1]) else 1, str(r[0])))
    candidates = candidates[:limit]

    if session is None:
        session = requests.Session()
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()

    flipped = 0
    resolved = 0
    for market_id, _last_mid in candidates:
        market_id = str(market_id)
        try:
            state, resolution = fetcher(session, market_id)
        except Exception as exc:  # noqa: BLE001 — one bad market must not kill the run
            log.error("Scored-market probe failed for market %s: %s", market_id, exc)
            continue
        if state not in ("closed", "gone"):
            continue  # open -> still live; error -> retry a later run
        conn.execute(
            """
            UPDATE markets
               SET status = 'resolved_or_removed',
                   last_updated = ?,
                   resolution = COALESCE(?, resolution)
             WHERE id = ?
            """,
            (now_iso, resolution, market_id),
        )
        flipped += 1
        if resolution is not None:
            resolved += 1
    conn.commit()
    return flipped, resolved


# Backwards-compatible alias: this helper used to live in this module before
# being promoted to ``src.utils.dates``. Kept so any old import paths still work.
_parse_iso8601 = parse_iso8601


_WEATHER_SNAPSHOT_RE = re.compile(
    r"\b(temperature|temp|°[FC]|highest temperature|lowest temperature|"
    r"high temp|low temp|degrees?\s+[FC])\b",
    re.IGNORECASE,
)


def _is_weather_market_question(question: str | None) -> bool:
    return bool(question and _WEATHER_SNAPSHOT_RE.search(question))


def get_snapshot_candidates(
    db_path: Path | str = DB_PATH,
) -> list[dict[str, Any]]:
    """Return markets eligible for order-book snapshotting.

    Filter:
      - status = 'active'
      - clob_token_ids is non-empty (not NULL, '', or '[]')
      - Politics / default: liquidity > 250000 OR volume > 500000,
        end_date more than 24 hours in the future
      - Weather override: lower liquidity/volume floors and nearer expiry
        (temperature markets are thinner and resolve daily)
      - orderbook_missing is not set (i.e. the CLOB has a book for it;
        the flag is cleared on every collect_markets run so a relisted
        market becomes eligible again automatically)

    Each returned dict has keys: ``id``, ``question``, ``clob_token_ids``.
    Also logs the count of candidates vs total active markets.
    """
    # Import here to avoid circular imports at module load for non-weather runs.
    try:
        from src.config import settings as _settings

        weather_min_liq = float(_settings.weather_min_liquidity)
        weather_min_vol = float(_settings.weather_min_volume)
        weather_end_hours = float(_settings.weather_snapshot_end_hours)
    except Exception:  # noqa: BLE001
        weather_min_liq, weather_min_vol, weather_end_hours = 1000.0, 5000.0, 2.0

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, question, clob_token_ids, end_date, liquidity, volume
              FROM markets
             WHERE status = 'active'
               AND clob_token_ids IS NOT NULL
               AND clob_token_ids != ''
               AND clob_token_ids != '[]'
               AND end_date IS NOT NULL
               AND end_date != ''
               AND (orderbook_missing = 0 OR orderbook_missing IS NULL)
            """
        ).fetchall()
        total_active = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE status = 'active'"
        ).fetchone()[0]
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    politics_cutoff = now + timedelta(hours=24)
    weather_cutoff = now + timedelta(hours=weather_end_hours)
    candidates: list[dict[str, Any]] = []
    weather_n = 0
    for market_id, question, clob_token_ids, end_date, liquidity, volume in rows:
        end_dt = _parse_iso8601(end_date)
        if end_dt is None:
            continue
        liq = float(liquidity or 0.0)
        vol = float(volume or 0.0)
        is_weather = _is_weather_market_question(question)
        if is_weather:
            if liq <= weather_min_liq and vol <= weather_min_vol:
                continue
            if end_dt <= weather_cutoff:
                continue
            weather_n += 1
        else:
            if liq <= 250000 and vol <= 500000:
                continue
            if end_dt <= politics_cutoff:
                continue
        candidates.append(
            {
                "id": market_id,
                "question": question,
                "clob_token_ids": clob_token_ids,
            }
        )

    pct = 100 * len(candidates) / total_active if total_active else 0.0
    log.info(
        "Snapshot candidates: %s / %s active markets (%.2f%%); weather=%s",
        len(candidates),
        total_active,
        pct,
        weather_n,
    )
    return candidates


def run() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        # Wait up to 15s on a locked DB instead of failing instantly — the
        # snapshot collector (and occasional backfill scripts) write to the
        # same file on their own schedules.
        conn.execute("PRAGMA busy_timeout = 15000")
        init_db(conn)

        now_iso = datetime.now(timezone.utc).isoformat()
        seen_ids: set[str] = set()
        inserted = 0
        updated = 0
        processed = 0
        offset = 0
        page = 0
        pagination_truncated = False

        while True:
            page += 1
            try:
                events = fetch_events_page(offset)
            except PaginationLimitReached as exc:
                log.info(
                    "Reached pagination limit (HTTP 422 at offset %s) — stopping "
                    "pagination and proceeding to stale sweep.", exc.offset,
                )
                pagination_truncated = True
                break
            except RuntimeError as exc:
                log.critical(
                    "Gamma API unreachable; aborting run: %s", exc
                )
                return 1

            if not events:
                log.info("No events returned at offset=%s; pagination complete", offset)
                break

            log.info(
                "Page %s: fetched %s events (offset=%s)",
                page,
                len(events),
                offset,
            )

            for market in iter_markets(events):
                try:
                    market_id = market.get("id")
                    if market_id is None:
                        log.warning("Skipping market with no id: %s", market)
                        continue
                    market_id = str(market_id)

                    result = upsert_market(conn, market, now_iso)
                    seen_ids.add(market_id)
                    processed += 1
                    if result == "inserted":
                        inserted += 1
                    else:
                        updated += 1
                except Exception as exc:
                    log.error(
                        "Failed to process market id=%s: %s",
                        market.get("id"),
                        exc,
                    )
                    continue

            conn.commit()

            if len(events) < PAGE_SIZE:
                log.info(
                    "Page %s returned fewer than %s events; pagination complete",
                    page,
                    PAGE_SIZE,
                )
                break
            offset += PAGE_SIZE

        if pagination_truncated:
            # The API truncated pagination at its offset ceiling, so seen_ids
            # is only the newest slice of the active universe (not the whole
            # set). Running mark_stale here would wrongly flip every active
            # market beyond the ceiling to resolved_or_removed. Skip it — a
            # resolved market lingering as 'active' is caught downstream by the
            # signal generator's freshness guard and the snapshot 404 handler,
            # which is far safer than mass-mislabeling live markets.
            stale_count = 0
            log.warning(
                "Stale sweep SKIPPED — pagination was truncated by the API "
                "(only newest %s markets seen). Resolved-market detection "
                "deferred to downstream guards.",
                len(seen_ids),
            )
        else:
            stale_count = mark_stale(conn, seen_ids, now_iso)
        conn.commit()

        # Resolution pass: capture Gamma outcomePrices for snapshot-backed
        # markets that resolved (flipped by this or any earlier run) but have
        # no stored outcome yet. Bounded per run to keep the collector fast.
        try:
            resolutions_captured = fetch_missing_resolutions(conn)
        except Exception as exc:  # noqa: BLE001 — never let this kill the run
            log.error("Resolution pass failed: %s", exc)
            resolutions_captured = 0
        if resolutions_captured:
            log.info("Resolution pass: captured %s outcome(s)", resolutions_captured)

        # Scored-market probe: with the stale sweep permanently skipped (422
        # truncation), this is the ONLY path that flips scored 'active' markets
        # to resolved_or_removed. See probe_scored_unresolved docstring.
        try:
            probe_flipped, probe_resolved = probe_scored_unresolved(conn)
        except Exception as exc:  # noqa: BLE001 — never let this kill the run
            log.error("Scored-market probe failed: %s", exc)
            probe_flipped = probe_resolved = 0
        if probe_flipped:
            log.info(
                "Scored-market probe: flipped %s market(s) to resolved_or_removed "
                "(%s with definitive outcome)",
                probe_flipped, probe_resolved,
            )

        log.info(
            "Run summary: processed=%s, new=%s, updated=%s, marked_stale=%s%s, "
            "resolutions_captured=%s, scored_probe_flipped=%s, scored_probe_resolved=%s",
            processed,
            inserted,
            updated,
            stale_count,
            " (stale sweep skipped: pagination truncated)" if pagination_truncated else "",
            resolutions_captured,
            probe_flipped,
            probe_resolved,
        )
        return 0
    finally:
        conn.close()


LOCK_PATH = REPO_ROOT / "logs" / "collect_markets.lock"


def main() -> int:
    from src.retired_polymarket import refuse_polymarket_runtime

    refuse_polymarket_runtime("collectors.collect_markets")
    setup_logging()
    try:
        with PidLock(LOCK_PATH):
            log.info("Starting Polymarket market collection run")
            try:
                exit_code = run()
            except Exception as exc:
                log.critical(
                    "Unhandled error during collection run: %s", exc, exc_info=True
                )
                return 1
            log.info("Collection run finished with exit code %s", exit_code)
            return exit_code
    except LockHeld as exc:
        log.info(
            "Previous collect_markets run still active (pid=%s); skipping this cycle",
            exc.pid,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
