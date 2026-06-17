"""Shadow-mode / A-B: aday ayar canlı sinyallerle SANAL test edilir (gerçek emir YOK).

S geçici override edilir → auto_decision → AYNEN geri yüklenir (deadlock yok: iç kilitler
nedeniyle _lock TUTULMAZ). Para-büyüklüğü/risk tavanları gölgede override edilemez.
"""

from __future__ import annotations

import pytest

import trader
from storage import Store


class _Item:
    def __init__(self, impact=7):
        self.impact = impact
        self.direction = "bullish"
        self.symbol = "FOOUSDT"
        self.confirmed = True
        self.atr_pct = None


@pytest.fixture()
def shadow_env(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader.S.auto_min_impact = 8
    trader.S.auto_require_confirm = True
    trader.S.market = "spot"
    trader.S.trade_usdt = 100.0
    trader.S.size_by_impact = False
    trader.S.size_by_kelly = False
    trader.S.size_by_volume = False
    trader.S.risk_parity = False
    trader.S.portfolio_risk = False
    trader.S.reduce_after_losses = 0
    trader.S.suppress_losing_sources = False
    trader.S.use_learned_vetoes = False
    trader.S.skip_already_priced_pct = 0.0
    trader.S.min_rel_volume = 0.0
    trader.S.max_same_direction = 0
    trader.set_shadow_overrides({})
    yield
    trader.set_shadow_overrides({})


# ── set/get overrides: güvenli alan filtresi ─────────────────────────────────
def test_shadow_overrides_filters_unsafe(shadow_env):
    applied = trader.set_shadow_overrides({"auto_min_impact": 7, "trade_usdt": 9999,
                                           "max_total_exposure_usdt": 1, "leverage": 50})
    assert applied == {"auto_min_impact": 7}   # yalnız güvenli karar-eşiği alanı
    assert "trade_usdt" not in applied
    assert "leverage" not in applied


def test_shadow_disabled_returns_none(shadow_env):
    assert trader.shadow_decision(_Item()) is None   # override yok → gölge kapalı


# ── shadow_decision: divergence + S geri yükleme ─────────────────────────────
def test_shadow_diverges_on_threshold(shadow_env):
    # canlı eşik 8 → impact 7 girmez; aday eşik 7 → girer (divergence)
    trader.set_shadow_overrides({"auto_min_impact": 7})
    res = trader.shadow_decision(_Item(impact=7))
    assert res is not None
    assert res["live"]["would_trade"] is False   # canlı: eşik altı
    assert res["shadow"]["would_trade"] is True   # aday: girer
    assert res["diverged"] is True


def test_shadow_no_divergence_when_same(shadow_env):
    # aday eşik 8 = canlı eşik → aynı karar
    trader.set_shadow_overrides({"auto_min_impact": 8})
    res = trader.shadow_decision(_Item(impact=9))
    assert res["live"]["would_trade"] is True
    assert res["shadow"]["would_trade"] is True
    assert res["diverged"] is False


def test_shadow_restores_settings(shadow_env):
    # gölge S'i geçici değiştirir ama AYNEN geri yükler (kalıcı sızıntı yok)
    trader.S.auto_min_impact = 8
    trader.set_shadow_overrides({"auto_min_impact": 5})
    trader.shadow_decision(_Item(impact=7))
    assert trader.S.auto_min_impact == 8   # geri yüklendi


def test_shadow_no_deadlock_with_position_gate(shadow_env, monkeypatch):
    # auto_decision iç kilit alan _open_side_count'u çağırır — gölge deadlock'a girmemeli
    monkeypatch.setattr(trader, "_positions", [{"symbol": "XUSDT", "side": "long"}])
    trader.S.max_same_direction = 5
    trader.set_shadow_overrides({"auto_min_impact": 7})
    res = trader.shadow_decision(_Item(impact=7))   # iç kilitler alınır; deadlock olmamalı
    assert res is not None   # buraya ulaşması = deadlock yok


def test_shadow_size_divergence(shadow_env):
    # aynı giriş kararı ama farklı boyut (aday size_by_impact açık) → divergence
    trader.set_shadow_overrides({"size_by_impact": True})
    res = trader.shadow_decision(_Item(impact=10))
    assert res["live"]["would_trade"] and res["shadow"]["would_trade"]
    assert res["live"]["usdt"] == 100.0
    assert res["shadow"]["usdt"] == 150.0   # 100 × 1.5
    assert res["diverged"] is True


# ── storage: add_shadow_decision + shadow_summary ───────────────────────────
def test_storage_shadow_roundtrip(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.add_shadow_decision({"news_id": "a", "symbol": "FOOUSDT", "side": "long",
                               "impact": 7, "live_trade": False, "shadow_trade": True,
                               "live_usdt": None, "shadow_usdt": 100.0, "diverged": True,
                               "overrides": '{"auto_min_impact": 7}'})
    store.add_shadow_decision({"news_id": "b", "symbol": "BARUSDT", "side": "long",
                               "impact": 9, "live_trade": True, "shadow_trade": True,
                               "live_usdt": 100.0, "shadow_usdt": 100.0, "diverged": False,
                               "overrides": '{"auto_min_impact": 7}'})
    s = store.shadow_summary()
    assert s["n"] == 2
    assert s["diverged"] == 1
    assert s["live_trades"] == 1
    assert s["shadow_trades"] == 2
    store.close()
