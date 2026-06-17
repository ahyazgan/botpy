"""Beyin kalibrasyon bilimi: Brier + ECE + reliability + eskalasyon-isabet + rubrik korelasyon.

Beyin conviction'ını kazanma-olasılığı tahmini gibi ölçer; aşırı-güveni (overconfidence)
ve rubrik alt-skorlarının sinyal taşıyıp taşımadığını nicel görür.
"""

from __future__ import annotations

import trader


def _bt(pnl, conviction, *, escalated=False, scores=None):
    """Beyinli kapanmış işlem."""
    return {"pnl": pnl, "brain": {"conviction": conviction, "escalated": escalated,
                                  "scores": scores}}


# ── _pearson ─────────────────────────────────────────────────────────────
def test_pearson_perfect_positive():
    assert trader._pearson([1, 2, 3, 4], [2, 4, 6, 8]) == 1.0


def test_pearson_perfect_negative():
    assert trader._pearson([1, 2, 3, 4], [8, 6, 4, 2]) == -1.0


def test_pearson_insufficient_or_flat():
    assert trader._pearson([1, 2], [1, 2]) is None       # <3 nokta
    assert trader._pearson([1, 1, 1], [1, 2, 3]) is None  # sıfır varyans x


# ── _calibration_science: Brier / ECE / reliability ────────────────────────
def test_brier_perfect_calibration():
    # conviction tam isabet: 1.0→kazanç, 0.0→kayıp → Brier=0
    pairs = [(1.0, 1), (1.0, 1), (0.0, 0), (0.0, 0)]
    sci = trader._calibration_science(pairs)
    assert sci["brier"] == 0.0
    assert sci["ece"] == 0.0


def test_brier_worst_calibration():
    # tam ters: yüksek conviction kaybeder → Brier=1
    pairs = [(1.0, 0), (1.0, 0)]
    sci = trader._calibration_science(pairs)
    assert sci["brier"] == 1.0


def test_overconfident_flag():
    # ort.conviction 0.9 ama isabet 0.5 → aşırı-güvenli
    pairs = [(0.9, 1), (0.9, 0), (0.9, 1), (0.9, 0)]
    sci = trader._calibration_science(pairs)
    assert sci["overconfident"] is True
    assert sci["mean_conviction"] == 0.9
    assert sci["base_rate"] == 0.5


def test_well_calibrated_not_overconfident():
    pairs = [(0.5, 1), (0.5, 0), (0.5, 1), (0.5, 0)]
    sci = trader._calibration_science(pairs)
    assert sci["overconfident"] is False  # 0.5 conviction = 0.5 isabet


def test_reliability_bins_structure():
    sci = trader._calibration_science([(0.1, 0), (0.9, 1), (0.9, 1)])
    assert len(sci["reliability"]) == 5
    # 0.0-0.2 bin'inde 1 nokta, 0.8-1.0 bin'inde 2 nokta
    assert sci["reliability"][0]["n"] == 1
    assert sci["reliability"][4]["n"] == 2
    assert sci["reliability"][4]["actual"] == 1.0


def test_calibration_empty():
    sci = trader._calibration_science([])
    assert sci["brier"] is None and sci["ece"] is None


# ── _escalation_accuracy ────────────────────────────────────────────────────
def test_escalation_accuracy_splits():
    rows = [_bt(10, 0.5, escalated=True), _bt(-5, 0.5, escalated=True),
            _bt(8, 0.9), _bt(6, 0.85)]
    out = trader._escalation_accuracy(rows)
    assert out["escalated"]["n"] == 2
    assert out["escalated"]["win_rate"] == 0.5
    assert out["base"]["n"] == 2
    assert out["base"]["win_rate"] == 1.0


# ── _rubric_correlation ─────────────────────────────────────────────────────
def test_rubric_correlation_detects_signal():
    # chase_risk yüksek → P&L düşük (negatif korelasyon, beklenen)
    rows = [_bt(-10, 0.6, scores={"chase_risk": 0.9, "fade_risk": 0.1, "liquidity": 0.5,
                                  "source_quality": 0.5, "correlation_risk": 0.1}),
            _bt(-8, 0.6, scores={"chase_risk": 0.8, "fade_risk": 0.1, "liquidity": 0.5,
                                 "source_quality": 0.5, "correlation_risk": 0.1}),
            _bt(10, 0.6, scores={"chase_risk": 0.1, "fade_risk": 0.1, "liquidity": 0.5,
                                 "source_quality": 0.5, "correlation_risk": 0.1}),
            _bt(8, 0.6, scores={"chase_risk": 0.2, "fade_risk": 0.1, "liquidity": 0.5,
                                "source_quality": 0.5, "correlation_risk": 0.1})]
    out = trader._rubric_correlation(rows)
    assert out["chase_risk"] is not None and out["chase_risk"] < 0  # yüksek chase=kötü
    # sabit skorlar (fade/liquidity/...) → None (varyans yok)
    assert out["fade_risk"] is None


# ── brain_scorecard entegrasyonu ────────────────────────────────────────────
def test_scorecard_includes_calibration_science(monkeypatch):
    rows = [_bt(10, 0.9, scores={"chase_risk": 0.1, "fade_risk": 0.1, "liquidity": 0.8,
                                 "source_quality": 0.8, "correlation_risk": 0.1})
            for _ in range(3)]
    rows += [_bt(-5, 0.3, scores={"chase_risk": 0.8, "fade_risk": 0.5, "liquidity": 0.3,
                                  "source_quality": 0.3, "correlation_risk": 0.5})
             for _ in range(3)]
    monkeypatch.setattr(trader, "_closed", rows)
    sc = trader.brain_scorecard()
    assert "brier" in sc and "ece" in sc and "reliability" in sc
    assert "escalation" in sc and "rubric" in sc
    assert sc["samples"] == 6
    assert sc["overconfident"] is not None
