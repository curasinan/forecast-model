"""Show price movement summary for every market with >= 20 snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "polymarket.db"
MIN_SNAPSHOTS = 20
MOVE_THRESHOLD = 0.05  # 5 cents


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        f"""
        WITH valid AS (
            SELECT market_id, timestamp, midpoint
              FROM snapshots
             WHERE midpoint IS NOT NULL
        ),
        bounds AS (
            SELECT
                v.market_id,
                MIN(v.timestamp)  AS first_ts,
                MAX(v.timestamp)  AS last_ts,
                MIN(v.midpoint)   AS min_mid,
                MAX(v.midpoint)   AS max_mid
              FROM valid v
             GROUP BY v.market_id
        ),
        first_mid AS (
            SELECT v.market_id, v.midpoint AS first_mid
              FROM valid v
              JOIN bounds b
                ON b.market_id = v.market_id AND b.first_ts = v.timestamp
             GROUP BY v.market_id
        ),
        last_mid AS (
            SELECT v.market_id, v.midpoint AS last_mid
              FROM valid v
              JOIN bounds b
                ON b.market_id = v.market_id AND b.last_ts = v.timestamp
             GROUP BY v.market_id
        ),
        counts AS (
            SELECT market_id, COUNT(*) AS n
              FROM snapshots
             GROUP BY market_id
        )
        SELECT
            c.market_id,
            m.question,
            c.n                AS snapshot_count,
            b.first_ts,
            b.last_ts,
            b.min_mid,
            b.max_mid,
            f.first_mid,
            l.last_mid
          FROM counts c
          JOIN markets m  ON m.id = c.market_id
          JOIN bounds b   ON b.market_id = c.market_id
          JOIN first_mid f ON f.market_id = c.market_id
          JOIN last_mid  l ON l.market_id = c.market_id
         WHERE c.n >= {MIN_SNAPSHOTS}
         ORDER BY ABS(l.last_mid - f.first_mid) DESC
        """
    ).fetchall()
    conn.close()

    total = len(rows)
    moved = [r for r in rows if abs(r[8] - r[7]) >= MOVE_THRESHOLD]
    stable = [r for r in rows if abs(r[8] - r[7]) < MOVE_THRESHOLD]

    print(f"Markets with >= {MIN_SNAPSHOTS} snapshots: {total:,}")
    print(f"  moved >= 5c first->latest:  {len(moved):,}")
    print(f"  stayed within 5c:           {len(stable):,}")
    print()

    print(
        f"{'mid':>10} | {'n':>4} | {'first_ts':<19} {'last_ts':<19} | "
        f"{'min':>6} {'max':>6} | {'first':>6} {'last':>6} {'move':>7}  question"
    )
    print("-" * 140)
    for r in rows:
        mid, q, n, first_ts, last_ts, min_m, max_m, first_m, last_m = r
        delta = last_m - first_m
        moved_flag = "MOVED" if abs(delta) >= MOVE_THRESHOLD else "  -  "
        q_short = (q[:60] + "...") if q and len(q) > 60 else (q or "")
        # Truncate timestamps to seconds
        ft = first_ts[:19] if first_ts else ""
        lt = last_ts[:19] if last_ts else ""
        print(
            f"{mid:>10} | {n:>4} | {ft:<19} {lt:<19} | "
            f"{min_m:>6.3f} {max_m:>6.3f} | "
            f"{first_m:>6.3f} {last_m:>6.3f} {delta:>+7.3f}  {moved_flag}  {q_short}"
        )


if __name__ == "__main__":
    main()
