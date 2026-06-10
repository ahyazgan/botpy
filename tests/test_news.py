"""Haber sinyali çekirdeği (news.py) testleri."""

from __future__ import annotations

from news import (
    NewsItem,
    evaluate_news,
    extract_symbols,
    news_key,
    sentiment,
    to_pair,
)

KNOWN = {"BTC", "ETH", "PEPE", "SOL", "ARB", "SUN"}


# ── sentiment ────────────────────────────────────────────────────────────
def test_sentiment_bullish():
    assert sentiment("Binance lists PEPE — new listing!") == "bullish"


def test_sentiment_bearish():
    assert sentiment("Major exploit and hack drains funds") == "bearish"


def test_sentiment_neutral():
    assert sentiment("The market moved sideways today") == "neutral"


# ── extract_symbols ──────────────────────────────────────────────────────
def test_extract_cashtag():
    assert extract_symbols("Buy $PEPE now", KNOWN) == ["PEPE"]


def test_extract_whitelist_word_boundary():
    syms = extract_symbols("Solana SOL upgrade live", KNOWN)
    assert "SOL" in syms


def test_extract_ignores_unknown():
    assert extract_symbols("$DOGE to the moon", KNOWN) == []  # DOGE whitelist'te yok


def test_extract_ignores_substring():
    # "ARBITRAGE" içinde ARB var ama kelime sınırı tutmamalı
    assert extract_symbols("arbitrage opportunity", KNOWN) == []


def test_extract_max_symbols():
    out = extract_symbols("$BTC $ETH $SOL $PEPE", KNOWN, max_symbols=2)
    assert len(out) == 2


# ── to_pair ──────────────────────────────────────────────────────────────
def test_to_pair():
    assert to_pair("pepe") == "PEPEUSDT"
    assert to_pair("BTC", quote="FDUSD") == "BTCFDUSD"


# ── news_key (dedup) ─────────────────────────────────────────────────────
def test_news_key_uses_external_id():
    a = NewsItem("twitter", "abc", external_id="123")
    assert news_key(a) == "twitter:123"


def test_news_key_hash_stable():
    a = NewsItem("newsapi", "Same Text")
    b = NewsItem("newsapi", "  same text  ")  # normalize → aynı
    assert news_key(a) == news_key(b)


# ── evaluate_news ────────────────────────────────────────────────────────
def test_evaluate_bullish_with_symbol():
    sig = evaluate_news(NewsItem("webhook", "Binance lists $PEPE — listing soon"), KNOWN)
    assert sig is not None
    assert sig.symbol == "PEPE" and sig.pair == "PEPEUSDT"
    assert sig.sentiment == "bullish"


def test_evaluate_bearish_returns_none():
    assert evaluate_news(NewsItem("webhook", "$PEPE exploit, funds hacked"), KNOWN) is None


def test_evaluate_bullish_no_known_symbol_none():
    assert evaluate_news(NewsItem("webhook", "Great new listing for $DOGE"), KNOWN) is None


def test_evaluate_neutral_none():
    assert evaluate_news(NewsItem("webhook", "$BTC price unchanged today"), KNOWN) is None
