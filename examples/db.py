import sqlite3
import json
from pathlib import Path
from config.settings import DB_PATH


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('long', 'short')),
                entry1 REAL NOT NULL,
                entry2 REAL,
                sl REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL,
                trade_date TEXT NOT NULL,
                outcome TEXT CHECK(outcome IN ('tp1_hit', 'tp2_hit', 'sl_hit', 'open', NULL)),
                notes TEXT,
                liquidity_levels TEXT,
                market_snapshot TEXT,
                indicators TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                market_phase TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Migrate existing DB: add columns if they don't exist yet
        for col, definition in [
            ("source", "TEXT NOT NULL DEFAULT 'manual'"),
            ("market_phase", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE examples ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
        conn.commit()


def insert_example(data: dict) -> int:
    """Insert a new example and return its id."""
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO examples (
                asset, direction, entry1, entry2, sl, tp1, tp2,
                trade_date, outcome, notes,
                liquidity_levels, market_snapshot, indicators,
                source, market_phase
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["asset"].upper(),
            data["direction"].lower(),
            float(data["entry1"]),
            float(data["entry2"]) if data.get("entry2") else None,
            float(data["sl"]),
            float(data["tp1"]),
            float(data["tp2"]) if data.get("tp2") else None,
            data["trade_date"],
            data.get("outcome") or ("tp2_hit" if data.get("tp2") else "tp1_hit"),
            data.get("notes"),
            json.dumps(data.get("liquidity_levels") or []),
            json.dumps(data.get("market_snapshot") or {}),
            json.dumps(data.get("indicators") or {}),
            data.get("source", "manual"),
            data.get("market_phase"),
        ))
        conn.commit()
        return cursor.lastrowid


def update_example_context(example_id: int, market_snapshot: dict, indicators: dict):
    """Update market snapshot and indicators for an existing example."""
    with get_connection() as conn:
        conn.execute("""
            UPDATE examples
            SET market_snapshot = ?, indicators = ?
            WHERE id = ?
        """, (json.dumps(market_snapshot), json.dumps(indicators), example_id))
        conn.commit()


def update_outcome(example_id: int, outcome: str):
    """Update the outcome of an example."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE examples SET outcome = ? WHERE id = ?",
            (outcome, example_id)
        )
        conn.commit()


def get_all_examples(asset: str = None) -> list[dict]:
    """Fetch all examples, optionally filtered by asset."""
    with get_connection() as conn:
        if asset:
            rows = conn.execute(
                "SELECT * FROM examples WHERE asset = ? ORDER BY trade_date DESC",
                (asset.upper(),)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM examples ORDER BY trade_date DESC"
            ).fetchall()

    return [_row_to_dict(r) for r in rows]


def get_example_by_id(example_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM examples WHERE id = ?", (example_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def delete_example(example_id: int) -> bool:
    """Delete an example by id. Returns True if deleted, False if not found."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM examples WHERE id = ?", (example_id,))
        conn.commit()
        return cursor.rowcount > 0


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in ("liquidity_levels", "market_snapshot", "indicators"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    return d
