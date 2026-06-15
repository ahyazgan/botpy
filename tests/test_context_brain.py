"""Bağlam beyni: kaynak güvenilirliği (_source_tier) + haber yorgunluğu
(_coin_fatigue) + Claude prompt'una eklenen bağlam etiketi (_item_context)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import news_bot as nb
from news_bot import NewsItem


def _item(source="⚡Twitter", coins=None, ago_min=1, title="haber"):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=ago_min)).isoformat()
    it = NewsItem(id=f"x{ago_min}", source=source, title=title, url="u",
                  published=ts, fetched_at=ts)
    it.coins = coins or []
    return it


# ── _source_tier ────────────────────────────────────────────────────────
def test_tier_exchange():
    assert nb._source_tier("Binance") == "resmi-borsa"
    assert nb._source_tier("⚡Coinbase") == "resmi-borsa"
    assert nb._source_tier("⚡Upbit") == "resmi-borsa"


def test_tier_social():
    assert nb._source_tier("⚡Twitter") == "sosyal"
    assert nb._source_tier("⚡tweet") == "sosyal"


def test_tier_media():
    assert nb._source_tier("CoinDesk") == "medya"
    assert nb._source_tier("Cointelegraph") == "medya"


def test_tier_other():
    assert nb._source_tier("BilinmeyenKaynak") == "diğer"


# ── _coin_fatigue ─────────────────────────────────────────────────────────
def test_fatigue_counts_recent_coin_mentions():
    now = datetime.now(timezone.utc)
    recent = [_item(coins=["BTC"], ago_min=10), _item(coins=["BTC", "ETH"], ago_min=20),
              _item(coins=["SOL"], ago_min=5)]
    assert nb._coin_fatigue("BTC", now, recent) == 2
    assert nb._coin_fatigue("ETH", now, recent) == 1
    assert nb._coin_fatigue("DOGE", now, recent) == 0


def test_fatigue_ignores_old_news():
    now = datetime.now(timezone.utc)
    old = _item(coins=["BTC"], ago_min=nb.FATIGUE_WINDOW_HOURS * 60 + 30)
    assert nb._coin_fatigue("BTC", now, [old]) == 0


# ── _item_context ───────────────────────────────────────────────────────
def test_context_includes_tier():
    now = datetime.now(timezone.utc)
    it = _item(source="⚡Twitter", coins=["BTC"])
    assert "kaynak:sosyal" in nb._item_context(it, now, [it])


def test_context_flags_fatigue_when_repeated():
    now = datetime.now(timezone.utc)
    target = _item(source="Binance", coins=["BTC"], ago_min=1)
    recent = [target, _item(coins=["BTC"], ago_min=10), _item(coins=["BTC"], ago_min=20)]
    ctx = nb._item_context(target, now, recent)
    assert "yorgun" in ctx and "resmi-borsa" in ctx


def test_context_no_fatigue_label_when_single():
    now = datetime.now(timezone.utc)
    it = _item(source="CoinDesk", coins=["BTC"])
    ctx = nb._item_context(it, now, [it])
    assert "yorgun" not in ctx and "kaynak:medya" in ctx
