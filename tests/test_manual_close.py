"""POST /trades/{id}/close manuel kapatma testleri."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import bot


@pytest.fixture()
def client():
    return TestClient(bot.app)


def test_manual_close_records_realized_pnl(client):
    # Güncel fiyat = market bid (YES) = 0.55; giriş 0.45
    bot.state.update_snapshot(
        None,
        [{"id": "mc1", "question": "Q", "bid": 0.55, "ask": 0.45,
          "spread": 0.10, "volume24h": 1.0}],
        1,
    )
    trade = bot.new_trade({"id": "mc1", "question": "Q"}, "YES", 10.0, 0.45)
    bot.state.add_trade(trade)

    resp = client.post(f"/trades/{trade['id']}/close")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason"] == "manual"
    assert body["close_price"] == pytest.approx(0.55)
    # 22.22 shares * 0.55 - 10 ≈ +2.22
    assert body["pnl"] > 0

    # Açık pozisyonlardan kalktı, kapanan deftere geçti
    open_ids = {t["id"] for t in bot.state.list_trades()}
    assert trade["id"] not in open_ids
    assert any(t["id"] == trade["id"] for t in bot.state.list_closed_trades())


def test_manual_close_unknown_id_404(client):
    resp = client.post("/trades/does-not-exist/close")
    assert resp.status_code == 404


def test_manual_close_no_price_422(client):
    # Market cache'te yok → güncel fiyat hesaplanamaz → 422
    bot.state.update_snapshot(None, [], 0)
    trade = bot.new_trade({"id": "absent", "question": "Q"}, "YES", 10.0, 0.45)
    bot.state.add_trade(trade)

    resp = client.post(f"/trades/{trade['id']}/close")
    assert resp.status_code == 422
    # Hâlâ açık (kapanmadı)
    assert any(t["id"] == trade["id"] for t in bot.state.list_trades())
    # Temizlik
    bot.state.remove_trade(trade["id"])
