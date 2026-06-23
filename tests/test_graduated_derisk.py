"""Kademeli drawdown de-risking: _drawdown_size_factor + auto_decision entegrasyonu."""

from __future__ import annotations

import pytest

import trader


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader.S.auto_min_impact = 7
    trader.S.market = "spot"
    yield


class _Item:
    impact = 9
    direction = "bullish"
    symbol = "FOOUSDT"
    confirmed = True
    rel_volume = None
    atr_pct = None
    volume_usd = None


def _dd_to(pct, base=10000):
    # base'ten %pct drawdown üreten kapanmış işlem: peak=base, sonra -pct%
    return [{"pnl": -base * pct / 100}]


# ── _drawdown_size_factor ────────────────────────────────────────────────
def test_full_size_no_drawdown():
    trader.S.account_equity_usdt = 10000
    assert trader._drawdown_size_factor() == 1.0


def test_half_size_at_half_reference():
    trader.S.account_equity_usdt = 10000
    trader.S.max_drawdown_pct = 20.0          # referans %20
    trader._closed = _dd_to(10)               # %10 drawdown = ref/2
    assert trader._drawdown_size_factor() == pytest.approx(0.5, abs=0.01)


def test_floor_at_reference():
    trader.S.account_equity_usdt = 10000
    trader.S.max_drawdown_pct = 20.0
    trader._closed = _dd_to(20)               # %20 = ref → taban 0.25
    assert trader._drawdown_size_factor() == 0.25


def test_floor_clamped_beyond_reference():
    trader.S.account_equity_usdt = 10000
    trader.S.max_drawdown_pct = 20.0
    trader._closed = _dd_to(35)               # ref ötesi → yine 0.25 taban
    assert trader._drawdown_size_factor() == 0.25


def test_default_reference_when_no_killswitch():
    trader.S.account_equity_usdt = 10000
    trader.S.max_drawdown_pct = 0.0           # kill-switch kapalı → ref %20 default
    trader._closed = _dd_to(10)
    assert trader._drawdown_size_factor() == pytest.approx(0.5, abs=0.01)


# ── auto_decision entegrasyonu ───────────────────────────────────────────
def test_derisk_scales_position():
    trader.S.account_equity_usdt = 10000
    trader.S.max_drawdown_pct = 20.0
    trader.S.derisk_on_drawdown = True
    trader.S.trade_usdt = 100.0
    trader._closed = _dd_to(10)               # %10 → 0.5x
    d = trader.auto_decision(_Item())
    assert d["usdt"] == pytest.approx(50.0, abs=0.5)


def test_disabled_full_size():
    trader.S.derisk_on_drawdown = False
    trader.S.trade_usdt = 100.0
    trader._closed = _dd_to(15)
    d = trader.auto_decision(_Item())
    assert d["usdt"] == pytest.approx(100.0)


# ── get_risk surfacing ───────────────────────────────────────────────────
def test_get_risk_exposes_derisk_factor():
    trader.S.account_equity_usdt = 10000
    trader.S.max_drawdown_pct = 20.0
    trader.S.derisk_on_drawdown = True
    trader._closed = _dd_to(10)
    r = trader.get_risk()
    assert r["drawdown"]["derisk_on"] is True
    assert r["drawdown"]["size_factor"] == pytest.approx(0.5, abs=0.01)
