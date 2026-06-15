"""Kalıcı kapanan-işlem defteri: storage + persist + /trades/closed arşivden."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader
from storage import Store


def _trade(tid="t1", closed_at="2026-06-15T00:00:00.1+00:00", pnl=5.0):
    return {
        "id": tid, "closed_at": closed_at, "opened_at": "2026-06-15T00:00:00+00:00",
        "symbol": "FOOUSDT", "side": "long", "mode": "paper", "usdt": 100.0,
        "entry_price": 1.0, "close_price": 1.05, "pnl": pnl, "pnl_pct": 5.0,
        "close_reason": "take-profit", "source": "auto", "news_source": "TreeNews",
        "impact": 9,
    }


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "cl.db"))
    yield s
    s.close()


def test_add_and_list(store):
    assert store.add_closed_news_trade(_trade("a")) is True
    assert store.add_closed_news_trade(_trade("b", pnl=-2.0)) is True
    rows = store.list_closed_news_trades()
    assert {r["id"] for r in rows} == {"a", "b"}
    assert rows[0]["news_source"] == "TreeNews" and rows[0]["impact"] == 9
    assert "row_id" not in rows[0] and "trade_id" not in rows[0]


def test_dedupe_same_id_and_time(store):
    assert store.add_closed_news_trade(_trade("dup")) is True
    assert store.add_closed_news_trade(_trade("dup")) is False   # aynı (id, closed_at)
    assert store.count_closed_news_trades() == 1


def test_partial_and_final_same_id_kept(store):
    # kısmi + tam: aynı id, farklı closed_at → ikisi de saklanır
    store.add_closed_news_trade(_trade("x", closed_at="2026-06-15T00:00:00.1+00:00"))
    store.add_closed_news_trade(_trade("x", closed_at="2026-06-15T00:05:00.2+00:00"))
    assert store.count_closed_news_trades() == 2


def test_missing_keys_rejected(store):
    assert store.add_closed_news_trade({"id": "", "closed_at": "t"}) is False
    assert store.add_closed_news_trade({"id": "y", "closed_at": None}) is False


def test_persists_across_connections(tmp_path):
    p = str(tmp_path / "persist.db")
    s1 = Store(p)
    s1.add_closed_news_trade(_trade("keep"))
    s1.close()
    s2 = Store(p)
    assert [r["id"] for r in s2.list_closed_news_trades()] == ["keep"]
    s2.close()


def test_trades_closed_reads_archive(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "e.db"))
    monkeypatch.setattr(nb, "_store", s)
    monkeypatch.setattr(trader, "_closed", [])           # in-memory boş
    s.add_closed_news_trade(_trade("arch1"))
    c = TestClient(nb.app)
    d = c.get("/trades/closed").json()
    assert [t["id"] for t in d["trades"]] == ["arch1"]   # arşivden okudu
    s.close()


def test_trades_closed_fallback_to_memory(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "e2.db"))                    # arşiv boş
    monkeypatch.setattr(nb, "_store", s)
    monkeypatch.setattr(trader, "_closed", [_trade("mem1")])
    c = TestClient(nb.app)
    d = c.get("/trades/closed").json()
    assert [t["id"] for t in d["trades"]] == ["mem1"]    # in-memory'e düştü
    s.close()
