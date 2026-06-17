"""Portföy-seviye risk: korelasyon-farkında boyut (açık pozisyonlarla koreleyse "tek bahis").

Korkuluklar: factor [0.25, 1.0]; veri yoksa nötr (heat=1, factor=1.0); ters-yön korelasyon
hedge sayılır (ısı düşürür). price_series çağırandan gelir → auto_decision saf/ağsız kalır.
"""

from __future__ import annotations

import pytest

import trader


# ── _returns / _corr ────────────────────────────────────────────────────────
def test_returns_basic():
    r = trader._returns([100.0, 110.0, 99.0])
    assert r[0] == pytest.approx(0.1)
    assert r[1] == pytest.approx(-0.1)


def test_returns_skips_zero():
    assert trader._returns([0.0, 100.0]) == []   # sıfır baz atlanır
    assert trader._returns([100.0]) == []         # tek nokta → boş


def test_corr_perfect_positive():
    a = [0.1, -0.2, 0.3, -0.1]
    assert trader._corr(a, a) == 1.0


def test_corr_perfect_negative():
    a = [0.1, -0.2, 0.3, -0.1]
    b = [-x for x in a]
    assert trader._corr(a, b) == -1.0


def test_corr_insufficient():
    assert trader._corr([0.1, 0.2], [0.1, 0.2]) is None   # <3


# ── _portfolio_heat ──────────────────────────────────────────────────────────
@pytest.fixture()
def heat_env(monkeypatch):
    trader.S.corr_threshold = 0.6
    trader.S.max_portfolio_heat = 2.5
    yield


def test_heat_no_open_positions(heat_env, monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    out = trader._portfolio_heat("NEWUSDT", "long", {"NEWUSDT": [0.1, -0.2, 0.3, -0.1]})
    assert out["heat"] == 1.0       # sadece yeni pozisyon
    assert out["factor"] == 1.0
    assert out["n_open"] == 0


def test_heat_correlated_same_direction_raises_heat(heat_env, monkeypatch):
    # 3 açık long, hepsi yeni adayla yüksek korelasyon → ısı tavanı aşılır → kıs
    ser = [0.1, -0.2, 0.3, -0.1, 0.2]
    monkeypatch.setattr(trader, "_positions", [
        {"symbol": "AUSDT", "side": "long"}, {"symbol": "BUSDT", "side": "long"},
        {"symbol": "CUSDT", "side": "long"}])
    series = {"NEWUSDT": ser, "AUSDT": ser, "BUSDT": ser, "CUSDT": ser}
    out = trader._portfolio_heat("NEWUSDT", "long", series)
    assert out["heat"] > 2.5        # 1 + 3×1.0 = 4
    assert out["factor"] < 1.0      # tavan aşıldı → kıs
    assert len(out["correlated"]) == 3


def test_heat_opposite_direction_is_hedge(heat_env, monkeypatch):
    # açık short, yeni long, pozitif fiyat-korelasyon → hedge (ısı DÜŞER, çarpan nötr)
    ser = [0.1, -0.2, 0.3, -0.1, 0.2]
    monkeypatch.setattr(trader, "_positions", [{"symbol": "AUSDT", "side": "short"}])
    out = trader._portfolio_heat("NEWUSDT", "long", {"NEWUSDT": ser, "AUSDT": ser})
    assert out["heat"] < 1.0        # hedge ısıyı düşürür
    assert out["factor"] == 1.0     # tavan altı → kıs yok


def test_heat_missing_data_neutral(heat_env, monkeypatch):
    monkeypatch.setattr(trader, "_positions", [{"symbol": "AUSDT", "side": "long"}])
    # AUSDT serisi yok → korelasyon hesaplanamaz → o pozisyon atlanır
    out = trader._portfolio_heat("NEWUSDT", "long", {"NEWUSDT": [0.1, -0.2, 0.3]})
    assert out["heat"] == 1.0
    assert out["factor"] == 1.0


# ── Entegrasyon: auto_decision portföy çarpanı ──────────────────────────────
class _Item:
    def __init__(self):
        self.impact = 9
        self.direction = "bullish"
        self.symbol = "NEWUSDT"
        self.confirmed = True
        self.atr_pct = None


@pytest.fixture()
def auto_env(monkeypatch):
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader.S.auto_min_impact = 7
    trader.S.auto_require_confirm = True
    trader.S.market = "spot"
    trader.S.trade_usdt = 100.0
    trader.S.size_by_impact = False
    trader.S.size_by_kelly = False
    trader.S.size_by_volume = False
    trader.S.risk_parity = False
    trader.S.portfolio_risk = True
    trader.S.corr_threshold = 0.6
    trader.S.max_portfolio_heat = 2.5
    trader.S.reduce_after_losses = 0
    trader.S.suppress_losing_sources = False
    trader.S.use_learned_vetoes = False
    trader.S.skip_already_priced_pct = 0.0
    trader.S.min_rel_volume = 0.0
    trader.S.max_same_direction = 0
    yield
    trader.S.portfolio_risk = False


def test_auto_decision_portfolio_shrinks(auto_env, monkeypatch):
    ser = [0.1, -0.2, 0.3, -0.1, 0.2]
    monkeypatch.setattr(trader, "_positions", [
        {"symbol": "AUSDT", "side": "long"}, {"symbol": "BUSDT", "side": "long"},
        {"symbol": "CUSDT", "side": "long"}])
    series = {"NEWUSDT": ser, "AUSDT": ser, "BUSDT": ser, "CUSDT": ser}
    d = trader.auto_decision(_Item(), price_series=series)
    assert d["would_trade"] is True
    assert d["usdt"] < 100.0        # korelasyon-yükü → kısıldı
    assert "portfolio_heat" in d


def test_auto_decision_no_series_neutral(auto_env, monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    d = trader.auto_decision(_Item(), price_series=None)
    assert d["usdt"] == 100.0        # seri yok → portföy-risk nötr
    assert "portfolio_heat" not in d
