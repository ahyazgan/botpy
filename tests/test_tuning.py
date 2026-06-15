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
