"""
SQLite kalıcılık katmanı: paper trade defteri ve arb fırsat geçmişi.

Bellekte tutulan durum restart'ta kaybolur; bu katman trade'leri ve
tespit edilen arb fırsatlarını kalıcı olarak saklar. Thread-safe (tarayıcı
arka plan thread'i ile API thread'leri aynı bağlantıyı paylaşır).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

DEFAULT_DB_PATH = os.environ.get("BOTPY_DB", "botpy.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id          TEXT PRIMARY KEY,
    market_id   TEXT NOT NULL,
    question    TEXT NOT NULL,
    side        TEXT NOT NULL,
    amount_usdc REAL NOT NULL,
    entry_price REAL NOT NULL,
    shares      REAL NOT NULL,
    opened_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arb_opportunities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    question    TEXT NOT NULL,
    direction   TEXT NOT NULL,
    profit_pct  REAL NOT NULL,
    yes_price   REAL NOT NULL,
    no_price    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_arb_ts ON arb_opportunities(ts);
"""

_TRADE_COLUMNS = (
    "id", "market_id", "question", "side",
    "amount_usdc", "entry_price", "shares", "opened_at",
)
_OPP_COLUMNS = (
    "ts", "market_id", "question", "direction",
    "profit_pct", "yes_price", "no_price",
)


class Store:
    """SQLite tabanlı kalıcı depo. Tüm erişimler tek kilit altında serialize."""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Paper trades ──────────────────────────────────────────────────────
    def add_trade(self, trade: dict[str, Any]) -> None:
        row = {k: trade[k] for k in _TRADE_COLUMNS}
        cols = ", ".join(_TRADE_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _TRADE_COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO paper_trades ({cols}) VALUES ({placeholders})", row,
            )
            self._conn.commit()

    def list_trades(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_trades ORDER BY opened_at",
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_trade(self, trade_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM paper_trades WHERE id = ?", (trade_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── Arb fırsat geçmişi ────────────────────────────────────────────────
    def record_opportunity(self, opp: dict[str, Any]) -> int:
        row = {k: opp[k] for k in _OPP_COLUMNS}
        cols = ", ".join(_OPP_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _OPP_COLUMNS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO arb_opportunities ({cols}) VALUES ({placeholders})", row,
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_opportunities(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM arb_opportunities ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
