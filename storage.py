"""
SQLite kalıcılık katmanı: paper trade defteri ve arb fırsat geçmişi.

Bellekte tutulan durum restart'ta kaybolur; bu katman trade'leri ve
tespit edilen arb fırsatlarını kalıcı olarak saklar. Thread-safe (tarayıcı
arka plan thread'i ile API thread'leri aynı bağlantıyı paylaşır).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    event      TEXT NOT NULL,
    market_id  TEXT,
    side       TEXT,
    price      REAL,
    size       REAL,
    status     TEXT,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS order_intents (
    id         TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    market_id  TEXT NOT NULL,
    direction  TEXT NOT NULL,
    detail     TEXT,
    status     TEXT NOT NULL,        -- 'open' | 'done'
    result     TEXT,
    closed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_intent_status ON order_intents(status);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    market_id  TEXT NOT NULL,
    bid        REAL,
    ask        REAL,
    spread     REAL
);

CREATE INDEX IF NOT EXISTS idx_snap_mkt_ts ON market_snapshots(market_id, ts);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS news_signals (
    id            TEXT PRIMARY KEY,      -- NewsItem.id (restart'lar arası dedupe)
    ts            TEXT NOT NULL,         -- arşivlenme zamanı (utcnow)
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    published     TEXT,
    fetched_at    TEXT,
    coins         TEXT,                  -- JSON list
    impact        INTEGER NOT NULL,
    direction     TEXT NOT NULL,
    reason        TEXT,
    scorer        TEXT,
    symbol        TEXT,
    price_24h_pct REAL,
    price_15m_pct REAL,
    volume_usd    REAL,
    confirmed     INTEGER,               -- 0/1
    price_note    TEXT
);

CREATE INDEX IF NOT EXISTS idx_signal_ts ON news_signals(ts);
CREATE INDEX IF NOT EXISTS idx_signal_impact ON news_signals(impact);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    mode           TEXT NOT NULL,         -- simple | grid | walk
    sl             REAL,
    tp             REAL,
    fee            REAL,
    usdt           REAL,
    hours          REAL,
    min_impact     INTEGER,
    n              INTEGER,
    win_rate       REAL,
    avg_net_pct    REAL,
    total_pnl_usdt REAL,
    note           TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_ts ON backtest_runs(ts);

CREATE TABLE IF NOT EXISTS news_closed_trades (
    row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT NOT NULL,
    closed_at   TEXT NOT NULL,
    opened_at   TEXT,
    symbol      TEXT,
    side        TEXT,
    mode        TEXT,
    usdt        REAL,
    entry_price REAL,
    close_price REAL,
    pnl         REAL,
    pnl_pct     REAL,
    close_reason TEXT,
    source      TEXT,
    news_source TEXT,
    impact      INTEGER,
    UNIQUE(trade_id, closed_at)
);

CREATE INDEX IF NOT EXISTS idx_nct_closed ON news_closed_trades(closed_at);

CREATE TABLE IF NOT EXISTS brain_decisions (
    row_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,          -- karar zamanı (utcnow)
    news_id      TEXT,
    source       TEXT,
    title        TEXT,
    symbol       TEXT,
    side         TEXT,
    impact       INTEGER,
    direction    TEXT,
    verdict      TEXT,                   -- enter | wait | veto
    conviction   REAL,
    sl_tightness TEXT,
    hold_minutes INTEGER,
    wait_seconds INTEGER,
    escalated    INTEGER,                -- 0/1
    model        TEXT,
    reason       TEXT,
    scores       TEXT,                   -- JSON
    published    TEXT,                   -- sinyal zamanı (veto-review klines için)
    price_24h_pct REAL,
    price_15m_pct REAL,
    atr_pct      REAL
);

CREATE INDEX IF NOT EXISTS idx_brain_ts ON brain_decisions(ts);
CREATE INDEX IF NOT EXISTS idx_brain_verdict ON brain_decisions(verdict);

CREATE TABLE IF NOT EXISTS shadow_decisions (
    row_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,          -- karar zamanı (utcnow)
    news_id       TEXT,
    symbol        TEXT,
    side          TEXT,
    impact        INTEGER,
    published     TEXT,                   -- sinyal zamanı (sonuç değerlendirmesi için)
    live_trade    INTEGER,                -- canlı ayar girer miydi (0/1)
    shadow_trade  INTEGER,                -- aday ayar girer miydi (0/1)
    live_usdt     REAL,
    shadow_usdt   REAL,
    diverged      INTEGER,                -- 0/1 (kararlar farklı mı)
    overrides     TEXT                    -- aday ayar JSON (hangi senaryo)
);

CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_decisions(ts);

CREATE TABLE IF NOT EXISTS latency_snapshots (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    stage  TEXT NOT NULL,           -- ingest | score | brain | confirm | order | pipeline
    p50    REAL,
    p95    REAL,
    max    REAL,
    count  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_latency_ts ON latency_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_latency_stage_ts ON latency_snapshots(stage, ts);

CREATE TABLE IF NOT EXISTS ops_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,         -- feed_stale | feed_recovered | source_disabled |
                                     -- source_recovered | latency_breach | latency_clear |
                                     -- halt_tripped | halt_cleared
    severity  TEXT NOT NULL,         -- info | warn | critical
    source    TEXT,                  -- ilgili kaynak/aşama (opsiyonel)
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_ts ON ops_events(ts);
CREATE INDEX IF NOT EXISTS idx_ops_kind_ts ON ops_events(kind, ts);
"""

