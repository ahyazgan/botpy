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
from datetime import datetime, timezone
from typing import Any

DEFAULT_DB_PATH = os.environ.get("BOTPY_DB", "botpy.db")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def closed_row(trade: dict[str, Any], close_price: float, reason: str) -> dict[str, Any]:
    """Açık trade'i kapanan-işlem satırına çevir (realize PnL ile). Saf fonksiyon."""
    pnl = trade["shares"] * close_price - trade["amount_usdc"]
    return {
        "id": trade["id"],
        "market_id": trade["market_id"],
        "question": trade["question"],
        "side": trade["side"],
        "amount_usdc": trade["amount_usdc"],
        "entry_price": trade["entry_price"],
        "shares": trade["shares"],
        "opened_at": trade["opened_at"],
        "closed_at": _utcnow(),
        "close_price": close_price,
        "pnl": pnl,
        "reason": reason,
    }

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

CREATE TABLE IF NOT EXISTS closed_trades (
    id          TEXT PRIMARY KEY,
    market_id   TEXT NOT NULL,
    question    TEXT NOT NULL,
    side        TEXT NOT NULL,
    amount_usdc REAL NOT NULL,
    entry_price REAL NOT NULL,
    shares      REAL NOT NULL,
    opened_at   TEXT NOT NULL,
    closed_at   TEXT NOT NULL,
    close_price REAL NOT NULL,
    pnl         REAL NOT NULL,
    reason      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_closed_at ON closed_trades(closed_at);
"""

_TRADE_COLUMNS = (
    "id", "market_id", "question", "side",
    "amount_usdc", "entry_price", "shares", "opened_at",
)
_OPP_COLUMNS = (
    "ts", "market_id", "question", "direction",
    "profit_pct", "yes_price", "no_price",
)
_CLOSED_COLUMNS = (
    "id", "market_id", "question", "side", "amount_usdc", "entry_price",
    "shares", "opened_at", "closed_at", "close_price", "pnl", "reason",
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

    # ── Kapanan işlemler (realize PnL geçmişi) ────────────────────────────
    def close_trade(
        self, trade_id: str, close_price: float, reason: str,
    ) -> dict[str, Any] | None:
        """Açık pozisyonu atomik kapat: kapanan deftere yaz + açıktan kaldır.

        Bulunamazsa None döner. Kapanan satırı (realize PnL ile) döner.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM paper_trades WHERE id = ?", (trade_id,),
            ).fetchone()
            if row is None:
                return None
            closed = closed_row(dict(row), close_price, reason)
            cols = ", ".join(_CLOSED_COLUMNS)
            placeholders = ", ".join(f":{c}" for c in _CLOSED_COLUMNS)
            self._conn.execute(
                f"INSERT INTO closed_trades ({cols}) VALUES ({placeholders})",
                {k: closed[k] for k in _CLOSED_COLUMNS},
            )
            self._conn.execute("DELETE FROM paper_trades WHERE id = ?", (trade_id,))
            self._conn.commit()
            return closed

    def add_closed_trade(self, trade: dict[str, Any]) -> None:
        row = {k: trade[k] for k in _CLOSED_COLUMNS}
        cols = ", ".join(_CLOSED_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _CLOSED_COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO closed_trades ({cols}) VALUES ({placeholders})", row,
            )
            self._conn.commit()

    def list_closed_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM closed_trades ORDER BY closed_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def realized_pnl_total(self) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM closed_trades",
            ).fetchone()
        return float(row["total"]) if row else 0.0

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
