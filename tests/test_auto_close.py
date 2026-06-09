"""Otomatik pozisyon kapatma (TP/SL) testleri + /trades/closed."""

from __future__ import annotations

import pytest

import bot
from storage import Store


# ── should_close ─────────────────────────────────────────────────────────
def test_should_close_take_profit():
    # entry 0.45, current 0.55 → +%22 → TP (eşik %20)
    assert bot.should_close(0.45, 0.55) == "take_profit"


def test_should_close_stop_loss():
    # entry 0.45, current 0.36 → -%20 → SL (eşik %15)
    assert bot.should_close(0.45, 0.36) == "stop_loss"


def test_should_close_hold():
    assert bot.should_close(0.45, 0.46) is None


def test_should_close_none_current():
    assert bot.should_close(0.45, None) is None


# ── closed_row ───────────────────────────────────────────────────────────
def test_closed_row_pnl_math():
    trade = {
        "id": "t1", "market_id": "m1", "question": "Q", "side": "YES",
        "amount_usdc": 10.0, "entry_price": 0.50, "shares": 20.0,
        "opened_at": "2026-01-01T00:00:00+00:00",
    }
    row = bot.closed_row(trade, close_price=0.60, reason="take_profit")
    # 20 shares * 0.60 - 10 = 2.0
    assert row["pnl"] == pytest.approx(2.0)
    assert row["reason"] == "take_profit"
    assert row["close_price"] == pytest.approx(0.60)


# ── auto_close_step ──────────────────────────────────────────────────────
@pytest.fixture()
def state(tmp_path):
    return bot.AppState(store=Store(str(tmp_path / "close.db")))


def test_auto_close_take_profit_flow(state):
    # YES pozisyonun güncel fiyatı = market bid
    state.update_snapshot(
        None,
        [{"id": "m1", "question": "Q", "bid": 0.55, "ask": 0.45,
          "spread": 0.10, "volume24h": 1.0}],
        1,
    )
    state.add_trade(bot.new_trade(
        {"id": "m1", "question": "Q"}, "YES", 10.0, 0.45,
    ))

    closed = bot.auto_close_step(state)
    assert closed == 1
    assert state.list_trades() == []          # açık pozisyon kalmadı
    closed_rows = state.list_closed_trades()
    assert len(closed_rows) == 1
    assert closed_rows[0]["reason"] == "take_profit"
    assert state.realized_pnl_total() > 0


def test_auto_close_holds_when_no_trigger(state):
    state.update_snapshot(
        None,
        [{"id": "m1", "question": "Q", "bid": 0.46, "ask": 0.45,
          "spread": 0.01, "volume24h": 1.0}],
        1,
    )
    state.add_trade(bot.new_trade({"id": "m1", "question": "Q"}, "YES", 10.0, 0.45))
    assert bot.auto_close_step(state) == 0
    assert len(state.list_trades()) == 1


# ── /trades/closed endpoint ──────────────────────────────────────────────
def test_closed_trades_endpoint():
    from fastapi.testclient import TestClient

    bot.state.store.add_closed_trade({
        "id": "cT", "market_id": "m9", "question": "Q?", "side": "YES",
        "amount_usdc": 10.0, "entry_price": 0.5, "shares": 20.0,
        "opened_at": "2026-01-01T00:00:00+00:00",
        "closed_at": "2026-01-01T01:00:00+00:00",
        "close_price": 0.6, "pnl": 2.0, "reason": "take_profit",
    })
    client = TestClient(bot.app)
    with client:
        data = client.get("/trades/closed?limit=10").json()
    assert any(t["id"] == "cT" for t in data["trades"])
    assert data["realized_pnl"] >= 2.0
