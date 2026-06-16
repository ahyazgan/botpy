"""auto_decision (yan etkisiz oto-işlem kararı) + /auto-preview endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader
from news_bot import NewsItem
from storage import Store


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    for k, v in {
        "auto_min_impact": 7, "auto_require_confirm": True, "market": "spot",
        "trade_usdt": 100.0, "size_by_impact": False, "skip_already_priced_pct": 0.0,
        "suppress_losing_sources": False, "reduce_after_losses": 0, "cooldown_sec": 0,
        "max_positions": 20, "auto_trade": False, "tier1_skip_confirm_impact": 0,
        "halt_trade_on_stale": True, "max_news_age_sec": 0, "max_same_direction": 0,
    }.items():
        setattr(trader.S, k, v)
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    yield


class _Item:
    def __init__(self, impact=9, direction="bullish", confirmed=True, symbol="FOOUSDT",
                 source="TreeNews", price_24h_pct=None):
        self.impact = impact
        self.direction = direction
        self.confirmed = confirmed
        self.symbol = symbol
        self.source = source
        self.price_24h_pct = price_24h_pct
        self.reason = ""


def test_decision_passes(env):
    d = trader.auto_decision(_Item(impact=9))
    assert d["would_trade"] is True and d["side"] == "long" and d["usdt"] == 100.0


def test_decision_low_impact(env):
    d = trader.auto_decision(_Item(impact=5))
    assert d["would_trade"] is False and "eşik" in d["reason"]


def test_decision_unconfirmed(env):
    d = trader.auto_decision(_Item(confirmed=False))
    assert d["would_trade"] is False and "teyid" in d["reason"]


def test_decision_neutral_and_spot_short(env):
    assert trader.auto_decision(_Item(direction="neutral"))["would_trade"] is False
    d = trader.auto_decision(_Item(direction="bearish"))
    assert d["would_trade"] is False and "short" in d["reason"]


def test_decision_already_priced(env):
    trader.S.skip_already_priced_pct = 15.0
    d = trader.auto_decision(_Item(price_24h_pct=20.0))
    assert d["would_trade"] is False and "fiyatlanmış" in d["reason"]


def test_decision_tier1_skips_confirm(env):
    """Tier-1: güç ≥ eşik net haberde teyit beklemeden gir (refleks)."""
    trader.S.tier1_skip_confirm_impact = 9
    d = trader.auto_decision(_Item(impact=9, confirmed=False))
    assert d["would_trade"] is True and d["reason"] == "tier1-refleks"


def test_decision_tier1_below_threshold_still_needs_confirm(env):
    """Tier-1 eşiğinin altındaki güç hâlâ teyit bekler (Tier-2)."""
    trader.S.tier1_skip_confirm_impact = 9
    d = trader.auto_decision(_Item(impact=8, confirmed=False))
    assert d["would_trade"] is False and "teyid" in d["reason"]


def test_decision_tier1_disabled_by_default(env):
    """Tier-1 kapalıyken (0) teyitsiz güçlü haber yine girmez."""
    d = trader.auto_decision(_Item(impact=10, confirmed=False))
    assert d["would_trade"] is False and "teyid" in d["reason"]


def test_decision_conviction_size(env):
    trader.S.size_by_impact = True
    assert trader.auto_decision(_Item(impact=10))["usdt"] == 150.0


# ── Güvenlik kapıları (Faz 1) ────────────────────────────────────────────
def test_decision_feed_stale_halts(env):
    """Akış kopukken oto-işlem durur (halt_trade_on_stale)."""
    d = trader.auto_decision(_Item(impact=10), feed_stale=True)
    assert d["would_trade"] is False and "kopuk" in d["reason"]


def test_decision_feed_stale_ignored_when_disabled(env):
    trader.S.halt_trade_on_stale = False
    d = trader.auto_decision(_Item(impact=10), feed_stale=True)
    assert d["would_trade"] is True


def test_decision_news_too_old(env):
    trader.S.max_news_age_sec = 300
    d = trader.auto_decision(_Item(impact=10), news_age_sec=600)
    assert d["would_trade"] is False and "eski" in d["reason"]


def test_decision_news_fresh_enough(env):
    trader.S.max_news_age_sec = 300
    d = trader.auto_decision(_Item(impact=10), news_age_sec=120)
    assert d["would_trade"] is True


def test_decision_age_gate_off_by_default(env):
    """max_news_age_sec=0 → yaş yok sayılır (geriye uyumlu)."""
    d = trader.auto_decision(_Item(impact=10), news_age_sec=99999)
    assert d["would_trade"] is True


def test_decision_same_direction_cap(env):
    trader.S.max_same_direction = 2
    monkey = [{"side": "long"}, {"side": "long"}]
    trader._positions = monkey  # type: ignore[assignment]
    d = trader.auto_decision(_Item(impact=10, symbol="BARUSDT"))
    assert d["would_trade"] is False and "aynı yönde" in d["reason"]


def test_decision_same_direction_under_cap(env):
    trader.S.max_same_direction = 2
    trader._positions = [{"side": "long"}]  # type: ignore[assignment]
    d = trader.auto_decision(_Item(impact=10, symbol="BARUSDT"))
    assert d["would_trade"] is True


def test_decision_is_side_effect_free(env):
    """auto_decision pozisyon açmamalı."""
    trader.auto_decision(_Item(impact=10))
    assert trader._positions == []


def test_auto_preview_endpoint(monkeypatch, tmp_path, env):
    store = Store(str(tmp_path / "ap.db"))
    monkeypatch.setattr(nb, "_store", store)
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    monkeypatch.setattr(nb, "_news", [
        NewsItem(id="a", source="TreeNews", title="strong", url="u", published=None,
                 fetched_at="2026-06-14T00:00:00+00:00", coins=["FOO"], impact=9,
                 direction="bullish", symbol="FOOUSDT", confirmed=True),
    ])
    c = TestClient(nb.app)
    d = c.get("/auto-preview").json()
    assert d["auto_trade_on"] is False
    assert len(d["preview"]) == 1
    assert d["preview"][0]["would_trade"] is True and d["preview"][0]["usdt"] == 100.0
    store.close()
