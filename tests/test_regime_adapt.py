"""Rejim oto-adaptasyon: bozulmada eşiği geçici sıkılaştır, toparlanınca geri al.

Korkuluklar: yalnız auto_min_impact'e dokunur (para-büyüklüğü/risk değil); tek seferde
+1, tavan +2; orijinal eşik saklanır ve toparlanınca aynen geri yüklenir. Opt-in.
"""

from __future__ import annotations

import pytest

import trader


def _t(pnl, day, hour=10):
    return {"pnl": pnl, "impact": 9, "side": "long", "symbol": "AUSDT",
            "news_source": "X", "rel_volume": 2.0,
            "opened_at": f"2026-06-{day:02d}T{hour:02d}:00:00+00:00",
            "closed_at": f"2026-06-{day:02d}T{hour:02d}:30:00+00:00"}


@pytest.fixture()
def regime_env(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    trader._regime_state.update({"active": False, "restore": None, "bump": 0, "since": ""})
    trader.S.regime_adapt = True
    trader.S.auto_min_impact = 8
    yield
    trader.S.regime_adapt = False
    trader._regime_state.update({"active": False, "restore": None, "bump": 0, "since": ""})


def _deteriorating():
    # eski yarı güçlü pozitif, son yarı güçlü negatif → shifted & improving=False
    return [_t(8.0, 1 + i) for i in range(8)] + [_t(-8.0, 15 + i) for i in range(8)]


def _stable():
    return [_t(3.0, 1 + i % 28) for i in range(16)]


# ── Opt-in kapısı ───────────────────────────────────────────────────────────
def test_disabled_no_op(monkeypatch):
    trader.S.regime_adapt = False
    monkeypatch.setattr(trader, "_closed", _deteriorating())
    out = trader.regime_adapt_step()
    assert out["acted"] is False
    assert "kapalı" in out["reason"]


def test_insufficient_samples(regime_env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_t(1.0, 1), _t(2.0, 2)])
    out = trader.regime_adapt_step()
    assert out["acted"] is False


# ── Bozulma → sıkılaştır ─────────────────────────────────────────────────────
def test_deterioration_tightens(regime_env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", _deteriorating())
    out = trader.regime_adapt_step()
    assert out["acted"] is True
    assert out["state"] == "tighten"
    assert trader.S.auto_min_impact == 9       # 8 → +1
    assert trader._regime_state["active"] is True
    assert trader._regime_state["restore"] == 8  # orijinal saklandı


def test_tighten_caps_at_max_bump(regime_env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", _deteriorating())
    trader.regime_adapt_step()  # 8 → 9 (bump 1)
    trader.regime_adapt_step()  # 9 → 10 (bump 2, tavan)
    assert trader.S.auto_min_impact == 10
    out = trader.regime_adapt_step()  # tavan: artık sıkılaştırma yok
    assert out["acted"] is False
    assert out["state"] == "tightened-max"
    assert trader.S.auto_min_impact == 10


# ── Toparlanma → geri al ─────────────────────────────────────────────────────
def test_recovery_restores(regime_env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", _deteriorating())
    trader.regime_adapt_step()
    assert trader.S.auto_min_impact == 9
    # piyasa toparlandı → stabil veri
    monkeypatch.setattr(trader, "_closed", _stable())
    out = trader.regime_adapt_step()
    assert out["acted"] is True
    assert out["state"] == "restore"
    assert trader.S.auto_min_impact == 8       # orijinale döndü
    assert trader._regime_state["active"] is False


def test_stable_no_action(regime_env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", _stable())
    out = trader.regime_adapt_step()
    assert out["acted"] is False
    assert out["state"] == "stable"
    assert trader.S.auto_min_impact == 8       # dokunulmadı


# ── Korkuluk: sadece eşiğe dokunur ──────────────────────────────────────────
def test_only_threshold_touched(regime_env, monkeypatch):
    trader.S.trade_usdt = 100.0
    trader.S.leverage = 1
    trader.S.max_total_exposure_usdt = 2000.0
    monkeypatch.setattr(trader, "_closed", _deteriorating())
    trader.regime_adapt_step()
    # para-büyüklüğü/risk tavanları DEĞİŞMEDİ
    assert trader.S.trade_usdt == 100.0
    assert trader.S.leverage == 1
    assert trader.S.max_total_exposure_usdt == 2000.0


def test_get_regime_state(regime_env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", _deteriorating())
    trader.regime_adapt_step()
    st = trader.get_regime_state()
    assert st["enabled"] is True
    assert st["active"] is True
    assert st["bump"] == 1
    assert st["restore"] == 8
