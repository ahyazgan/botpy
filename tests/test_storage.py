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
