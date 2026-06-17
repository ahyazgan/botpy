"""Sinyal kalitesi derinliği: impact-ölçekli RVOL eşiği + başlık↔gövde çelişki tespiti.

Yüksek-güç haber daha çok hacim bekler (gerçek büyük haber piyasayı oransal hareketlendirir);
başlık gövdeyle çelişiyorsa (clickbait) impact deterministik tavanla kıstırılır.
"""

from __future__ import annotations

import pytest

import trader


# ── _required_rvol: impact-ölçekli eşik ─────────────────────────────────────
def test_required_rvol_disabled_is_flat():
    trader.S.rvol_scale_by_impact = False
    trader.S.min_rel_volume = 1.5
    assert trader._required_rvol(10) == 1.5  # ölçekleme kapalı → sabit
    assert trader._required_rvol(7) == 1.5


def test_required_rvol_zero_base():
    trader.S.rvol_scale_by_impact = True
    trader.S.min_rel_volume = 0.0
    assert trader._required_rvol(10) == 0.0  # taban 0 → kapı kapalı


def test_required_rvol_scales_with_impact():
    trader.S.rvol_scale_by_impact = True
    trader.S.min_rel_volume = 2.0
    # impact 8 = taban; her puan üstü +%15
    assert trader._required_rvol(8) == 2.0
    assert trader._required_rvol(10) == round(2.0 * 1.30, 2)  # +%30
    assert trader._required_rvol(6) == round(2.0 * 0.70, 2)   # −%30 (gevşek)
    trader.S.rvol_scale_by_impact = False


def test_required_rvol_clamped():
    trader.S.rvol_scale_by_impact = True
    trader.S.min_rel_volume = 2.0
    # aşırı yüksek/düşük impact → taban×[0.5, 2.0] kıstırması
    assert trader._required_rvol(20) == 4.0   # taban×2 tavan
    assert trader._required_rvol(0) == 1.0    # taban×0.5 zemin
    trader.S.rvol_scale_by_impact = False


# ── Entegrasyon: auto_decision RVOL kapısı impact-ölçekli ───────────────────
class _Item:
    def __init__(self, impact=10, rvol=2.0):
        self.impact = impact
        self.direction = "bullish"
        self.symbol = "FOOUSDT"
        self.confirmed = True
        self.rel_volume = rvol
        self.atr_pct = None


@pytest.fixture()
def rvol_env(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader.S.auto_min_impact = 7
    trader.S.auto_require_confirm = True
    trader.S.market = "spot"
    trader.S.min_rel_volume = 2.0
    trader.S.rvol_scale_by_impact = True
    trader.S.size_by_impact = False
    trader.S.size_by_kelly = False
    trader.S.size_by_volume = False
    trader.S.risk_parity = False
    trader.S.reduce_after_losses = 0
    trader.S.suppress_losing_sources = False
    trader.S.use_learned_vetoes = False
    trader.S.skip_already_priced_pct = 0.0
    trader.S.max_same_direction = 0
    yield
    trader.S.rvol_scale_by_impact = False
    trader.S.min_rel_volume = 0.0


def test_high_impact_needs_more_rvol(rvol_env):
    # impact 10 → gereken 2.6x; RVOL 2.0 yetersiz → reddet
    d = trader.auto_decision(_Item(impact=10, rvol=2.0))
    assert d["would_trade"] is False
    assert "hacim zayıf" in d["reason"]


def test_high_impact_passes_with_enough_rvol(rvol_env):
    d = trader.auto_decision(_Item(impact=10, rvol=3.0))  # 3.0 > 2.6 → geç
    assert d["would_trade"] is True


def test_low_impact_relaxed_rvol(rvol_env):
    # impact 7 → gereken 1.7x; RVOL 1.8 yeterli (sabit 2.0 olsa reddedilirdi)
    d = trader.auto_decision(_Item(impact=7, rvol=1.8))
    assert d["would_trade"] is True
