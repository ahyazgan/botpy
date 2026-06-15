"""Acil 'tümünü kapat': trader.close_all + POST /positions/close-all."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader
from storage import Store


def _pos(pid, symbol="FOOUSDT", entry=100.0, usdt=100.0):
    return {"id": pid, "symbol": symbol, "side": "long", "market": "spot",
            "mode": "paper", "usdt": usdt, "entry_price": entry,
            "amount": round(usdt / entry, 6), "leverage": 1}


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(trader, "get_price", lambda s: 110.0)   # +%10
    monkeypatch.setattr(trader, "_positions", [_pos("p1"), _pos("p2", "BARUSDT")])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    yield


def test_close_all_closes_every_position(env):
    rep = trader.close_all()
    assert rep["count"] == 2 and rep["failed"] == 0
    assert {c["symbol"] for c in rep["closed"]} == {"FOOUSDT", "BARUSDT"}
    assert rep["total_pnl"] == pytest.approx(20.0)   # her biri +10
    assert trader._positions == []                    # hepsi kapandı


def test_close_all_error_isolation(env, monkeypatch):
    real = trader.close_position

    def flaky(pid, reason="manuel"):
        if pid == "p2":
            raise RuntimeError("borsa hatası")
        return real(pid, reason)
    monkeypatch.setattr(trader, "close_position", flaky)

    rep = trader.close_all()
    assert rep["count"] == 1 and rep["failed"] == 1
    assert rep["errors"][0]["symbol"] == "BARUSDT"
    assert rep["closed"][0]["symbol"] == "FOOUSDT"   # diğeri yine kapandı


def test_close_all_empty(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    rep = trader.close_all()
    assert rep == {"closed": [], "errors": [], "count": 0, "failed": 0, "total_pnl": 0.0}


def test_close_all_endpoint(env, monkeypatch, tmp_path):
    s = Store(str(tmp_path / "ca.db"))
    monkeypatch.setattr(nb, "_store", s)
    monkeypatch.setattr(nb, "API_TOKEN", None)
    notes: list[str] = []
    monkeypatch.setattr(nb, "notify_remote", lambda m: notes.append(m))
    c = TestClient(nb.app)
    rep = c.post("/positions/close-all").json()
    assert rep["count"] == 2 and rep["total_pnl"] == pytest.approx(20.0)
    assert s.count_closed_news_trades() == 2          # arşive yazıldı
    assert notes and "TÜMÜ KAPATILDI" in notes[0]     # uzak bildirim
    s.close()


def test_close_all_token_protected(env, monkeypatch):
    monkeypatch.setattr(nb, "API_TOKEN", "secret")
    c = TestClient(nb.app)
    assert c.post("/positions/close-all").status_code == 401
    assert c.post("/positions/close-all", headers={"X-API-Token": "secret"}).status_code == 200
