"""Akıllı çıkış + sinyal attribution + chase önleme + dinamik/portföy riski."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import trader


@pytest.fixture()
def clean(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    # tüm yeni özellikler varsayılan kapalı başlasın
    for k, v in {
        "time_stop_min": 0, "breakeven_pct": 0.0, "partial_tp_pct": 0.0,
        "partial_tp_frac": 0.5, "max_open_risk_usdt": 0.0, "reduce_after_losses": 0,
        "suppress_losing_sources": False, "min_source_samples": 8,
        "skip_already_priced_pct": 0.0, "auto_trade": True, "paper_trading": True,
        "auto_min_impact": 7, "auto_require_confirm": False, "market": "spot",
        "trade_usdt": 100.0, "size_by_impact": False, "max_positions": 20,
        "cooldown_sec": 0, "stop_loss_pct": 3.0, "use_entry_brain": False,
    }.items():
        setattr(trader.S, k, v)
    yield
    trader.S.auto_trade = False


def _pos(symbol="FOOUSDT", side="long", entry=100.0, usdt=100.0, **kw):
    p = {
        "id": symbol, "symbol": symbol, "side": side, "market": "spot", "mode": "paper",
        "usdt": usdt, "entry_price": entry, "amount": round(usdt / entry, 6),
        "leverage": 1, "sl_price": None, "tp_price": None, "trailing_pct": 0.0,
        "high_water": entry, "opened_at": trader._now(), "source": "auto",
        "news_source": "TreeNews", "reason": "",
    }
    p.update(kw)
    return p


class _Item:
    def __init__(self, impact=9, direction="bullish", source="TreeNews", price_24h_pct=None):
        self.impact = impact
        self.direction = direction
        self.symbol = "FOOUSDT"
        self.confirmed = True
        self.source = source
        self.price_24h_pct = price_24h_pct
        self.reason = ""


# ── 1. Akıllı çıkış: time-stop / breakeven / partial TP ────────────────────
def test_time_stop_closes_stale(clean, monkeypatch):
    trader.S.time_stop_min = 30
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    monkeypatch.setattr(trader, "_positions", [_pos(opened_at=old, tp_price=200.0)])
    monkeypatch.setattr(trader, "get_prices", lambda syms: {s: 100.5 for s in syms})   # hareket yok
    closed = trader.monitor_positions()
    assert len(closed) == 1 and closed[0]["close_reason"] == "time-stop"


def test_time_stop_respects_age(clean, monkeypatch):
    trader.S.time_stop_min = 30
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    monkeypatch.setattr(trader, "_positions", [_pos(opened_at=fresh, tp_price=200.0)])
    monkeypatch.setattr(trader, "get_prices", lambda syms: {s: 100.5 for s in syms})
    assert trader.monitor_positions() == []


def test_breakeven_moves_sl_to_entry(clean, monkeypatch):
    trader.S.breakeven_pct = 2.0
    p = _pos(entry=100.0, sl_price=97.0)
    monkeypatch.setattr(trader, "_positions", [p])
    monkeypatch.setattr(trader, "get_prices", lambda syms: {s: 103.0 for s in syms})   # +%3 > breakeven %2
    trader.monitor_positions()
    assert p["sl_price"] == 100.0 and p["breakeven_done"] is True


def test_partial_tp_scales_out(clean, monkeypatch):
    trader.S.partial_tp_pct = 5.0
    trader.S.partial_tp_frac = 0.5
    p = _pos(entry=100.0, usdt=100.0, tp_price=200.0)
    monkeypatch.setattr(trader, "_positions", [p])
    monkeypatch.setattr(trader, "get_prices", lambda syms: {s: 106.0 for s in syms})   # +%6 > partial %5
    closed = trader.monitor_positions()
    assert len(closed) == 1 and closed[0]["close_reason"] == "partial-tp"
    assert closed[0]["usdt"] == 50.0           # yarısı kapandı
    assert p["usdt"] == 50.0 and p["partial_done"] is True
    # ikinci turda tekrar kısmi alınmaz
    assert trader.monitor_positions() == []


# ── 2. Chase önleme ────────────────────────────────────────────────────────
def test_skip_already_priced(clean, monkeypatch):
    trader.S.skip_already_priced_pct = 15.0
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    captured = {}
    monkeypatch.setattr(trader, "place_trade", lambda *a, **k: captured.setdefault("hit", True))
    # bullish + 24s'te +%20 → zaten fiyatlanmış, atla
    assert trader.maybe_auto_trade(_Item(direction="bullish", price_24h_pct=20.0)) is None
    assert "hit" not in captured
    # +%5 → girer
    trader.maybe_auto_trade(_Item(direction="bullish", price_24h_pct=5.0))
    assert captured.get("hit") is True


# ── 3. Sinyal attribution / öğrenme ────────────────────────────────────────
def test_source_stats(clean, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [
        {"news_source": "BadSrc", "pnl": -5.0}, {"news_source": "BadSrc", "pnl": -3.0},
        {"news_source": "GoodSrc", "pnl": 8.0},
    ])
    assert trader.source_stats("BadSrc") == {"count": 2, "avg_pnl": -4.0}
    assert trader.source_stats("GoodSrc")["avg_pnl"] == 8.0
    assert trader.source_stats("Unknown")["count"] == 0


def test_suppress_losing_source(clean, monkeypatch):
    trader.S.suppress_losing_sources = True
    trader.S.min_source_samples = 2
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    monkeypatch.setattr(trader, "_closed",
                        [{"news_source": "BadSrc", "pnl": -5.0}, {"news_source": "BadSrc", "pnl": -3.0}])
    captured = {}
    monkeypatch.setattr(trader, "place_trade", lambda *a, **k: captured.setdefault("hit", True))
    assert trader.maybe_auto_trade(_Item(source="BadSrc")) is None
    assert "hit" not in captured
    # örnek yetersizse susturma yok
    trader.S.min_source_samples = 5
    trader.maybe_auto_trade(_Item(source="BadSrc"))
    assert captured.get("hit") is True


# ── 4. Dinamik / portföy riski ─────────────────────────────────────────────
def test_losing_streak_halves_size(clean, monkeypatch):
    trader.S.reduce_after_losses = 3
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    monkeypatch.setattr(trader, "_closed", [{"pnl": -1.0}, {"pnl": -2.0}, {"pnl": -1.0}])
    cap = {}
    monkeypatch.setattr(trader, "place_trade", lambda *a, **k: cap.update(usdt=k.get("usdt")))
    trader.maybe_auto_trade(_Item())
    assert cap["usdt"] == 50.0                  # 100 * 0.5 (kayıp serisi freni)


def test_losing_streak_count(clean, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [{"pnl": 5.0}, {"pnl": -1.0}, {"pnl": -2.0}])
    assert trader._losing_streak() == 2         # son iki zarar, öncesi kâr kırar


def test_open_risk_cap(clean, monkeypatch):
    trader.S.max_open_risk_usdt = 5.0
    trader.S.stop_loss_pct = 3.0
    # mevcut açık risk: 100 USDT, SL %4 → risk 4 USDT
    monkeypatch.setattr(trader, "_positions", [_pos(entry=100.0, usdt=100.0, sl_price=96.0)])
    # yeni işlem riski 100*3% = 3 → toplam 7 > 5 → red
    with pytest.raises(RuntimeError, match="Açık risk"):
        trader._check_risk("BARUSDT", 100.0)


def test_position_risk():
    assert trader._position_risk({"usdt": 100.0, "entry_price": 100.0, "sl_price": 97.0}) == 3.0
    assert trader._position_risk({"usdt": 100.0, "entry_price": 100.0, "sl_price": None}) == 100.0
