"""Yüzde-bazlı pozisyon boyutlama: _account_equity + _risk_per_trade_base + auto_decision."""

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


# ── _account_equity ──────────────────────────────────────────────────────
def test_equity_base_plus_realized():
    trader.S.account_equity_usdt = 10000
    trader._closed = [{"pnl": 250.0}, {"pnl": -100.0}]
    assert trader._account_equity() == pytest.approx(10150.0)


def test_equity_never_negative():
    trader.S.account_equity_usdt = 100
    trader._closed = [{"pnl": -5000.0}]
    assert trader._account_equity() == 0.0


# ── _risk_per_trade_base ─────────────────────────────────────────────────
def test_risk_base_formula():
    trader.S.account_equity_usdt = 10000
    trader.S.risk_per_trade_pct = 1.0   # %1 risk
    # SL %3 → notional = 10000*1/3 = 3333.33 (SL'de %3 → 100 USDT = %1 sermaye)
    base = trader._risk_per_trade_base(3.0)
    assert base == pytest.approx(3333.33, abs=0.5)
    # Doğrula: base * stop% = risk = equity * pct%
    assert base * 0.03 == pytest.approx(10000 * 0.01, abs=0.2)


def test_tight_stop_bigger_position():
    trader.S.account_equity_usdt = 10000
    trader.S.risk_per_trade_pct = 1.0
    wide = trader._risk_per_trade_base(5.0)
    tight = trader._risk_per_trade_base(1.0)
    assert tight > wide   # dar SL → büyük pozisyon (sabit USDT-risk)


def test_stop_floor_prevents_blowup():
    trader.S.account_equity_usdt = 10000
    trader.S.risk_per_trade_pct = 1.0
    # SL %0.01 → taban %0.5'e kıstırılır, sonuç 3×sermaye tavanıyla sınırlı
    base = trader._risk_per_trade_base(0.01)
    assert base <= 10000 * 3.0


# ── auto_decision entegrasyonu ───────────────────────────────────────────
def test_auto_decision_uses_risk_pct_base():
    trader.S.account_equity_usdt = 10000
    trader.S.risk_per_trade_pct = 2.0
    trader.S.stop_loss_pct = 4.0
    d = trader.auto_decision(_Item())
    assert d["would_trade"] is True
    # taban = 10000*2/4 = 5000
    assert d["usdt"] == pytest.approx(5000.0, abs=1.0)


def test_fixed_usdt_when_disabled():
    trader.S.risk_per_trade_pct = 0.0   # kapalı
    trader.S.trade_usdt = 123.0
    d = trader.auto_decision(_Item())
    assert d["usdt"] == pytest.approx(123.0)


def test_risk_parity_skipped_when_pct_active():
    # risk_per_trade_pct açıkken risk_parity çift-saymamalı (taban zaten SL-normalize)
    trader.S.account_equity_usdt = 10000
    trader.S.risk_per_trade_pct = 1.0
    trader.S.stop_loss_pct = 5.0
    trader.S.risk_parity = True
    trader.S.target_risk_usdt = 50.0
    d = trader.auto_decision(_Item())
    # risk_parity uygulanmasaydı taban = 10000*1/5 = 2000; parity çarpanı yok
    assert d["usdt"] == pytest.approx(2000.0, abs=1.0)


# ── get_risk surfacing ───────────────────────────────────────────────────
def test_get_risk_sizing_field():
    trader.S.risk_per_trade_pct = 1.5
    trader.S.account_equity_usdt = 5000
    r = trader.get_risk()
    assert r["sizing"]["mode"] == "risk_pct"
    assert r["sizing"]["risk_per_trade_pct"] == 1.5
    assert r["sizing"]["equity"] == pytest.approx(5000.0)