_NCT_COLUMNS = (
    "trade_id", "closed_at", "opened_at", "symbol", "side", "mode", "usdt",
    "entry_price", "close_price", "pnl", "pnl_pct", "close_reason",
    "source", "news_source", "impact",
)

_BACKTEST_COLUMNS = (
    "ts", "mode", "sl", "tp", "fee", "usdt", "hours", "min_impact",
    "n", "win_rate", "avg_net_pct", "total_pnl_usdt", "note",
)

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
_SIGNAL_COLUMNS = (
    "id", "ts", "source", "title", "url", "published", "fetched_at", "coins",
    "impact", "direction", "reason", "scorer", "symbol", "price_24h_pct",
    "price_15m_pct", "volume_usd", "confirmed", "price_note",
)
_BRAIN_COLUMNS = (
    "ts", "news_id", "source", "title", "symbol", "side", "impact", "direction",
    "verdict", "conviction", "sl_tightness", "hold_minutes", "wait_seconds",
    "escalated", "model", "reason", "scores", "published", "price_24h_pct",
    "price_15m_pct", "atr_pct",
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

    def equity_curve(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Kapanan işlemlerden kronolojik kümülatif PnL eğrisi.

        [{closed_at, pnl, cumulative}, ...] — en eskiden en yeniye.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT closed_at, pnl FROM closed_trades "
                "ORDER BY closed_at ASC, id ASC LIMIT ?", (limit,),
            ).fetchall()
        curve: list[dict[str, Any]] = []
        cumulative = 0.0
        for r in rows:
            cumulative += float(r["pnl"])
            curve.append({
                "closed_at": r["closed_at"],
                "pnl": float(r["pnl"]),
                "cumulative": cumulative,
            })
        return curve

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

    # ── Audit log (değişmez emir/olay kaydı) ──────────────────────────────
    def log_event(
        self,
        event: str,
        *,
        market_id: str | None = None,
        side: str | None = None,
        price: float | None = None,
        size: float | None = None,
        status: str | None = None,
        detail: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit_log (ts, event, market_id, side, price, size, "
                "status, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_utcnow(), event, market_id, side, price, size, status, detail),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Emir niyet günlüğü (crash recovery) ───────────────────────────────
    def open_intent(
        self, intent_id: str, market_id: str, direction: str, detail: str | None = None,
    ) -> str:
        with self._lock:
            self._conn.execute(
                "INSERT INTO order_intents (id, ts, market_id, direction, detail, "
                "status) VALUES (?, ?, ?, ?, ?, 'open')",
                (intent_id, _utcnow(), market_id, direction, detail),
            )
            self._conn.commit()
        return intent_id

    def close_intent(self, intent_id: str, result: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE order_intents SET status='done', result=?, closed_at=? "
                "WHERE id=? AND status='open'",
                (result, _utcnow(), intent_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_open_intents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM order_intents WHERE status='open' ORDER BY ts",
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Market snapshot geçmişi (gerçek-veri backtest için) ───────────────
    def record_snapshots(self, ts: str, rows: list[dict[str, Any]]) -> int:
        """Bir taramadaki market satırlarını geçmişe yaz. Eklenen sayıyı döner."""
        data = [
            (ts, str(r.get("id", "")), r.get("bid"), r.get("ask"), r.get("spread"))
            for r in rows if r.get("id")
        ]
        if not data:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO market_snapshots (ts, market_id, bid, ask, spread) "
                "VALUES (?, ?, ?, ?, ?)", data,
            )
            self._conn.commit()
        return len(data)

    def count_snapshots(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM market_snapshots",
            ).fetchone()
        return int(row["c"]) if row else 0

    def history_series(
        self, limit_per_market: int = 1000, markets: list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Backtest için {market_id: [{bid,ask,spread} kronolojik], ...}."""
        query = (
            "SELECT market_id, bid, ask, spread FROM market_snapshots"
            + (" WHERE market_id IN ({})".format(",".join("?" * len(markets)))
               if markets else "")
            + " ORDER BY market_id, ts ASC, id ASC"
        )
        with self._lock:
            rows = self._conn.execute(query, markets or ()).fetchall()
        series: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            series.setdefault(r["market_id"], []).append(
                {"bid": r["bid"], "ask": r["ask"], "spread": r["spread"]},
            )
        # market başına en yeni limit_per_market örneği tut
        if limit_per_market > 0:
            for mid in series:
                series[mid] = series[mid][-limit_per_market:]
        return series

    def prune_snapshots(self, keep: int) -> int:
        """En yeni `keep` snapshot dışındakileri sil. Silinen sayıyı döner."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM market_snapshots WHERE id NOT IN "
                "(SELECT id FROM market_snapshots ORDER BY id DESC LIMIT ?)", (keep,),
            )
            self._conn.commit()
            return cur.rowcount

    def snapshot_span(self) -> dict[str, Any]:
        """Geçmiş verisinin kapsamı: adet, ilk/son zaman, market sayısı."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
                "COUNT(DISTINCT market_id) AS markets FROM market_snapshots",
            ).fetchone()
        return {
            "count": int(row["c"]) if row else 0,
            "first_ts": row["first_ts"] if row else None,
            "last_ts": row["last_ts"] if row else None,
            "markets": int(row["markets"]) if row else 0,
        }

    # ── Haber sinyali arşivi (restart'a dayanıklı backtest verisi) ────────
    def add_signal(self, item: dict[str, Any]) -> bool:
        """Bir haber sinyalini arşivle. id zaten varsa atlanır (dedupe).

        `item` = NewsItem.to_dict() biçimi. Yeni eklendiyse True döner.
        """
        row = {
            "id": item["id"],
            "ts": _utcnow(),
            "source": item.get("source", ""),
            "title": item.get("title", ""),
            "url": item.get("url"),
            "published": item.get("published"),
            "fetched_at": item.get("fetched_at"),
            "coins": json.dumps(item.get("coins") or [], ensure_ascii=False),
            "impact": int(item.get("impact", 0)),
            "direction": item.get("direction", "neutral"),
            "reason": item.get("reason", ""),
            "scorer": item.get("scorer", ""),
            "symbol": item.get("symbol"),
            "price_24h_pct": item.get("price_24h_pct"),
            "price_15m_pct": item.get("price_15m_pct"),
            "volume_usd": item.get("volume_usd"),
            "confirmed": 1 if item.get("confirmed") else 0,
            "price_note": item.get("price_note", ""),
        }
        cols = ", ".join(_SIGNAL_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _SIGNAL_COLUMNS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO news_signals ({cols}) VALUES ({placeholders})",
                row,
            )
            self._conn.commit()
            return cur.rowcount > 0

    def _decode_signal(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["coins"] = json.loads(d["coins"]) if d.get("coins") else []
        except (ValueError, TypeError):
            d["coins"] = []
        d["confirmed"] = bool(d.get("confirmed"))
        return d

    def list_signals(
        self, limit: int = 500, min_impact: int = 0,
    ) -> list[dict[str, Any]]:
        """Arşivlenmiş sinyaller, en yeniden eskiye. coins listeye çözülür."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM news_signals WHERE impact >= ? "
                "ORDER BY ts DESC, id DESC LIMIT ?", (min_impact, limit),
            ).fetchall()
        return [self._decode_signal(r) for r in rows]

    def signal_span(self) -> dict[str, Any]:
        """Sinyal arşivinin kapsamı: adet, ilk/son zaman."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
                "FROM news_signals",
            ).fetchone()
        return {
            "count": int(row["c"]) if row else 0,
            "first_ts": row["first_ts"] if row else None,
            "last_ts": row["last_ts"] if row else None,
        }

    def prune_signals(self, keep: int) -> int:
        """En yeni `keep` sinyal dışındakileri sil (sınırsız büyümeyi önler).

        Silinen satır sayısını döndürür. keep <= 0 ise hiçbir şey yapmaz.
        """
        if keep <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM news_signals WHERE id NOT IN "
                "(SELECT id FROM news_signals ORDER BY ts DESC, id DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()
            return cur.rowcount

    # ── Backtest çalıştırma geçmişi (karşılaştırma için) ──────────────────
    def add_backtest_run(self, run: dict[str, Any]) -> int:
        """Bir backtest özetini kaydet. Eklenen satır id'sini döner."""
        row = {c: run.get(c) for c in _BACKTEST_COLUMNS}
        row["ts"] = run.get("ts") or _utcnow()
        cols = ", ".join(_BACKTEST_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _BACKTEST_COLUMNS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO backtest_runs ({cols}) VALUES ({placeholders})", row,
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_backtest_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Backtest çalıştırmaları, en yeniden eskiye (karşılaştırma)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Gecikme anlık görüntüleri (kalıcı trend; restart'a dayanıklı) ─────────
    def add_latency_snapshot(self, stages: dict[str, dict[str, Any]],
                             ts: str | None = None) -> int:
        """Bir gecikme özetini (aşama başına p50/p95/max/count) kalıcı yaz.

        `stages`: `latency.summary()` çıktısı {stage: {p50_ms, p95_ms, max_ms, count}}.
        Aşama başına bir satır ekler. Yazılan satır sayısını döner. Boş özet → 0.
        """
        t = ts or _utcnow()
        rows = [
            {"ts": t, "stage": stage, "p50": st.get("p50_ms"), "p95": st.get("p95_ms"),
             "max": st.get("max_ms"), "count": st.get("count")}
            for stage, st in stages.items() if st.get("count")
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO latency_snapshots (ts, stage, p50, p95, max, count) "
                "VALUES (:ts, :stage, :p50, :p95, :max, :count)", rows,
            )
            self._conn.commit()
        return len(rows)

    def latency_history(self, hours: float = 24.0, stage: str | None = None,
                        limit: int = 5000) -> list[dict[str, Any]]:
        """Gecikme anlık görüntüleri, eskiden yeniye (trend). `stage` ile filtrele.

        `hours`: bu kadar saat geriye. Zaman serisi grafiği için ts-artan döner.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        q = "SELECT ts, stage, p50, p95, max, count FROM latency_snapshots WHERE ts >= ?"
        params: list[Any] = [cutoff]
        if stage:
            q += " AND stage = ?"
            params.append(stage)
        q += " ORDER BY ts ASC, id ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def latency_span(self) -> dict[str, Any]:
        """Gecikme arşivi kapsamı: satır sayısı + ilk/son zaman (gözlemlenebilirlik)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
                "FROM latency_snapshots",
            ).fetchone()
        return {"count": row["n"], "first_ts": row["first_ts"], "last_ts": row["last_ts"]}

    def prune_latency_snapshots(self, keep: int) -> int:
        """En yeni `keep` satır dışındakileri sil (sınırsız büyümeyi önler). keep<=0 → no-op."""
        if keep <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM latency_snapshots WHERE id NOT IN "
                "(SELECT id FROM latency_snapshots ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()
            return cur.rowcount

    # ── Operasyonel olay zaman çizelgesi (incident günlüğü; post-mortem) ─────
    def add_ops_event(self, kind: str, severity: str, detail: str = "",
                      source: str = "", ts: str | None = None) -> int:
        """Bir operasyonel olayı (incident) kalıcı yaz. Eklenen satır id'sini döner."""
        row = {"ts": ts or _utcnow(), "kind": kind, "severity": severity,
               "source": source, "detail": detail[:300]}
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO ops_events (ts, kind, severity, source, detail) "
                "VALUES (:ts, :kind, :severity, :source, :detail)", row,
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_ops_events(self, limit: int = 200, kind: str | None = None,
                        severity: str | None = None, hours: float | None = None
                        ) -> list[dict[str, Any]]:
        """Operasyonel olaylar, en yeniden eskiye. kind/severity/hours ile filtrele."""
        q = "SELECT ts, kind, severity, source, detail FROM ops_events WHERE 1=1"
        params: list[Any] = []
        if kind:
            q += " AND kind = ?"
            params.append(kind)
        if severity:
            q += " AND severity = ?"
            params.append(severity)
        if hours is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            q += " AND ts >= ?"
            params.append(cutoff)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def ops_event_span(self) -> dict[str, Any]:
        """Olay arşivi kapsamı: toplam + son 24s severity sayımı (gözlemlenebilirlik)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM ops_events").fetchone()["n"]
            rows = self._conn.execute(
                "SELECT severity, COUNT(*) AS n FROM ops_events WHERE ts >= ? GROUP BY severity",
                (cutoff,),
            ).fetchall()
        return {"count": total, "last24h": {r["severity"]: r["n"] for r in rows}}

    def prune_ops_events(self, keep: int) -> int:
        """En yeni `keep` olay dışındakileri sil (sınırsız büyümeyi önler). keep<=0 → no-op."""
        if keep <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM ops_events WHERE id NOT IN "
                "(SELECT id FROM ops_events ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()
            return cur.rowcount

    # ── Shadow-mode (A/B): canlı vs aday ayar karar günlüğü ──────────────────
    def add_shadow_decision(self, row: dict[str, Any]) -> int:
        """Bir gölge karar kaydını ekle (canlı vs aday ayar). Eklenen satır id'si döner."""
        rec = {
            "ts": row.get("ts") or _utcnow(),
            "news_id": row.get("news_id"), "symbol": row.get("symbol"),
            "side": row.get("side"), "impact": row.get("impact"),
            "published": row.get("published"),
            "live_trade": 1 if row.get("live_trade") else 0,
            "shadow_trade": 1 if row.get("shadow_trade") else 0,
            "live_usdt": row.get("live_usdt"), "shadow_usdt": row.get("shadow_usdt"),
            "diverged": 1 if row.get("diverged") else 0,
            "overrides": row.get("overrides"),
        }
        cols = ", ".join(rec)
        ph = ", ".join(f":{c}" for c in rec)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO shadow_decisions ({cols}) VALUES ({ph})", rec)
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def shadow_summary(self, limit: int = 1000) -> dict[str, Any]:
        """Gölge karar özeti: kaç karar, kaç divergence, canlı vs aday giriş sayısı."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shadow_decisions ORDER BY row_id DESC LIMIT ?", (limit,),
            ).fetchall()
        rows = [dict(r) for r in rows]
        n = len(rows)
        return {
            "n": n,
            "diverged": sum(r["diverged"] for r in rows),
            "live_trades": sum(r["live_trade"] for r in rows),
            "shadow_trades": sum(r["shadow_trade"] for r in rows),
            "recent": rows[:50],
        }

    # ── Kapanan haber-işlem defteri (kalıcı; trade_state.json 500 sınırı dışı) ──
    def add_closed_news_trade(self, trade: dict[str, Any]) -> bool:
        """Kapanan bir işlemi arşivle. (trade_id, closed_at) zaten varsa atlar.

        Kısmi + tam kapanışlar farklı closed_at taşıdığı için ikisi de saklanır.
        Yeni eklendiyse True döner.
        """
        row = {
            "trade_id": trade.get("id"),
            "closed_at": trade.get("closed_at"),
            "opened_at": trade.get("opened_at"),
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "mode": trade.get("mode"),
            "usdt": trade.get("usdt"),
            "entry_price": trade.get("entry_price"),
            "close_price": trade.get("close_price"),
            "pnl": trade.get("pnl"),
            "pnl_pct": trade.get("pnl_pct"),
            "close_reason": trade.get("close_reason"),
            "source": trade.get("source"),
            "news_source": trade.get("news_source"),
            "impact": trade.get("impact"),
        }
        if not row["trade_id"] or not row["closed_at"]:
            return False
        cols = ", ".join(_NCT_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _NCT_COLUMNS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO news_closed_trades ({cols}) VALUES ({placeholders})",
                row,
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_closed_news_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        """Arşivdeki kapanan işlemler, en yeniden eskiye. `id` alanı geri eklenir."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM news_closed_trades ORDER BY closed_at DESC, row_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["id"] = d.pop("trade_id")
            d.pop("row_id", None)
            out.append(d)
        return out

    # ── Giriş beyni karar günlüğü ────────────────────────────────────────
    def add_brain_decision(self, d: dict[str, Any]) -> None:
        """Bir giriş beyni kararını (gir/bekle/veto) kalıcı günlüğe yaz. Hata yutmaz çağıran."""
        row = {c: d.get(c) for c in _BRAIN_COLUMNS}
        row["ts"] = _utcnow()
        row["escalated"] = 1 if d.get("escalated") else 0
        if isinstance(row.get("scores"), (dict, list)):
            row["scores"] = json.dumps(row["scores"], ensure_ascii=False)
        cols = ", ".join(_BRAIN_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _BRAIN_COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO brain_decisions ({cols}) VALUES ({placeholders})", row)
            self._conn.commit()

    def list_brain_decisions(self, limit: int = 200, verdict: str | None = None) -> list[dict[str, Any]]:
        """Beyin kararları, en yeniden eskiye. `verdict` ile filtrele (enter/wait/veto)."""
        q = "SELECT * FROM brain_decisions"
        params: list[Any] = []
        if verdict:
            q += " WHERE verdict = ?"
            params.append(verdict)
        q += " ORDER BY row_id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("row_id", None)
            d["escalated"] = bool(d.get("escalated"))
            try:
                d["scores"] = json.loads(d["scores"]) if d.get("scores") else {}
            except (ValueError, TypeError):
                d["scores"] = {}
            out.append(d)
        return out

    def count_closed_news_trades(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM news_closed_trades",
            ).fetchone()
        return int(row["c"]) if row else 0

    # ── Uygulama ayarları (kalıcı; restart'a dayanıklı) ───────────────────
    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value),
            )
            self._conn.commit()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,),
            ).fetchone()
        return row["value"] if row else default
