"""Öğrenen beyin (öneri modu): trader.suggest_tuning + GET /tuning."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader


def _c(pnl, impact=9, news_source="Binance"):
    return {"pnl": pnl, "impact": impact, "news_source": news_source,
            "symbol": "FOOUSDT", "source": "auto", "close_reason": "take-profit"}


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(trader.S, "auto_min_impact", 7)
    monkeypatch.setattr(trader.S, "min_source_samples", 8)
    yield


def test_not_ready_below_min_samples(env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_c(1.0) for _ in range(5)])
    out = trader.suggest_tuning()
    assert out["ready"] is False and out["samples"] == 5 and out["suggestions"] == []


def test_suggests_raising_auto_min_impact(env, monkeypatch):
    # güç 7 net zarar, güç 9 net kâr → eşiği 7→9 yükselt önerisi
    closed = [_c(-3.0, impact=7) for _ in range(6)] + [_c(5.0, impact=9) for _ in range(6)]
    monkeypatch.setattr(trader, "_closed", closed)
    out = trader.suggest_tuning()
    assert out["ready"] is True
    s = [x for x in out["suggestions"] if x["type"] == "auto_min_impact"]
    assert s and s[0]["current"] == 7 and s[0]["suggested"] == 9


def test_suggests_suppress_negative_tier(env, monkeypatch):
    # sosyal kaynak net zarar (≥10 örnek) → tier kısma önerisi
    closed = [_c(-2.0, impact=9, news_source="⚡Twitter") for _ in range(12)]
    monkeypatch.setattr(trader, "_closed", closed)
    out = trader.suggest_tuning(tier_of=nb._source_tier)
    tiers = [x for x in out["suggestions"] if x["type"] == "suppress_tier"]
    assert tiers and tiers[0]["tier"] == "sosyal" and tiers[0]["avg_pnl"] < 0


def test_suggests_suppress_negative_source(env, monkeypatch):
    closed = [_c(-1.0, impact=9, news_source="ZayifKaynak") for _ in range(10)]
    monkeypatch.setattr(trader, "_closed", closed)
    out = trader.suggest_tuning()
    srcs = [x for x in out["suggestions"] if x["type"] == "suppress_source"]
    assert srcs and srcs[0]["source"] == "ZayifKaynak"


def test_suggest_is_side_effect_free(env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_c(-3.0, impact=7) for _ in range(6)]
                        + [_c(5.0, impact=9) for _ in range(6)])
    trader.suggest_tuning()
    assert trader.S.auto_min_impact == 7   # ayar DEĞİŞMEDİ


def test_tuning_endpoint(env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_c(5.0, impact=9) for _ in range(12)])
    c = TestClient(nb.app)
    out = c.get("/tuning").json()
    assert out["ready"] is True and "by_impact" in out and "by_tier" in out


# ── Oto-kalibrasyon: apply_tuning + POST /tuning/apply (Faz 2) ──────────────
def test_apply_not_ready_does_nothing(env, monkeypatch):
    monkeypatch.setattr(trader.S, "suppress_losing_sources", False)
    monkeypatch.setattr(trader, "_closed", [_c(1.0) for _ in range(5)])
    out = trader.apply_tuning(trader.suggest_tuning())
    assert out["applied"] is False and out["changes"] == []
    assert trader.S.auto_min_impact == 7   # değişmedi


def test_apply_raises_auto_min_impact(env, monkeypatch):
    closed = [_c(-3.0, impact=7) for _ in range(6)] + [_c(5.0, impact=9) for _ in range(6)]
    monkeypatch.setattr(trader, "_closed", closed)
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    out = trader.apply_tuning(trader.suggest_tuning())
    assert out["applied"] is True
    assert trader.S.auto_min_impact == 9
    assert any(c["field"] == "auto_min_impact" and c["to"] == 9 for c in out["changes"])


def test_apply_respects_floor(env, monkeypatch):
    # güç 5 kârlı, eşik düşürme önerisi → ama taban 7'nin altına inmez
    closed = [_c(5.0, impact=5) for _ in range(12)]
    monkeypatch.setattr(trader, "_closed", closed)
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    trader.apply_tuning(trader.suggest_tuning(), min_impact_floor=7)
    assert trader.S.auto_min_impact == 7   # tabana kıstırıldı, 5'e düşmedi


def test_apply_enables_source_suppression(env, monkeypatch):
    monkeypatch.setattr(trader.S, "suppress_losing_sources", False)
    closed = [_c(-1.0, impact=9, news_source="ZayifKaynak") for _ in range(10)]
    monkeypatch.setattr(trader, "_closed", closed)
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    out = trader.apply_tuning(trader.suggest_tuning())
    assert trader.S.suppress_losing_sources is True
    assert any(c["field"] == "suppress_losing_sources" for c in out["changes"])


def test_apply_endpoint(env, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_c(5.0, impact=9) for _ in range(12)])
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(nb, "notify_remote", lambda msg: None)
    c = TestClient(nb.app)
    out = c.post("/tuning/apply").json()
    assert "applied" in out and "changes" in out


# ── İşlemsiz ön-bilgi: backtest sonuçlarından öneri (suggest_from_backtest) ──
def _bt(net_pct, impact=9, source="Binance"):
    return {"net_pct": net_pct, "impact": impact, "source": source,
            "symbol": "FOOUSDT", "direction": "bullish", "outcome": "tp"}


def test_pretrade_not_ready_below_min(env):
    out = trader.suggest_from_backtest([_bt(1.0) for _ in range(5)])
    assert out["ready"] is False and out["pretrade"] is True


def test_pretrade_suggests_from_backtest(env):
    # güç 7 backtest'te negatif, güç 9 pozitif → eşik 7→9 yükselt (gerçek işlem YOK)
    results = [_bt(-2.0, impact=7) for _ in range(6)] + [_bt(4.0, impact=9) for _ in range(6)]
    out = trader.suggest_from_backtest(results)
    s = [x for x in out["suggestions"] if x["type"] == "auto_min_impact"]
    assert out["ready"] is True and s and s[0]["suggested"] == 9
    assert "%" in s[0]["message"]   # birim % (USDT değil)


def test_pretrade_tier_from_backtest(env):
    results = [_bt(-1.5, impact=9, source="⚡Twitter") for _ in range(12)]
    out = trader.suggest_from_backtest(results, tier_of=nb._source_tier)
    tiers = [x for x in out["suggestions"] if x["type"] == "suppress_tier"]
    assert tiers and tiers[0]["tier"] == "sosyal"


def test_pretrade_endpoint_empty_archive(env, monkeypatch):
    class _FakeStore:
        def list_signals(self, **kw):
            return []
    monkeypatch.setattr(nb, "get_store", lambda: _FakeStore())
    c = TestClient(nb.app)
    out = c.get("/tuning/pretrade").json()
    assert out["ready"] is False and out["pretrade"] is True and out["samples"] == 0
