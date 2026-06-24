"""Coin tercihleri: kara liste (asla oto-işlem) + izleme listesi (uyarı eşiği düşür).

Kullanıcı kontrolü — öğrenme değil, aşırı-uydurma riski yok, ağsız test edilebilir.
"""

from __future__ import annotations

import news_bot as nb
import trader


class _Item:
    def __init__(self, *, impact=9, coins=None, symbol="FOOUSDT", direction="bullish"):
        self.impact = impact
        self.direction = direction
        self.symbol = symbol
        self.confirmed = True
        self.rel_volume = None
        self.atr_pct = None
        self.coins = coins if coins is not None else []
        self.source = "test"


# ── _coin_set normalizasyonu ────────────────────────────────────────────────
def test_coin_set_normalizes():
    assert trader._coin_set("btc, eth") == {"BTC", "ETH"}
    assert trader._coin_set("DOGEUSDT SHIB/USDT") == {"DOGE", "SHIB"}
    assert trader._coin_set("") == set()
    assert trader._coin_set("  ,  ") == set()


def test_item_coins_includes_symbol_base():
    it = _Item(coins=["pepe"], symbol="WIFUSDT")
    assert trader._item_coins(it) == {"PEPE", "WIF"}


# ── Kara liste: auto_decision mutlak reddi ──────────────────────────────────
def _base_settings(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader.S.auto_min_impact = 7
    trader.S.auto_require_confirm = True
    trader.S.market = "spot"
    trader.S.min_rel_volume = 0.0
    trader.S.max_same_direction = 0
    trader.S.suppress_losing_sources = False
    trader.S.use_learned_vetoes = False
    trader.S.skip_already_priced_pct = 0.0
    trader.S.size_by_impact = False
    trader.S.size_by_kelly = False
    trader.S.size_by_volume = False
    trader.S.risk_parity = False
    trader.S.reduce_after_losses = 0


def test_blocklist_blocks_matching_coin(monkeypatch):
    _base_settings(monkeypatch)
    trader.S.blocked_coins = "DOGE, SHIB"
    d = trader.auto_decision(_Item(coins=["DOGE"], symbol="DOGEUSDT"))
    assert d["would_trade"] is False
    assert "kara listede" in d["reason"]
    trader.S.blocked_coins = ""


def test_blocklist_matches_via_symbol_base(monkeypatch):
    _base_settings(monkeypatch)
    trader.S.blocked_coins = "shib"
    d = trader.auto_decision(_Item(coins=[], symbol="SHIBUSDT"))
    assert d["would_trade"] is False
    assert "kara listede" in d["reason"]
    trader.S.blocked_coins = ""


def test_blocklist_allows_other_coins(monkeypatch):
    _base_settings(monkeypatch)
    trader.S.blocked_coins = "DOGE"
    d = trader.auto_decision(_Item(coins=["BTC"], symbol="BTCUSDT"))
    assert d["would_trade"] is True
    trader.S.blocked_coins = ""


def test_empty_blocklist_is_noop(monkeypatch):
    _base_settings(monkeypatch)
    trader.S.blocked_coins = ""
    d = trader.auto_decision(_Item(coins=["DOGE"], symbol="DOGEUSDT"))
    assert d["would_trade"] is True


# ── İzleme listesi: eşik düşürme ────────────────────────────────────────────
def test_watch_lowers_threshold_for_favored(monkeypatch):
    trader.S.watch_coins = "BTC"
    try:
        it = _Item(impact=5, coins=["BTC"], symbol="BTCUSDT")
        assert nb._alert_threshold_for(it, 7) == 5   # 7 - 2 bonus
        other = _Item(impact=5, coins=["ETH"], symbol="ETHUSDT")
        assert nb._alert_threshold_for(other, 7) == 7  # izlenmeyen → değişmez
    finally:
        trader.S.watch_coins = ""


def test_watch_threshold_floored_at_3(monkeypatch):
    trader.S.watch_coins = "BTC"
    try:
        it = _Item(coins=["BTC"], symbol="BTCUSDT")
        assert nb._alert_threshold_for(it, 4) == 3   # 4 - 2 = 2 ama taban 3
    finally:
        trader.S.watch_coins = ""


def test_no_watchlist_is_noop():
    trader.S.watch_coins = ""
    it = _Item(coins=["BTC"], symbol="BTCUSDT")
    assert nb._alert_threshold_for(it, 7) == 7


# ── Kalıcılık: PATCH /settings round-trip ───────────────────────────────────
def test_settings_roundtrip(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    out = trader.update_settings({"blocked_coins": "DOGE,SHIB", "watch_coins": "BTC"})
    assert out["blocked_coins"] == "DOGE,SHIB"
    assert out["watch_coins"] == "BTC"
    assert trader.blocked_coins_set() == {"DOGE", "SHIB"}
    trader.S.blocked_coins = ""
    trader.S.watch_coins = ""
