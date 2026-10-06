"""Binance duyuru başlıklarından ticker çıkarımı (kural puanlayıcı)."""

from __future__ import annotations

import pytest

import news_bot as nb


@pytest.mark.parametrize("title,expected", [
    ("Binance Will List Bonk (BONK) with Seed Tag Applied", ["BONK"]),
    ("Binance Will Delist ANT, MULTI, VAI, XMR on 2024-02-20", ["ANT", "MULTI", "VAI", "XMR"]),
    ("Binance Will Delist WAVES, WNXM, OMG and XEM on 2024-06-17", ["WAVES", "WNXM", "OMG", "XEM"]),
    ("Binance Will Delist BTCST & SNT on 2024-01-05", ["BTCST", "SNT"]),
    ("Binance Will Delist Bonk (BONK)", ["BONK"]),
])
def test_extract_binance_tickers(title, expected):
    assert nb.extract_binance_tickers(title) == expected


def test_multi_delist_scores_bearish_with_coins():
    it = nb.NewsItem(id="x", source="Binance", title="Binance Will Delist ANT, MULTI, VAI, XMR on 2024-02-20",
                     url="", published="2024-02-10T00:00:00+00:00", fetched_at="2024-02-10T00:00:00+00:00")
    nb.score_item(it)
    assert it.direction == "bearish" and it.impact >= 8
    assert it.coins[:2] == ["ANT", "MULTI"]


@pytest.mark.parametrize("source", ["Binance", "⚡Binance EN", "?Binance EN"])
def test_monitoring_tag_scored_bearish_with_coins(source):
    it = nb.NewsItem(id="x", source=source,
                     title="Binance Will Extend the Monitoring Tag to Include AVA, GNS, SCR & TOWNS on 2026-09-04",
                     url="", published="2026-09-04T08:00:00+00:00", fetched_at="2026-09-04T08:00:00+00:00")
    nb.score_item(it)
    assert it.direction == "bearish" and it.impact >= 8
    assert {"AVA", "GNS", "SCR", "TOWNS"} <= set(it.coins)


def test_treenews_binance_delist_gets_coins():
    it = nb.NewsItem(id="x", source="⚡Binance EN", title="Binance Will Delist ICX, SCRT, STORJ on 2026-09-03",
                     url="", published="2026-08-27T08:00:00+00:00", fetched_at="2026-08-27T08:00:00+00:00")
    nb.score_item(it)
    assert it.direction == "bearish" and it.coins[:3] == ["ICX", "SCRT", "STORJ"]
