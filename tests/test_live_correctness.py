"""Canlı doğruluk düzeltmeleri: emir hassasiyeti/minNotional, tasfiye-farkında SL, mutabakat-iyileştirme."""

from __future__ import annotations

import pytest

import trader


# ── Fix 1: _round_amount (lot-size / minNotional) ─────────────────────────
class MarketEx:
    def __init__(self, min_cost=None, min_amount=None, prec=4):
        self.markets = {"FOO/USDT": {"limits": {
            "cost": {"min": min_cost}, "amount": {"min": min_amount}}}}
        self._prec = prec

    def market(self, sym):
        return self.markets[sym]

    def amount_to_precision(self, sym, amount):
        return f"{amount:.{self._prec}f}"


def test_round_amount_applies_precision():
    ex = MarketEx(prec=2)
    assert trader._round_amount(ex, "FOO/USDT", 1.23456, 100.0) == 1.23


def test_round_amount_rejects_below_min_notional():
    ex = MarketEx(min_cost=10.0)
    with pytest.raises(RuntimeError, match="minNotional"):
        trader._round_amount(ex, "FOO/USDT", 0.05, 100.0)   # 0.05*100=$5 < $10


def test_round_amount_rejects_below_min_amount():
    ex = MarketEx(min_amount=1.0, prec=4)
    with pytest.raises(RuntimeError, match="min miktar"):
        trader._round_amount(ex, "FOO/USDT", 0.5, 100.0)


def test_round_amount_no_market_returns_raw():
    class Bare:
        markets = None
        def load_markets(self): raise RuntimeError("yok")
    assert trader._round_amount(Bare(), "FOO/USDT", 1.2345, 100.0) == 1.2345


# ── Fix 3: tasfiye-farkında SL (place_trade, futures) ─────────────────────
@pytest.fixture()
def paper_fut(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    monkeypatch.setattr(trader, "_estimate_fill", lambda *a, **k: None)
    monkeypatch.setattr(trader, "get_price", lambda s: 100.0)
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    for k, v in {
        "paper_trading": True, "market": "futures", "leverage": 20, "use_sl_tp": True,
        "trade_usdt": 100.0, "order_type": "market", "max_positions": 20,
        "min_orderbook_usd": 0.0, "slippage_guard_pct": 0.0, "trailing_stop_pct": 0.0,
        "stop_loss_pct": 8.0, "take_profit_pct": 6.0, "use_atr_exits": False,
        "exchange_native_stops": False,
        "daily_loss_limit_usdt": 0.0, "max_total_exposure_usdt": 0.0, "max_per_coin_usdt": 0.0,
    }.items():
        setattr(trader.S, k, v)
    yield


def test_sl_clamped_inside_liquidation(paper_fut):
    # 20x → tasfiye ~%5; güvenli SL = 0.8*5 = %4. İstenen %8 → %4'e kıstırılmalı.
    pos = trader.place_trade("FOOUSDT", "long")
    # long SL = entry*(1 - sl%/100); %4 → 96.0
    assert pos["sl_price"] == pytest.approx(96.0)


def test_sl_not_clamped_when_safe(paper_fut):
    trader.S.stop_loss_pct = 3.0   # 20x'te %3 < güvenli %4 → dokunma
    pos = trader.place_trade("FOOUSDT", "long")
    assert pos["sl_price"] == pytest.approx(97.0)


# ── Fix 2: reconcile_and_heal (hayalet pozisyon) ──────────────────────────
def test_reconcile_heal_detects_phantom(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [
        {"id": "p1", "symbol": "FOOUSDT", "side": "long", "mode": "live", "market": "futures",
         "amount": 1.0, "entry_price": 100.0, "usdt": 100.0, "leverage": 20},
    ])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader.S, "paper_trading", False)
    monkeypatch.setattr(trader, "has_live_keys", lambda: True)
    monkeypatch.setattr(trader, "_fetch_exchange_symbols", lambda: set())   # borsa düz
    out = trader.reconcile_and_heal(autoclose=False)
    assert out["checked"] and len(out["phantoms"]) == 1 and out["healed"] == []
    assert len(trader._positions) == 1   # autoclose kapalı → dokunmadı


def test_reconcile_heal_autocloses_phantom(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [
        {"id": "p1", "symbol": "FOOUSDT", "side": "long", "mode": "live", "market": "futures",
         "amount": 1.0, "entry_price": 100.0, "usdt": 100.0, "leverage": 20},
    ])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader.S, "paper_trading", False)
    monkeypatch.setattr(trader, "has_live_keys", lambda: True)
    monkeypatch.setattr(trader, "_fetch_exchange_symbols", lambda: set())
    monkeypatch.setattr(trader, "get_price", lambda s: 105.0)
    sent = {"order": False}
    monkeypatch.setattr(trader, "_get_exchange", lambda: object())
    monkeypatch.setattr(trader, "_ccxt_symbol", lambda s: "FOO/USDT")
    monkeypatch.setattr(trader, "_cancel_protective_orders", lambda *a: None)
    monkeypatch.setattr(trader, "_create_order_idempotent",
                        lambda *a, **k: sent.update(order=True))
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    out = trader.reconcile_and_heal(autoclose=True)
    assert len(out["healed"]) == 1 and trader._positions == []
    assert sent["order"] is False   # borsaya kapanış emri GÖNDERİLMEDİ (zaten düz)
