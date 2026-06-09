"""api.py read-only uçları (/trades, /trades/closed, /arb) testleri."""

from __future__ import annotations

import api
from fastapi.testclient import TestClient


def test_api_open_trades():
    api._store.add_trade({
        "id": "apiT", "market_id": "mA", "question": "Q", "side": "YES",
        "amount_usdc": 10.0, "entry_price": 0.45, "shares": 22.2,
        "opened_at": "2026-01-01T00:00:00+00:00",
    })
    client = TestClient(api.app)
    d = client.get("/trades").json()
    assert any(t["id"] == "apiT" for t in d["trades"])
    assert d["count"] >= 1


def test_api_closed_trades_and_realized():
    api._store.add_closed_trade({
        "id": "apiC", "market_id": "mB", "question": "Q2", "side": "YES",
        "amount_usdc": 10.0, "entry_price": 0.5, "shares": 20.0,
        "opened_at": "2026-01-01T00:00:00+00:00",
        "closed_at": "2026-01-01T01:00:00+00:00",
        "close_price": 0.6, "pnl": 2.0, "reason": "take_profit",
    })
    client = TestClient(api.app)
    d = client.get("/trades/closed?limit=50").json()
    assert any(t["id"] == "apiC" for t in d["trades"])
    assert d["realized_pnl"] >= 2.0
