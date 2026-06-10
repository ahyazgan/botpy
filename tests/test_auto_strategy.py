"""Otomatik strateji testleri: evaluate_signal + auto_trade_step."""

from __future__ import annotations

import pytest

import bot
from storage import Store


def _row(mid="m1", bid=0.44, ask=0.45, spread=0.01, vol=50_000.0):
    return {
        "id": mid, "question": f"Q-{mid}", "bid": bid, "ask": ask,
        "spread": spread, "volume24h": vol,
    }


# ── evaluate_signal ──────────────────────────────────────────────────────
def test_signal_fires_on_tight_spread_in_band():
    assert bot.evaluate_signal(_row(spread=0.01, ask=0.45)) == "YES"


def test_signal_skips_wide_spread():
    assert bot.evaluate_signal(_row(spread=0.10, ask=0.45)) is None


def test_signal_skips_out_of_band_price():
    assert bot.evaluate_signal(_row(spread=0.01, ask=0.97)) is None
    assert bot.evaluate_signal(_row(spread=0.01, ask=0.02)) is None


def test_signal_skips_missing_fields():
    assert bot.evaluate_signal({"id": "x", "ask": None, "spread": 0.01}) is None
    assert bot.evaluate_signal({"id": "x", "ask": 0.45, "spread": None}) is None


# ── auto_trade_step ──────────────────────────────────────────────────────
@pytest.fixture()
def state(tmp_path):
    return bot.AppState(store=Store(str(tmp_path / "auto.db")))


def test_auto_trade_opens_for_signals(state):
    rows = [
        _row("m1", ask=0.45, spread=0.01),   # sinyal
        _row("m2", ask=0.50, spread=0.20),   # geniş spread → yok
        _row("m3", ask=0.30, spread=0.02),   # sinyal
    ]
    opened = bot.auto_trade_step(state, rows)
    assert opened == 2
    trades = state.list_trades()
    assert {t["market_id"] for t in trades} == {"m1", "m3"}
    assert all(t["side"] == "YES" for t in trades)
    # m1 girişi = ask
    m1 = next(t for t in trades if t["market_id"] == "m1")
    assert m1["entry_price"] == pytest.approx(0.45)
    assert m1["amount_usdc"] == pytest.approx(bot.AUTO_TRADE_AMOUNT)


def test_auto_trade_skips_existing_position(state):
    rows = [_row("m1", ask=0.45, spread=0.01)]
    assert bot.auto_trade_step(state, rows) == 1
    # İkinci çağrıda zaten açık → yeni açmaz
    assert bot.auto_trade_step(state, rows) == 0
    assert len(state.list_trades()) == 1


def test_auto_trade_no_signal_no_trade(state):
    rows = [_row("m1", ask=0.45, spread=0.50)]
    assert bot.auto_trade_step(state, rows) == 0
    assert state.list_trades() == []
