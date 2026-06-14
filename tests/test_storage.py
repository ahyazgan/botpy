"""storage.Store SQLite kalıcılık testleri."""

from __future__ import annotations

import pytest

from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def _trade(tid: str = "t1") -> dict:
    return {
        "id": tid, "market_id": "m1", "question": "Test?", "side": "YES",
        "amount_usdc": 10.0, "entry_price": 0.45, "shares": 22.22,
        "opened_at": "2026-01-01T00:00:00+00:00",
    }


def test_add_and_list_trade(store):
    store.add_trade(_trade("a"))
    store.add_trade(_trade("b"))
    rows = store.list_trades()
    assert {r["id"] for r in rows} == {"a", "b"}
    assert rows[0]["entry_price"] == pytest.approx(0.45)


def test_remove_trade(store):
    store.add_trade(_trade("a"))
    assert store.remove_trade("a") is True
    assert store.remove_trade("a") is False
    assert store.list_trades() == []


def test_persistence_across_connections(tmp_path):
    path = str(tmp_path / "persist.db")
    s1 = Store(path)
    s1.add_trade(_trade("keep"))
    s1.close()

    s2 = Store(path)  # yeni bağlantı — veri hâlâ orada olmalı
    rows = s2.list_trades()
    s2.close()
    assert [r["id"] for r in rows] == ["keep"]


def test_close_trade_moves_to_closed(store):
    store.add_trade(_trade("x"))
    closed = store.close_trade("x", close_price=0.60, reason="manual")
    assert closed is not None
    assert closed["reason"] == "manual"
    # 22.22 shares * 0.60 - 10 ≈ +3.33
    assert closed["pnl"] == pytest.approx(22.22 * 0.60 - 10.0)
    assert store.list_trades() == []                      # açıktan kalktı
    assert [c["id"] for c in store.list_closed_trades()] == ["x"]
    assert store.realized_pnl_total() == pytest.approx(closed["pnl"])


def test_close_trade_unknown_returns_none(store):
    assert store.close_trade("nope", 0.5, "manual") is None


def _closed(tid, closed_at, pnl):
    return {
        "id": tid, "market_id": "m", "question": "Q", "side": "YES",
        "amount_usdc": 10.0, "entry_price": 0.5, "shares": 20.0,
        "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": closed_at,
        "close_price": 0.6, "pnl": pnl, "reason": "manual",
    }


def test_equity_curve_cumulative_and_order(store):
    # Kronolojik olmayan sırada ekle; eğri closed_at'e göre artan olmalı
    store.add_closed_trade(_closed("c2", "2026-01-02T00:00:00+00:00", -1.0))
    store.add_closed_trade(_closed("c1", "2026-01-01T00:00:00+00:00", 3.0))
    store.add_closed_trade(_closed("c3", "2026-01-03T00:00:00+00:00", 2.5))

    curve = store.equity_curve()
    assert [p["pnl"] for p in curve] == pytest.approx([3.0, -1.0, 2.5])
    # kümülatif: 3.0, 2.0, 4.5
    assert [p["cumulative"] for p in curve] == pytest.approx([3.0, 2.0, 4.5])


def test_equity_curve_empty(store):
    assert store.equity_curve() == []


def test_record_and_list_opportunity(store):
    oid = store.record_opportunity({
        "ts": "2026-01-01T00:00:00+00:00", "market_id": "m1", "question": "Q?",
        "direction": "buy", "profit_pct": 5.0, "yes_price": 0.45, "no_price": 0.45,
    })
    assert oid > 0
    rows = store.list_opportunities()
    assert len(rows) == 1
    assert rows[0]["direction"] == "buy"
    assert rows[0]["profit_pct"] == pytest.approx(5.0)


def test_list_opportunities_limit_and_order(store):
    for i in range(5):
        store.record_opportunity({
            "ts": f"2026-01-01T00:00:0{i}+00:00", "market_id": f"m{i}",
            "question": "Q", "direction": "buy", "profit_pct": float(i),
            "yes_price": 0.4, "no_price": 0.4,
        })
    rows = store.list_opportunities(limit=3)
    assert len(rows) == 3
    # en yeni (en büyük id) önce
    assert rows[0]["market_id"] == "m4"


# ── Haber sinyali arşivi ──────────────────────────────────────────────────
def _signal(sid="s1", impact=8, **kw):
    base = {
        "id": sid, "source": "TreeNews", "title": "Binance lists FOO",
        "url": "https://x/foo", "published": None,
        "fetched_at": "2026-06-14T00:00:00+00:00",
        "coins": ["FOO"], "impact": impact, "direction": "bullish",
        "reason": "listeleme", "scorer": "claude", "symbol": "FOOUSDT",
        "price_24h_pct": 1.2, "price_15m_pct": 0.5, "volume_usd": 2_000_000.0,
        "confirmed": True, "price_note": "uyumlu",
    }
    base.update(kw)
    return base


def test_add_signal_and_list(store):
    assert store.add_signal(_signal("a")) is True
    assert store.add_signal(_signal("b", impact=5, confirmed=False, coins=[])) is True
    rows = store.list_signals()
    assert {r["id"] for r in rows} == {"a", "b"}
    a = next(r for r in rows if r["id"] == "a")
    assert a["coins"] == ["FOO"]          # JSON listeye çözüldü
    assert a["confirmed"] is True         # int → bool
    b = next(r for r in rows if r["id"] == "b")
    assert b["coins"] == [] and b["confirmed"] is False


def test_add_signal_dedupe(store):
    assert store.add_signal(_signal("dup")) is True
    assert store.add_signal(_signal("dup")) is False   # aynı id → atlanır
    assert len(store.list_signals()) == 1


def test_list_signals_min_impact(store):
    store.add_signal(_signal("low", impact=5))
    store.add_signal(_signal("high", impact=9))
    rows = store.list_signals(min_impact=7)
    assert [r["id"] for r in rows] == ["high"]


def test_signal_span(store):
    assert store.signal_span()["count"] == 0
    store.add_signal(_signal("a"))
    store.add_signal(_signal("b"))
    span = store.signal_span()
    assert span["count"] == 2
    assert span["first_ts"] is not None and span["last_ts"] is not None


def test_signals_persist_across_connections(tmp_path):
    path = str(tmp_path / "sig.db")
    s1 = Store(path)
    s1.add_signal(_signal("keep"))
    s1.close()
    s2 = Store(path)
    rows = s2.list_signals()
    s2.close()
    assert [r["id"] for r in rows] == ["keep"]
