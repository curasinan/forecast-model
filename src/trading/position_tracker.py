"""Position tracker — persistent portfolio state in SQLite.

Tracks:
- Open positions (question, side, entry price, size, current value)
- Realized P&L (closed trades)
- Unrealized P&L (open positions vs current market)
- Trade history log
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from src.trading.order_manager import TradeOrder, OrderStatus

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """An open position in a market."""

    question_id: str
    outcome: str  # "YES" or "NO"
    entry_price: float
    size: float  # Number of tokens
    cost_basis: float  # Total $ spent
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    opened_at: str = ""


@dataclass
class TradeRecord:
    """Historical record of a completed trade."""

    order_id: str
    question_id: str
    outcome: str
    side: str
    price: float
    size: float
    cost_usd: float
    status: str
    reason: str
    created_at: str
    filled_at: str


class PositionTracker:
    """Persistent position and P&L tracking via SQLite."""

    def __init__(self, db_path: str = "data/predictions.db"):
        self._db_path = db_path
        self._init_tables()

    def _init_tables(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    question_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    size REAL NOT NULL,
                    cost_basis REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    exit_price REAL,
                    realized_pnl REAL,
                    PRIMARY KEY (question_id, outcome, opened_at)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_log (
                    order_id TEXT PRIMARY KEY,
                    question_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    cost_usd REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    filled_at TEXT
                )
            """)
            # Venue-neutral columns added by the IBKR migration (2026-08-23);
            # ALTER-in-place so an existing predictions.db keeps its history.
            self._ensure_columns(conn, "trade_log", {
                "market_id": "TEXT", "venue": "TEXT", "venue_order_id": "TEXT",
                "filled_size": "REAL",
            })
            self._ensure_columns(conn, "positions", {"market_id": "TEXT", "venue": "TEXT", "order_id": "TEXT"})

    @staticmethod
    def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ctype in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}")

    def record_trade(self, order: TradeOrder) -> None:
        """Record a filled/simulated trade."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO trade_log
                   (order_id, question_id, outcome, side, price, size,
                    cost_usd, status, reason, created_at, filled_at,
                    market_id, venue, venue_order_id, filled_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.order_id,
                    order.question_id,
                    order.outcome,
                    order.side.value,
                    order.price,
                    order.size,
                    order.cost_usd,
                    order.status.value,
                    order.reason,
                    order.created_at.isoformat(),
                    order.filled_at.isoformat() if order.filled_at else None,
                    getattr(order, "market_id", "") or "",
                    getattr(order, "venue", "") or "",
                    getattr(order, "venue_order_id", "") or "",
                    float(getattr(order, "filled_size", 0.0) or 0.0),
                ),
            )

        # If filled, open a position
        if order.status in (OrderStatus.FILLED, OrderStatus.SIMULATED):
            self._open_position(order)

    def _open_position(self, order: TradeOrder) -> None:
        """Record a new open position (idempotent per order_id)."""
        with sqlite3.connect(self._db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM positions WHERE order_id = ? LIMIT 1", (order.order_id,)
            ).fetchone()
            if exists:
                return
            conn.execute(
                """INSERT INTO positions
                   (question_id, outcome, entry_price, size, cost_basis, opened_at,
                    market_id, venue, order_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.question_id,
                    order.outcome,
                    order.price,
                    order.size,
                    order.cost_usd,
                    datetime.now().isoformat(),
                    getattr(order, "market_id", "") or "",
                    getattr(order, "venue", "") or "",
                    order.order_id,
                ),
            )

    def close_position(
        self,
        question_id: str,
        outcome: str,
        resolution: int,
    ) -> float:
        """Close a position when market resolves.

        Args:
            question_id: Which market resolved
            outcome: "YES" or "NO" (what we held)
            resolution: 1 if YES won, 0 if NO won

        Returns:
            Realized P&L for this position.
        """
        positions = self.get_open_positions(question_id)
        total_pnl = 0.0

        for pos in positions:
            if pos.outcome != outcome:
                continue

            # If we held YES and YES won: payout = size * $1
            # If we held YES and NO won: payout = 0
            if (pos.outcome == "YES" and resolution == 1) or \
               (pos.outcome == "NO" and resolution == 0):
                payout = pos.size * 1.0  # Each winning token pays $1
            else:
                payout = 0.0

            pnl = payout - pos.cost_basis
            total_pnl += pnl

            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """UPDATE positions
                       SET closed_at = ?, exit_price = ?, realized_pnl = ?
                       WHERE question_id = ? AND outcome = ? AND opened_at = ?""",
                    (
                        datetime.now().isoformat(),
                        1.0 if payout > 0 else 0.0,
                        pnl,
                        question_id,
                        outcome,
                        pos.opened_at,
                    ),
                )

        return total_pnl

    def get_open_positions(self, question_id: str | None = None) -> list[Position]:
        """Get all open (unclosed) positions."""
        query = "SELECT * FROM positions WHERE closed_at IS NULL"
        params: list = []
        if question_id:
            query += " AND question_id = ?"
            params.append(question_id)

        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            Position(
                question_id=row[0],
                outcome=row[1],
                entry_price=row[2],
                size=row[3],
                cost_basis=row[4],
                opened_at=row[5],
            )
            for row in rows
        ]

    def get_trade_history(self, limit: int = 50) -> list[TradeRecord]:
        """Get recent trade history."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM trade_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [
            TradeRecord(
                order_id=row[0],
                question_id=row[1],
                outcome=row[2],
                side=row[3],
                price=row[4],
                size=row[5],
                cost_usd=row[6],
                status=row[7],
                reason=row[8] or "",
                created_at=row[9],
                filled_at=row[10] or "",
            )
            for row in rows
        ]

    def get_portfolio_summary(self) -> dict:
        """Overall portfolio statistics."""
        open_positions = self.get_open_positions()
        total_cost = sum(p.cost_basis for p in open_positions)

        with sqlite3.connect(self._db_path) as conn:
            # Realized P&L from closed positions
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
            realized_pnl = row[0] if row else 0.0

            # Total trades
            row = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()
            total_trades = row[0] if row else 0

        return {
            "open_positions": len(open_positions),
            "total_exposure_usd": round(total_cost, 2),
            "realized_pnl": round(realized_pnl, 2),
            "total_trades": total_trades,
        }
