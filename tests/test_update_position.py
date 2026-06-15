"""Canlı SL/TP düzenleme: trader.update_position + PATCH /positions/{id}."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader


@pytest.fixture()
def pos(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(trader, "_positions", [
        {"id": "p1", "symbol": "FOOUSDT", "side": "long", "entry_price": 100.0,
         "sl_price": 97.0, "tp_price": 106.0},
    ])
    monkeypatch.setattr(nb, "API_TOKEN", None)
    yield


def test_update_sl_tp(pos):
    out = trader.update_position("p1", sl_price=98.0, tp_price=110.0)
    assert out["sl_price"] == 98.0 and out["tp_price"] == 110.0
    assert trader._positions[0]["sl_price"] == 98.0


def test_clear_sl_with_zero(pos):
    out = trader.update_position("p1", sl_price=0)
    assert out["sl_price"] is None
    assert out["tp_price"] == 106.0          # tp dokunulmadı (None geçildi)


def test_partial_update_keeps_other(pos):
    out = trader.update_position("p1", tp_price=120.0)
    assert out["tp_price"] == 120.0 and out["sl_price"] == 97.0


def test_unknown_raises(pos):
    with pytest.raises(RuntimeError, match="bulunamadı"):
        trader.update_position("nope", sl_price=1.0)


def test_patch_endpoint(pos):
    c = TestClient(nb.app)
    r = c.patch("/positions/p1", json={"sl_price": 95.5})
    assert r.status_code == 200 and r.json()["sl_price"] == 95.5
    assert c.patch("/positions/zzz", json={"tp_price": 1.0}).status_code == 404


def test_patch_token_protected(pos, monkeypatch):
    monkeypatch.setattr(nb, "API_TOKEN", "secret")
    c = TestClient(nb.app)
    assert c.patch("/positions/p1", json={"sl_price": 95.0}).status_code == 401
    assert c.patch("/positions/p1", json={"sl_price": 95.0},
                   headers={"X-API-Token": "secret"}).status_code == 200
