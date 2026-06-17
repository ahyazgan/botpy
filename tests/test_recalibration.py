"""Conviction recalibration: ham beyin conviction'ını geçmiş isabetle düzelt.

Isotonic (PAV) eğri aşırı-güveni bastırır: conviction 0.9 ama o bantta gerçek win-rate
0.4 ise → ~0.4'e iner. Opt-in; yetersiz örnek → ham geçerli (gürültüden düzeltme yok).
"""

from __future__ import annotations

import trader


def _bt(pnl, conviction):
    return {"pnl": pnl, "brain": {"conviction": conviction}}


# ── _isotonic: PAV monoton eğri ─────────────────────────────────────────────
def test_isotonic_already_monotone():
    # düşük conv → kayıp, yüksek conv → kazanç (zaten monoton)
    pairs = [(0.2, 0), (0.4, 0), (0.7, 1), (0.9, 1)]
    curve = trader._isotonic(pairs)
    probs = [p for _, p in curve]
    assert probs == sorted(probs)  # azalmayan


def test_isotonic_pools_violation():
    # ihlal: yüksek conv kaybetmiş → komşu havuzlanır, monotonluk zorlanır
    pairs = [(0.3, 1), (0.3, 1), (0.9, 0), (0.9, 0)]
    curve = trader._isotonic(pairs)
    probs = [p for _, p in curve]
    assert probs == sorted(probs)
    # 0.9 bandı 0.3'ten yüksek olamaz (havuzlandı)
    assert trader._apply_calibration(0.9, curve) <= trader._apply_calibration(0.3, curve) + 0.01


def test_isotonic_empty():
    assert trader._isotonic([]) == []


# ── _apply_calibration: basamak ara değer ───────────────────────────────────
def test_apply_calibration_step():
    curve = [(0.2, 0.1), (0.5, 0.5), (0.8, 0.9)]
    assert trader._apply_calibration(0.1, curve) == 0.1  # ilk basamak altı
    assert trader._apply_calibration(0.6, curve) == 0.5  # 0.5 eşiği
    assert trader._apply_calibration(0.95, curve) == 0.9  # son basamak
    assert trader._apply_calibration(0.5, []) == 0.5     # boş eğri → aynen


# ── _fit_calibration: yeterlilik kapısı ─────────────────────────────────────
def test_fit_needs_min_samples():
    pairs = [(0.5, 1), (0.6, 0)]
    fit = trader._fit_calibration(pairs, min_n=20)
    assert fit["ready"] is False
    assert fit["n"] == 2


def test_fit_ready_with_enough():
    pairs = [(0.3, 0)] * 10 + [(0.8, 1)] * 10
    fit = trader._fit_calibration(pairs, min_n=20)
    assert fit["ready"] is True
    assert len(fit["curve"]) >= 2


# ── recalibrate_conviction: opt-in + aşırı-güven bastırma ────────────────────
def test_recalibrate_disabled(monkeypatch):
    trader.S.brain_recalibrate = False
    out = trader.recalibrate_conviction(0.9)
    assert out["adjusted"] is False
    assert out["value"] == 0.9


def test_recalibrate_insufficient(monkeypatch):
    trader.S.brain_recalibrate = True
    trader.S.brain_recalibrate_min = 20
    monkeypatch.setattr(trader, "_closed", [_bt(5, 0.8), _bt(-3, 0.4)])  # 2 < 20
    out = trader.recalibrate_conviction(0.8)
    assert out["adjusted"] is False
    assert out["value"] == 0.8
    trader.S.brain_recalibrate = False


def test_recalibrate_suppresses_overconfidence(monkeypatch):
    trader.S.brain_recalibrate = True
    trader.S.brain_recalibrate_min = 10
    # yüksek conviction'lı işlemler aslında çoğu kaybetmiş → 0.9 aşağı çekilmeli
    closed = ([_bt(-5, 0.9) for _ in range(7)] + [_bt(8, 0.9) for _ in range(3)]  # 0.9→%30
              + [_bt(6, 0.4) for _ in range(6)] + [_bt(-4, 0.4) for _ in range(4)])  # 0.4→%60
    monkeypatch.setattr(trader, "_closed", closed)
    out = trader.recalibrate_conviction(0.9)
    assert out["adjusted"] is True
    assert out["raw"] == 0.9
    assert out["value"] < 0.9   # aşırı-güven bastırıldı
    trader.S.brain_recalibrate = False
