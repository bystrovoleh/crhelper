"""
Intraday examples database.
Separate SQLite DB from swing examples — isolated, same schema pattern.
"""

import sqlite3
import json
from pathlib import Path
from config.settings import INTRADAY_DB_PATH


def _conn() -> sqlite3.Connection:
    INTRADAY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INTRADAY_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create intraday_examples table if it doesn't exist."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intraday_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('long', 'short')),
                entry1 REAL NOT NULL,
                entry2 REAL,
                sl REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL,
                trade_datetime TEXT NOT NULL,       -- ISO datetime (includes time, unlike swing 'trade_date')
                session TEXT,                       -- asia | europe | us | overlap_eu_us
                outcome TEXT CHECK(outcome IN ('tp1_hit', 'tp2_hit', 'sl_hit', 'open')),
                notes TEXT,
                market_snapshot TEXT,               -- JSON: full intraday snapshot
                indicators TEXT,                    -- JSON: computed intraday indicators
                source TEXT NOT NULL DEFAULT 'manual',  -- manual | auto
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def insert_example(data: dict) -> int:
    """Insert a new intraday example. Returns new row id."""
    init_db()
    with _conn() as conn:
        cursor = conn.execute("""
            INSERT INTO intraday_examples
              (asset, direction, entry1, entry2, sl, tp1, tp2,
               trade_datetime, session, outcome, notes,
               market_snapshot, indicators, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["asset"],
            data["direction"],
            data["entry1"],
            data.get("entry2"),
            data["sl"],
            data["tp1"],
            data.get("tp2"),
            data["trade_datetime"],
            data.get("session"),
            data.get("outcome"),
            data.get("notes"),
            json.dumps(data.get("market_snapshot")) if data.get("market_snapshot") else None,
            json.dumps(data.get("indicators")) if data.get("indicators") else None,
            data.get("source", "manual"),
        ))
        conn.commit()
        return cursor.lastrowid


def update_outcome(example_id: int, outcome: str):
    """Update outcome for a given example: tp1_hit | tp2_hit | sl_hit | open."""
    with _conn() as conn:
        conn.execute(
            "UPDATE intraday_examples SET outcome = ? WHERE id = ?",
            (outcome, example_id)
        )
        conn.commit()


def update_example_context(example_id: int, snapshot: dict, indicators: dict):
    """Attach market snapshot and indicators to an example (after fetching historical data)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE intraday_examples SET market_snapshot = ?, indicators = ? WHERE id = ?",
            (json.dumps(snapshot), json.dumps(indicators), example_id)
        )
        conn.commit()


def get_all_examples(asset: str = None, source: str = None) -> list[dict]:
    """
    Fetch all examples, optionally filtered by asset and/or source.
    source: 'manual' | 'auto' | None (all)
    """
    init_db()
    with _conn() as conn:
        query = "SELECT * FROM intraday_examples WHERE 1=1"
        params = []
        if asset:
            query += " AND asset = ?"
            params.append(asset.upper())
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY trade_datetime DESC"
        rows = conn.execute(query, params).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        for field in ("market_snapshot", "indicators"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        result.append(d)
    return result


def get_example_by_id(example_id: int) -> dict | None:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM intraday_examples WHERE id = ?", (example_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for field in ("market_snapshot", "indicators"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    return d


def delete_example(example_id: int):
    with _conn() as conn:
        conn.execute("DELETE FROM intraday_examples WHERE id = ?", (example_id,))
        conn.commit()


def get_examples_count(asset: str = None) -> int:
    init_db()
    with _conn() as conn:
        query = "SELECT COUNT(*) FROM intraday_examples"
        params = []
        if asset:
            query += " WHERE asset = ?"
            params.append(asset.upper())
        return conn.execute(query, params).fetchone()[0]
