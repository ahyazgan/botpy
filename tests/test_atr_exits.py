"""Volatilite-bazlı (ATR) SL/TP — place_trade dinamik çıkış (Faz 4)."""

from __future__ import annotations

import pytest

import trader


@pytest.fixture()
def clean(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    monkeypatch.setattr(trader, "_estimate_fill", lambda *a, **k: None)  # orderbook atla
    monkeypatch.setattr(trader, "get_price", lambda s: 100.0)
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    for k, v in {
        "paper_trading": True, "market": "spot", "use_sl_tp": True,
        "trade_usdt": 100.0, "leverage": 1, "order_type": "market", "max_positions": 20,
        "min_orderbook_usd": 0.0, "slippage_guard_pct": 0.0, "trailing_stop_pct": 0.0,
        "stop_loss_pct": 3.0, "take_profit_pct": 6.0,
        "daily_loss_limit_usdt": 0.0, "max_total_exposure_usdt": 0.0,
        "max_per_coin_usdt": 0.0, "max_open_risk_usdt": 0.0,
        "use_atr_exits": False, "atr_sl_mult": 1.5, "atr_tp_mult": 3.0,
    }.items():
        setattr(trader.S, k, v)
    yield


def test_fixed_pct_when_atr_off(clean):
    """ATR kapalıyken sabit % SL/TP kullanılır."""
    pos = trader.place_trade("FOOUSDT", "long", atr_pct=4.0)
    assert pos["sl_price"] == pytest.approx(97.0)   # -%3
    assert pos["tp_price"] == pytest.approx(106.0)  # +%6
    assert pos["atr_pct"] is None


def test_atr_scales_sl_tp(clean):
    """ATR açıkken SL=1.5×ATR, TP=3×ATR (ATR=%4 → SL %6, TP %12)."""
    trader.S.use_atr_exits = True
    pos = trader.place_trade("FOOUSDT", "long", atr_pct=4.0)
    assert pos["sl_price"] == pytest.approx(94.0)    # -%6
    assert pos["tp_price"] == pytest.approx(112.0)   # +%12
    assert pos["atr_pct"] == pytest.approx(4.0)


def test_atr_clamped_to_bounds(clean):
    """Aşırı oynaklıkta SL %15, TP %30'a kıstırılır."""
    trader.S.use_atr_exits = True
    pos = trader.place_trade("FOOUSDT", "long", atr_pct=20.0)
    assert pos["sl_price"] == pytest.approx(85.0)    # SL %15 (1.5×20=30→15)
    assert pos["tp_price"] == pytest.approx(130.0)   # TP %30 (3×20=60→30)


def test_atr_short_direction(clean):
    """Short'ta ATR SL yukarı, TP aşağı."""
    trader.S.use_atr_exits = True
    trader.S.market = "futures"
    pos = trader.place_trade("FOOUSDT", "short", atr_pct=4.0)
    assert pos["sl_price"] == pytest.approx(106.0)   # +%6 (short SL yukarı)
    assert pos["tp_price"] == pytest.approx(88.0)    # -%12 (short TP aşağı)


def test_atr_falls_back_without_value(clean):
    """ATR açık ama atr_pct yok → sabit %'ye düşer."""
    trader.S.use_atr_exits = True
    pos = trader.place_trade("FOOUSDT", "long", atr_pct=None)
    assert pos["sl_price"] == pytest.approx(97.0) and pos["tp_price"] == pytest.approx(106.0)
    assert pos["atr_pct"] is None
