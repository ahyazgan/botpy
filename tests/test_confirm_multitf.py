"""confirm_with_price çoklu zaman dilimi (15dk + 1s) teyidi — ağsız."""

from __future__ import annotations

import news_bot as nb
from news_bot import NewsItem


def _item(direction="bullish"):
    return NewsItem(id="x", source="S", title="t", url="u", published=None,
                    fetched_at="2026-06-15T00:00:00+00:00", coins=["FOO"],
                    impact=9, direction=direction)


def _patch_stats(monkeypatch, *, pct24, vol, move15, move60):
    monkeypatch.setattr(nb, "_fetch_symbol_stats",
                        lambda session, sym: {"pct24": pct24, "vol": vol,
                                              "move15": move15, "move60": move60})


def test_confirmed_when_both_timeframes_align(monkeypatch):
    _patch_stats(monkeypatch, pct24=2.0, vol=5_000_000, move15=1.0, move60=2.0)
    it = _item("bullish")
    nb.confirm_with_price(None, it)
    assert it.confirmed is True
    assert it.price_60m_pct == 2.0
    assert "1s %+2.0" in it.price_note


def test_not_confirmed_when_1h_opposes(monkeypatch):
    # 15dk yukarı (spike) ama 1s belirgin aşağı → fade riski, teyit yok
    _patch_stats(monkeypatch, pct24=0.0, vol=5_000_000, move15=1.0, move60=-3.0)
    it = _item("bullish")
    nb.confirm_with_price(None, it)
    assert it.confirmed is False
    assert "1s ters yönde" in it.price_note


def test_bearish_alignment(monkeypatch):
    _patch_stats(monkeypatch, pct24=-2.0, vol=5_000_000, move15=-1.0, move60=-2.0)
    it = _item("bearish")
    nb.confirm_with_price(None, it)
    assert it.confirmed is True


def test_bearish_blocked_when_1h_up(monkeypatch):
    _patch_stats(monkeypatch, pct24=0.0, vol=5_000_000, move15=-1.0, move60=3.0)
    it = _item("bearish")
    nb.confirm_with_price(None, it)
    assert it.confirmed is False and "ters yönde" in it.price_note


def test_low_liquidity_not_confirmed(monkeypatch):
    _patch_stats(monkeypatch, pct24=2.0, vol=100, move15=1.0, move60=2.0)
    it = _item("bullish")
    nb.confirm_with_price(None, it)
    assert it.confirmed is False and "likidite" in it.price_note


def test_60m_in_to_dict(monkeypatch):
    _patch_stats(monkeypatch, pct24=2.0, vol=5_000_000, move15=1.0, move60=2.0)
    it = _item("bullish")
    nb.confirm_with_price(None, it)
    assert it.to_dict()["price_60m_pct"] == 2.0
