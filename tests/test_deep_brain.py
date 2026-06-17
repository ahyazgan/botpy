"""Öğrenen Beyin v3 (derin) — istatistiksel anlamlılık, recency/rejim, koşullu edge,
segment SL/TP (MFE/MAE). Beyin artık gürültüyü öğrenmez, etkileşimi yakalar, rejim
kaymasını görür, çıkış parametrelerini gerçekleşen fiyat hareketinden öğrenir.
"""

from __future__ import annotations

import trader


def _t(pnl, *, impact=9, side="long", symbol="AUSDT", source="X", rvol=2.0,
       mae=None, mfe=None, day=1):
    return {"pnl": pnl, "impact": impact, "side": side, "symbol": symbol,
            "news_source": source, "rel_volume": rvol, "mae_pct": mae, "mfe_pct": mfe,
            "opened_at": f"2026-06-{day:02d}T10:00:00+00:00",
            "closed_at": f"2026-06-{day:02d}T11:00:00+00:00"}


# ── A: İstatistiksel anlamlılık ─────────────────────────────────────────────
def test_expectancy_ci_significance():
    assert trader._expectancy_ci([3.0] * 10)["significant"] is True       # tutarlı poz
    assert trader._expectancy_ci([-50, 40, -30, 30])["significant"] is False  # gürültü
    assert trader._expectancy_ci([])["significant"] is False
    assert trader._expectancy_ci([5.0])["significant"] is False           # n=1


def test_wilson_lower_bound():
    assert trader._wilson_lo(0, 0) == 0.0
    # az örnek → ihtiyatlı (alt sınır gerçek orandan düşük)
    assert trader._wilson_lo(3, 4) < 0.75
    # çok örnek → orana yaklaşır
    assert trader._wilson_lo(80, 100) > 0.7


def test_bucket_has_confidence_fields():
    out = trader._bucket_stats([{"pnl": 2.0}] * 6, lambda c: "k")
    d = out["k"]
    for f in ("ci_lo", "ci_hi", "significant", "wilson_lo"):
        assert f in d


def test_noise_not_suppressed_but_real_is():
    noisy = [_t(v, source="NOISE") for v in [-50, 40, -30, 30]]
    real = [_t(-3.0, source="REAL") for _ in range(12)]
    out = trader._suggest_from_trades(noisy + real, value_key="pnl",
                                      source_key="news_source", tier_of=None, unit=" USDT")
    sup = {s["source"] for s in out["suggestions"] if s["type"] == "suppress_source"}
    assert "REAL" in sup and "NOISE" not in sup


# ── C: Recency + rejim ──────────────────────────────────────────────────────
def test_regime_shift_detected():
    old = [_t(5.0, day=1 + i) for i in range(8)]
    new = [_t(-5.0, day=15 + i % 9) for i in range(8)]
    r = trader._regime_check(old + new)
    assert r["ready"] and r["shifted"] and r["improving"] is False


def test_regime_stable_when_consistent():
    r = trader._regime_check([_t(3.0, day=1 + i % 28) for i in range(16)])
    assert r["ready"] and r["shifted"] is False


# ── B: Koşullu edge + öğrenilen-veto ────────────────────────────────────────
def test_conditional_edge_finds_interaction():
    # X kaynağı marjinalde nötr ama düşük-RVOL'de kaybeder, yüksekte kazanır (Simpson)
    trades = ([_t(-4.0, rvol=0.7) for _ in range(6)]
              + [_t(4.0, rvol=2.5) for _ in range(6)])
    out = trader._suggest_from_trades(trades, value_key="pnl",
                                      source_key="news_source", tier_of=None, unit=" USDT")
    assert abs(out["by_source"]["X"]["avg_pnl"]) < 0.01      # marjinal nötr
    conds = {e["condition"] for e in out["conditional_edges"]}
    assert any("<1.0" in c for c in conds)                   # koşullu negatif yakalandı


def test_learned_veto_blocks_only_bad_segment(monkeypatch):
    trades = ([_t(-4.0, rvol=0.7) for _ in range(6)]
              + [_t(4.0, rvol=2.5) for _ in range(6)])
    monkeypatch.setattr(trader, "_closed", trades)
    assert trader.refresh_learned_vetoes() >= 1

    class _I:
        def __init__(self, rv):
            self.source, self.rel_volume, self.impact = "X", rv, 9
            self.direction, self.symbol = "bullish", "AUSDT"
    assert trader._learned_veto_hit(_I(0.7)) is not None     # kötü segment → veto
    assert trader._learned_veto_hit(_I(2.5)) is None         # iyi segment → serbest


# ── D: Segment SL/TP (MFE/MAE) ──────────────────────────────────────────────
def test_sl_tp_learned_from_mfe_mae():
    # Kazananlar 4.5% dipliyor (SL 3% çok sıkı), medyan MFE 8% (TP 6% düşük)
    trades = [_t(5.0, mae=4.5, mfe=8.0) for _ in range(12)]
    monkeypatch_sl = trader.S.stop_loss_pct
    trader.S.stop_loss_pct, trader.S.take_profit_pct = 3.0, 6.0
    try:
        out = trader._suggest_from_trades(trades, value_key="pnl",
                                          source_key="news_source", tier_of=None, unit=" USDT")
        types = {s["type"] for s in out["suggestions"]}
        assert "stop_loss_pct" in types and "take_profit_pct" in types
        sl = next(s for s in out["suggestions"] if s["type"] == "stop_loss_pct")
        assert sl["suggested"] > 3.0                          # gevşet (kazananları koru)
    finally:
        trader.S.stop_loss_pct = monkeypatch_sl
        trader.S.take_profit_pct = 6.0


def test_apply_sl_tp_clamped(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(trader.S, "stop_loss_pct", 3.0)
    monkeypatch.setattr(trader.S, "take_profit_pct", 6.0)
    trader.apply_tuning({"ready": True, "samples": 12, "suggestions": [
        {"type": "stop_loss_pct", "suggested": 99}, {"type": "take_profit_pct", "suggested": 99}]})
    assert trader.S.stop_loss_pct == 15.0 and trader.S.take_profit_pct == 30.0
