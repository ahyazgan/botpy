"""Gecikme derinliği: SLA değerlendirme + kaynak kırılımı + oto-işlem latency guard-rail."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import latency as lat
import news_bot as nb
import trader
from latency import evaluate_sla


# ── evaluate_sla (saf) ───────────────────────────────────────────────────
def test_sla_ok_when_under_threshold():
    summ = {"pipeline": {"p95_ms": 5000.0, "count": 10}}
    out = evaluate_sla(summ, {"pipeline": 12000.0})
    assert out["pipeline"]["ok"] is True


def test_sla_breach_when_over_threshold():
    summ = {"pipeline": {"p95_ms": 15000.0, "count": 10}}
    out = evaluate_sla(summ, {"pipeline": 12000.0})
    assert out["pipeline"]["ok"] is False


def test_sla_skips_low_sample_stages():
    summ = {"pipeline": {"p95_ms": 99999.0, "count": 2}}   # < min_samples
    out = evaluate_sla(summ, {"pipeline": 12000.0}, min_samples=5)
    assert "pipeline" not in out


def test_sla_skips_undefined_stage():
    summ = {"ingest": {"p95_ms": 100.0, "count": 10}}
    out = evaluate_sla(summ, {"pipeline": 12000.0})
    assert out == {}


# ── kaynak-bazlı ingest kırılımı ─────────────────────────────────────────
def test_source_breakdown_separate():
    lat.reset()
    lat.record_source("treenews", 50.0)
    lat.record_source("rss", 4000.0)
    s = lat.source_summary()
    assert s["treenews"]["p50_ms"] == 50.0
    assert s["rss"]["p50_ms"] == 4000.0
    lat.reset()


def test_source_bucket_classification():
    def mk(src):
        it = nb.NewsItem(id="x", source=src, title="t", url="", published=None, fetched_at="")
        return nb._source_bucket(it)
    assert mk("⚡twitter") == "treenews"
    assert mk("Binance Listing") == "binance"
    assert mk("CoinDesk RSS") == "rss"


# ── /latency endpoint zenginleşti ────────────────────────────────────────
def test_latency_endpoint_has_sla_and_sources():
    lat.reset()
    lat.record_source("treenews", 100.0)
    d = TestClient(nb.app).get("/latency").json()
    assert "by_source" in d and "sla" in d and "sla_ok" in d
    assert "treenews" in d["by_source"]
    lat.reset()


# ── Guard-rail: SLA aşımında oto-işlem durur ─────────────────────────────
class _Item:
    impact = 10
    direction = "bullish"
    symbol = "FOOUSDT"
    confirmed = True
    rel_volume = None
    atr_pct = None


@pytest.fixture()
def trade_env(monkeypatch):
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader.S.auto_min_impact = 7
    trader.S.market = "spot"
    yield


def test_latency_slow_blocks_when_guard_on(trade_env):
    trader.S.halt_trade_on_latency = True
    d = trader.auto_decision(_Item(), latency_slow=True)
    assert d["would_trade"] is False
    assert "gecikme" in d["reason"].lower()


def test_latency_slow_allowed_when_guard_off(trade_env):
    trader.S.halt_trade_on_latency = False
    d = trader.auto_decision(_Item(), latency_slow=True)
    assert d["would_trade"] is True


def test_latency_ok_does_not_block(trade_env):
    trader.S.halt_trade_on_latency = True
    d = trader.auto_decision(_Item(), latency_slow=False)
    assert d["would_trade"] is True
