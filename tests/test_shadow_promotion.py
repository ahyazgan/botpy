"""Shadow terfi önerisi: gölge kararların SANAL sonucundan aday-ayar edge → ÖNERİ.

OTO-TERFİ DEĞİL — yalnız öneri (kontrol kullanıcıda). Sadece divergence'larda sonuç
sayılır; aday tutarlı daha iyi net %% + yeterli örnek → recommend=True.
"""

from __future__ import annotations

import trader


def _row(diverged, live_trade, shadow_trade, outcome_pct):
    return {"diverged": diverged, "live_trade": live_trade,
            "shadow_trade": shadow_trade, "outcome_pct": outcome_pct}


def test_not_ready_below_min():
    rows = [_row(True, False, True, 2.0) for _ in range(5)]  # 5 < 10
    out = trader.shadow_promotion_advice(rows)
    assert out["ready"] is False
    assert out["recommend"] is False


def test_ignores_non_diverged_and_none_outcome():
    rows = ([_row(False, True, True, 5.0) for _ in range(20)]      # diverged değil → sayılmaz
            + [_row(True, False, True, None) for _ in range(20)])  # sonuç yok → sayılmaz
    out = trader.shadow_promotion_advice(rows)
    assert out["ready"] is False
    assert out["n"] == 0


def test_recommends_when_shadow_better():
    # aday GİRER (kazanır +3%), canlı girmez (0) → aday tutarlı daha iyi
    rows = [_row(True, False, True, 3.0) for _ in range(12)]
    out = trader.shadow_promotion_advice(rows)
    assert out["ready"] is True
    assert out["shadow_avg"] == 3.0
    assert out["live_avg"] == 0.0
    assert out["edge_pct"] == 3.0
    assert out["recommend"] is True


def test_no_recommend_when_shadow_loses():
    # aday GİRER ama o sinyaller KAYBEDİYOR (-2%); canlı akıllıca girmedi (0)
    rows = [_row(True, False, True, -2.0) for _ in range(12)]
    out = trader.shadow_promotion_advice(rows)
    assert out["ready"] is True
    assert out["edge_pct"] < 0
    assert out["recommend"] is False   # aday daha kötü → terfi etme


def test_live_better_no_recommend():
    # canlı GİRER kazanır (+4), aday girmez → canlı daha iyi
    rows = [_row(True, True, False, 4.0) for _ in range(12)]
    out = trader.shadow_promotion_advice(rows)
    assert out["live_avg"] == 4.0
    assert out["shadow_avg"] == 0.0
    assert out["recommend"] is False


def test_edge_below_threshold_no_recommend():
    # aday marjinal daha iyi (+0.3%) ama eşik 0.5% → terfi etme (gürültü olabilir)
    rows = [_row(True, False, True, 0.3) for _ in range(12)]
    out = trader.shadow_promotion_advice(rows)
    assert 0 < out["edge_pct"] < trader._SHADOW_PROMOTE_EDGE_PCT
    assert out["recommend"] is False
