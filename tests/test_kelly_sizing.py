"""Kelly + risk-eşitleme (vol-hedef) pozisyon boyutlama — kazanma matematiğine bağlar.

Korkuluklar: çarpan [0.25, 1.5] kıstırılır, gürültüde nötr (1.0x), trade_usdt tabanı
korunur. Edge belirsizse Kelly devreye girmez (gürültüden aşırı-bahis önleme).
"""

from __future__ import annotations

import pytest

import trader


def _c(pnl, day=1):
    return {"pnl": pnl, "closed_at": f"2026-06-{day:02d}T10:00:00+00:00"}


# ── _kelly_fraction: f* = W − (1−W)/R ───────────────────────────────────────
def test_kelly_fraction_positive_edge():
    # %60 kazanma, payoff 2.0 → f* = 0.6 − 0.4/2 = 0.4
    trader.S.kelly_min_trades = 10
    closed = [_c(20.0) for _ in range(6)] + [_c(-10.0) for _ in range(4)]
    k = trader._kelly_fraction(closed)
    assert k["ready"] is True
    assert k["win_rate"] == 0.6
    assert k["payoff"] == 2.0
    assert abs(k["f_star"] - 0.4) < 0.001


def test_kelly_fraction_needs_min_trades():
    trader.S.kelly_min_trades = 20
    closed = [_c(20.0) for _ in range(6)] + [_c(-10.0) for _ in range(4)]  # n=10 < 20
    k = trader._kelly_fraction(closed)
    assert k["ready"] is False
    assert k["n"] == 10


def test_kelly_fraction_negative_edge_not_ready():
    # kaybeden sistem (f* < 0) → ready False (boyut şişirilmez)
    trader.S.kelly_min_trades = 10
    closed = [_c(5.0) for _ in range(3)] + [_c(-10.0) for _ in range(7)]
    k = trader._kelly_fraction(closed)
    assert k["f_star"] < 0
    assert k["ready"] is False


def test_kelly_fraction_no_losses_not_ready():
    trader.S.kelly_min_trades = 5
    k = trader._kelly_fraction([_c(5.0) for _ in range(10)])
    assert k["ready"] is False  # payoff tanımsız (kayıp yok)


# ── _kelly_multiplier: korkuluklar ──────────────────────────────────────────
def test_kelly_multiplier_neutral_when_not_ready(monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_c(5.0), _c(-3.0)])  # yetersiz örnek
    trader.S.kelly_min_trades = 20
    assert trader._kelly_multiplier() == 1.0


def test_kelly_multiplier_scales_with_edge(monkeypatch):
    trader.S.kelly_min_trades = 10
    trader.S.kelly_fraction = 0.25
    monkeypatch.setattr(trader, "_closed",
                        [_c(20.0) for _ in range(6)] + [_c(-10.0) for _ in range(4)])
    # f*=0.4, çeyrek-Kelly → 1.0 + 0.4*0.25 = 1.1x
    assert abs(trader._kelly_multiplier() - 1.1) < 0.001


def test_kelly_multiplier_clamped(monkeypatch):
    trader.S.kelly_min_trades = 10
    trader.S.kelly_fraction = 1.0  # tam-Kelly
    # neredeyse hep kazanan → büyük f* ama çarpan 1.5'i aşamaz
    monkeypatch.setattr(trader, "_closed",
                        [_c(30.0) for _ in range(19)] + [_c(-5.0)])
    assert trader._kelly_multiplier() <= trader._KELLY_MAX_MULT


# ── _risk_parity_factor: SL mesafesine göre boyut ───────────────────────────
def test_risk_parity_neutral_without_stop():
    assert trader._risk_parity_factor(100.0, None) == 1.0
    assert trader._risk_parity_factor(100.0, 0.0) == 1.0


