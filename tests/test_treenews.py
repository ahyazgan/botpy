"""TreeNews: parse_tree_message biçimleri + WS yeniden bağlanma backoff'u."""

from __future__ import annotations

import json

import news_bot as nb
from news_bot import parse_tree_message


# ── Üstel backoff (saf) ─────────────────────────────────────────────────────
def test_backoff_grows_and_caps():
    b = nb._next_backoff(0)
    assert b == nb._WS_BACKOFF_BASE          # 0 → taban
    seq = []
    for _ in range(10):
        b = nb._next_backoff(b)
        seq.append(b)
    assert seq[0] == nb._WS_BACKOFF_BASE * 2
    assert max(seq) == nb._WS_BACKOFF_MAX     # tavanla sınırlı
    assert nb._next_backoff(nb._WS_BACKOFF_MAX) == nb._WS_BACKOFF_MAX


# ── parse_tree_message ──────────────────────────────────────────────────────
def test_parse_exchange_listing():
    raw = json.dumps({"title": "Binance Will List FOO", "type": "blogs",
                      "coin": "foo", "url": "https://x/1", "_id": "1",
                      "time": 1_700_000_000_000})
    it = parse_tree_message(raw)
    assert it is not None
    assert it.title == "Binance Will List FOO"
    assert it.source == "⚡blogs"
    assert it.coins == ["FOO"]
    assert it.url == "https://x/1" and it.published is not None


def test_parse_twitter_with_suggestions():
    raw = json.dumps({"body": "ETH pumping hard", "type": "twitter",
                      "suggestions": [{"coin": "eth"}, {"coin": "btc"}]})
    it = parse_tree_message(raw)
    assert it is not None
    assert it.title == "ETH pumping hard"
    assert it.source == "⚡twitter"
    assert it.coins == ["ETH", "BTC"]


def test_parse_source_dict_and_symbols_list():
    raw = json.dumps({"title": "t", "source": {"name": "CoinDesk"},
                      "symbols": ["sol", "arb", "sol"]})
    it = parse_tree_message(raw)
    assert it is not None
    assert it.source == "⚡CoinDesk"
    assert it.coins == ["SOL", "ARB"]        # tekilleştirildi, sıra korundu


def test_parse_invalid_returns_none():
    assert parse_tree_message("not json") is None
    assert parse_tree_message(json.dumps([1, 2, 3])) is None        # dict değil
    assert parse_tree_message(json.dumps({"foo": "bar"})) is None   # başlık yok


def test_parse_dedupe_id_stable():
    raw = json.dumps({"title": "Same", "type": "blogs", "url": "https://x/9"})
    a = parse_tree_message(raw)
    b = parse_tree_message(raw)
    assert a is not None and b is not None and a.id == b.id          # aynı id (dedupe)
