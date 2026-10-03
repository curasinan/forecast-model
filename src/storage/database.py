"""SQLite storage layer for questions, evidence, predictions, and market snapshots.

Zero configuration, ships with Python. SQL for analysis.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from src.models.evidence import EvidenceBundle
from src.models.prediction import PredictionRecord
from src.models.question import MarketData, Question, QuestionStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    deadline TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    tags TEXT DEFAULT '[]',
    search_keywords TEXT DEFAULT '[]',
    created_at TEXT,
    resolution_date TEXT,
    resolution_source TEXT,
    market_id TEXT DEFAULT '',
    market_slug TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id TEXT REFERENCES questions(id),
    source_type TEXT,
    source_name TEXT,
    title TEXT,
    url TEXT,
    content_snippet TEXT,
    published_at TEXT,
    collected_at TEXT,
    relevance_score REAL DEFAULT 0.0,
    sentiment_score REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id TEXT REFERENCES questions(id),
    predictor_name TEXT,
    probability REAL,
    confidence REAL,
    reasoning TEXT,
    evidence_count INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS edge_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id TEXT REFERENCES questions(id),
    model_prob REAL,
    market_prob REAL,
    edge_pct REAL,
    expected_value REAL,
    kelly_fraction REAL,
    confidence REAL,
    vpin_flag INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id TEXT REFERENCES questions(id),
    yes_price REAL,
    no_price REAL,
    volume_24h REAL,
    total_volume REAL,
    liquidity REAL,
    snapshot_at TEXT
);
"""


class Database:
    def __init__(self, db_path: str = "data/predictions.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(SCHEMA)
        conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- Questions ---

    def store_question(self, q: Question) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO questions
               (id, title, description, deadline, status, tags, search_keywords,
                created_at, resolution_date, resolution_source, market_id, market_slug)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                q.id,
                q.title,
                q.description,
                q.deadline.isoformat(),
                q.status.value,
                json.dumps(q.tags),
                json.dumps(q.search_keywords),
                q.created_at.isoformat(),
                q.resolution_date.isoformat() if q.resolution_date else None,
                q.resolution_source,
                q.market.market_id,
                q.market.slug,
            ),
        )
        conn.commit()

    def get_question(self, question_id: str) -> Question | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
        if not row:
            return None
        return self._row_to_question(row)

    def get_open_questions(self) -> list[Question]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM questions WHERE status = 'open'").fetchall()
        return [self._row_to_question(r) for r in rows]

    def _row_to_question(self, row: sqlite3.Row) -> Question:
        return Question(
            id=row["id"],
            title=row["title"],
            description=row["description"] or "",
            deadline=datetime.fromisoformat(row["deadline"]),
            status=QuestionStatus(row["status"]),
            tags=json.loads(row["tags"]) if row["tags"] else [],
            search_keywords=json.loads(row["search_keywords"]) if row["search_keywords"] else [],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
            market=MarketData(
                market_id=row["market_id"] or "",
                slug=row["market_slug"] or "",
            ),
        )

    # --- Evidence ---

    def store_evidence(self, bundle: EvidenceBundle) -> None:
        conn = self._get_conn()
        for item in bundle.items:
            conn.execute(
                """INSERT INTO evidence_items
                   (question_id, source_type, source_name, title, url,
                    content_snippet, published_at, collected_at,
                    relevance_score, sentiment_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    bundle.question_id,
                    item.source_type,
                    item.source_name,
                    item.title,
                    item.url,
                    item.content_snippet,
                    item.published_at.isoformat() if item.published_at else None,
                    item.collected_at.isoformat(),
                    item.relevance_score,
                    item.sentiment_score,
                ),
            )
        conn.commit()

    # --- Predictions ---

    def store_prediction(self, record: PredictionRecord) -> None:
        conn = self._get_conn()
        for pred in record.predictions:
            conn.execute(
                """INSERT INTO predictions
                   (question_id, predictor_name, probability, confidence,
                    reasoning, evidence_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    pred.question_id,
                    pred.predictor_name,
                    pred.probability,
                    pred.confidence,
                    pred.reasoning,
                    pred.evidence_count,
                    pred.created_at.isoformat(),
                ),
            )

        if record.edge:
            e = record.edge
            conn.execute(
                """INSERT INTO edge_signals
                   (question_id, model_prob, market_prob, edge_pct,
                    expected_value, kelly_fraction, confidence, vpin_flag, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    e.question_id,
                    e.model_prob,
                    e.market_prob,
                    e.edge_pct,
                    e.expected_value,
                    e.kelly_fraction,
                    e.confidence,
                    1 if e.vpin_flag else 0,
                    e.created_at.isoformat(),
                ),
            )
        conn.commit()

    # --- Market Snapshots ---

    def store_market_snapshot(self, question_id: str, market: MarketData) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO market_snapshots
               (question_id, yes_price, no_price, volume_24h,
                total_volume, liquidity, snapshot_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                question_id,
                market.yes_price,
                market.no_price,
                market.volume_24h,
                market.total_volume,
                market.liquidity,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()

    def get_prediction_history(self, question_id: str) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT predictor_name, probability, confidence, reasoning, created_at
               FROM predictions WHERE question_id = ? ORDER BY created_at""",
            (question_id,),
        ).fetchall()
        return [dict(r) for r in rows]
