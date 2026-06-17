"""Hacim Beyni — RVOL hesabı, likidite-katmanlı boyut, RVOL kapısı, orderbook payı.

Profesyonel haber-trade hacim mantığı: haber + fiyat + HACİM birlikte = gerçek;
hacimsiz hareket = fake. İnce coinde küçül (çıkış-tuzağı). Hepsi opt-in (preset/ayar).
"""

from __future__ import annotations

import pytest

import news_bot
import trader


# ── RVOL (göreceli hacim) hesabı ────────────────────────────────────────────
def _candle(qv: float) -> list:
    """Quote-hacmi index 7'de olan minimal bir kline satırı."""
    return [0, 0, 0, 0, 0, 0, 0, qv]


def test_rvol_surge_in_forming_candle():
    # baz 100, oluşan son mum 300 → 3x
    candles = [_candle(100)] * 4 + [_candle(100), _candle(300)]
    assert news_bot._compute_rvol(candles) == 3.0


def test_rvol_surge_in_just_closed_candle():
    # sürüş yeni kapanan mumda (sondan ikinci); oluşan mum henüz küçük → yine yakalanır
    candles = [_candle(100)] * 4 + [_candle(300), _candle(100)]
    assert news_bot._compute_rvol(candles) == 3.0


def test_rvol_no_surge():
    assert news_bot._compute_rvol([_candle(100)] * 6) == 1.0


def test_rvol_too_few_candles():
    assert news_bot._compute_rvol([_candle(100)] * 3) == 0.0
    assert news_bot._compute_rvol([]) == 0.0


def test_rvol_zero_baseline_is_safe():
    # önceki tüm mumlar sıfır hacim → bölme yok, 0 döner
    candles = [_candle(0)] * 4 + [_candle(0), _candle(500)]
    assert news_bot._compute_rvol(candles) == 0.0


def test_rvol_malformed_rows_ignored():
    # bozuk satır (index 7 yok) patlatmaz
    candles = [_candle(100), _candle(100), [1, 2], _candle(100), _candle(100), _candle(400)]
    assert news_bot._compute_rvol(candles) == 4.0


# ── Likidite-katmanlı boyut çarpanı ─────────────────────────────────────────
def test_liquidity_factor_tiers():
    assert trader._liquidity_factor(100_000_000) == 1.0   # derin
    assert trader._liquidity_factor(50_000_000) == 1.0
    assert trader._liquidity_factor(30_000_000) == 0.8
    assert trader._liquidity_factor(10_000_000) == 0.8
    assert trader._liquidity_factor(7_000_000) == 0.6
    assert trader._liquidity_factor(5_000_000) == 0.6
    assert trader._liquidity_factor(2_000_000) == 0.4
    assert trader._liquidity_factor(1_000_000) == 0.4
    assert trader._liquidity_factor(500_000) == 0.25       # çok ince


def test_liquidity_factor_unknown_is_cautious():
    assert trader._liquidity_factor(None) == 0.5
    assert trader._liquidity_factor(0) == 0.5


# ── auto_decision: hacim boyutu + RVOL kapısı (yan etkisiz karar) ────────────
class _Item:
    def __init__(self, impact=8, direction="bullish", volume_usd=None, rel_volume=None):
        self.impact = impact
        self.direction = direction
        self.symbol = "FOOUSDT"
        self.confirmed = True
        self.source = "TestSrc"
        self.volume_usd = volume_usd
        self.rel_volume = rel_volume
        self.price_24h_pct = 0.0


@pytest.fixture()
def decide_env(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader._halt.update(active=False, reason="", since="")   # operasyonel durdurma kapalı
    trader.S.auto_min_impact = 7
    trader.S.auto_require_confirm = True
    trader.S.market = "spot"
    trader.S.trade_usdt = 100.0
    trader.S.size_by_impact = False
    trader.S.size_by_volume = False
    trader.S.min_rel_volume = 0.0
    trader.S.reduce_after_losses = 0
    trader.S.suppress_losing_sources = False
    trader.S.skip_already_priced_pct = 0.0
    trader.S.tier1_skip_confirm_impact = 0
    trader.S.max_same_direction = 0
    trader.S.max_news_age_sec = 0
    yield
    for k in ("size_by_impact", "size_by_volume"):
        setattr(trader.S, k, False)
    trader.S.min_rel_volume = 0.0


def test_volume_sizing_scales_down_thin_coin(decide_env):
    trader.S.size_by_volume = True
    d = trader.auto_decision(_Item(impact=8, volume_usd=2_000_000))  # 0.4x
    assert d["would_trade"] and d["usdt"] == 40.0
    d = trader.auto_decision(_Item(impact=8, volume_usd=100_000_000))  # 1.0x
    assert d["usdt"] == 100.0


def test_volume_and_conviction_compose(decide_env):
    trader.S.size_by_impact = True
    trader.S.size_by_volume = True
    # güç 10 (1.5x) × ince coin 2M (0.4x) = 100 * 0.6 = 60
    d = trader.auto_decision(_Item(impact=10, volume_usd=2_000_000))
    assert d["usdt"] == 60.0


def test_rvol_gate_blocks_low_volume(decide_env):
    trader.S.min_rel_volume = 1.5
    d = trader.auto_decision(_Item(impact=9, rel_volume=1.0))
    assert not d["would_trade"] and "hacim zayıf" in d["reason"]


def test_rvol_gate_passes_on_surge(decide_env):
    trader.S.min_rel_volume = 1.5
    d = trader.auto_decision(_Item(impact=9, rel_volume=2.3))
    assert d["would_trade"]


def test_rvol_gate_passes_when_no_data(decide_env):
    # RVOL verisi yok (None) → engelleme (eksik veri ≠ düşük hacim)
    trader.S.min_rel_volume = 1.5
    d = trader.auto_decision(_Item(impact=9, rel_volume=None))
    assert d["would_trade"]


# ── Preset entegrasyonu ─────────────────────────────────────────────────────
def test_news_preset_enables_volume_brain():
    assert trader.PRESETS["news"]["size_by_volume"] is True
    assert trader.PRESETS["news"]["min_rel_volume"] == 1.5
    assert trader.PRESETS["news"]["max_book_frac"] == 0.10
    # safe preset hepsini kapatır
    assert trader.PRESETS["safe"]["size_by_volume"] is False
    assert trader.PRESETS["safe"]["min_rel_volume"] == 0.0


def test_new_settings_persisted():
    for k in ("size_by_volume", "min_rel_volume", "max_book_frac"):
        assert k in trader._PERSIST_KEYS