def test_risk_parity_shrinks_wide_stop():
    trader.S.target_risk_usdt = 3.0  # hedef: 3 USDT risk
    # 100 USDT × 6% SL = 6 USDT risk → hedef 3 → 0.5x (boyutu yarıla)
    assert trader._risk_parity_factor(100.0, 6.0) == 0.5
    trader.S.target_risk_usdt = 0.0


def test_risk_parity_grows_tight_stop():
    trader.S.target_risk_usdt = 6.0
    # 100 USDT × 3% = 3 USDT risk → hedef 6 → 2.0x ama [.., 1.5] clamp → 1.5
    assert trader._risk_parity_factor(100.0, 3.0) == trader._KELLY_MAX_MULT
    trader.S.target_risk_usdt = 0.0


def test_risk_parity_default_target_from_settings():
    trader.S.target_risk_usdt = 0.0
    trader.S.trade_usdt = 100.0
    trader.S.stop_loss_pct = 3.0  # default hedef = 100 × 3% = 3 USDT
    # aynı SL → 1.0x (değişiklik yok)
    assert trader._risk_parity_factor(100.0, 3.0) == 1.0


# ── _effective_stop_pct: tek doğruluk kaynağı ───────────────────────────────
def test_effective_stop_fixed():
    trader.S.use_atr_exits = False
    trader.S.stop_loss_pct = 3.0
    assert trader._effective_stop_pct(None) == 3.0
    assert trader._effective_stop_pct(5.0) == 3.0  # ATR kapalı → sabit


def test_effective_stop_atr():
    trader.S.use_atr_exits = True
    trader.S.atr_sl_mult = 1.5
    assert trader._effective_stop_pct(4.0) == 6.0  # 1.5 × 4%
    assert trader._effective_stop_pct(20.0) == 15.0  # clamp üst
    assert trader._effective_stop_pct(0.1) == 0.5  # clamp alt
    trader.S.use_atr_exits = False


# ── Entegrasyon: auto_decision boyut zinciri ────────────────────────────────
class _Item:
    def __init__(self, impact=9):
        self.impact = impact
        self.direction = "bullish"
        self.symbol = "FOOUSDT"
        self.confirmed = True
        self.atr_pct = None


@pytest.fixture()
def sizing_env(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader.S.auto_min_impact = 7
    trader.S.auto_require_confirm = True
    trader.S.market = "spot"
    trader.S.trade_usdt = 100.0
    trader.S.size_by_impact = False
    trader.S.size_by_kelly = False
    trader.S.size_by_volume = False
    trader.S.risk_parity = False
    trader.S.reduce_after_losses = 0
    trader.S.suppress_losing_sources = False
    trader.S.use_learned_vetoes = False
    trader.S.skip_already_priced_pct = 0.0
    trader.S.min_rel_volume = 0.0
    trader.S.max_same_direction = 0
    yield
    trader.S.size_by_kelly = False
    trader.S.risk_parity = False


def test_auto_decision_kelly_applied(sizing_env, monkeypatch):
    trader.S.size_by_kelly = True
    trader.S.kelly_min_trades = 10
    trader.S.kelly_fraction = 0.25
    monkeypatch.setattr(trader, "_closed",
                        [_c(20.0) for _ in range(6)] + [_c(-10.0) for _ in range(4)])
    d = trader.auto_decision(_Item())
    assert d["would_trade"] is True
    assert d["usdt"] == 110.0  # 100 × 1.1 (Kelly)


def test_auto_decision_risk_parity_applied(sizing_env):
    trader.S.risk_parity = True
    trader.S.target_risk_usdt = 1.5  # hedef düşük → SL 3%'te 100×3%=3 > 1.5 → 0.5x
    trader.S.use_atr_exits = False
    trader.S.stop_loss_pct = 3.0
    d = trader.auto_decision(_Item())
    assert d["usdt"] == 50.0  # 100 × 0.5
    trader.S.target_risk_usdt = 0.0


def test_auto_decision_no_change_when_disabled(sizing_env):
    d = trader.auto_decision(_Item())
    assert d["usdt"] == 100.0  # hiçbiri açık değil → taban
