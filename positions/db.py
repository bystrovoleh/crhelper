import sqlite3
from pathlib import Path
from config.settings import DB_PATH

POSITIONS_DB_PATH = DB_PATH.parent.parent / "positions" / "positions.db"


def get_connection() -> sqlite3.Connection:
    POSITIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(POSITIONS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL CHECK(direction IN ('long', 'short')),
                size_usd    REAL    NOT NULL,
                leverage    INTEGER NOT NULL DEFAULT 10,
                entry_price REAL    NOT NULL,
                sl_price    REAL    NOT NULL,
                tp1_price   REAL    NOT NULL,
                tp2_price   REAL,
                status      TEXT    NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
                close_price REAL,
                close_reason TEXT,
                pnl_usd     REAL,
                pnl_percent REAL,
                opened_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                closed_at   TEXT
            )
        """)
        conn.commit()


def insert_position(data: dict) -> int:
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO positions (
                symbol, direction, size_usd, leverage,
                entry_price, sl_price, tp1_price, tp2_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["symbol"].upper(),
            data["direction"].lower(),
            float(data["size_usd"]),
            int(data.get("leverage", 10)),
            float(data["entry_price"]),
            float(data["sl_price"]),
            float(data["tp1_price"]),
            float(data["tp2_price"]) if data.get("tp2_price") else None,
        ))
        conn.commit()
        return cursor.lastrowid


def close_position(position_id: int, close_price: float, close_reason: str = None) -> dict | None:
    pos = get_position_by_id(position_id)
    if not pos or pos["status"] == "closed":
        return None

    exposure = pos["size_usd"] * pos["leverage"]
    price_change_pct = (close_price - pos["entry_price"]) / pos["entry_price"]
    if pos["direction"] == "short":
        price_change_pct = -price_change_pct

    pnl_usd = round(exposure * price_change_pct, 2)
    pnl_percent = round(price_change_pct * 100, 2)

    with get_connection() as conn:
        conn.execute("""
            UPDATE positions
            SET status = 'closed',
                close_price = ?,
                close_reason = ?,
                pnl_usd = ?,
                pnl_percent = ?,
                closed_at = datetime('now')
            WHERE id = ?
        """, (close_price, close_reason, pnl_usd, pnl_percent, position_id))
        conn.commit()

    return get_position_by_id(position_id)


def get_open_positions() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_positions() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY opened_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_position_by_id(position_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
    return dict(row) if row else None
