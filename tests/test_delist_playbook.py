"""Delist senaryosu: resmi Binance 'Will Delist' duyurusunda short (opt-in).

Tanım (is_delist_signal), karar kapıları (teyit/chase/RVOL atlanır, spot short yok),
senaryo çıkışı (kendi SL/TP/süre; global trailing/breakeven/kısmi TP uygulanmaz).
"""

from __future__ import annotations

import pytest

import trader


class _Item:
    def __init__(self, title="Binance Will Delist ANT, MULTI, VAI, XMR on 2024-02-20",
                 source="Binance", direction="bearish", impact=8, symbol="ANTUSDT",
                 confirmed=False, price_24h_pct=-15.0, rel_volume=0.5):
        self.title, self.source, self.direction = title, source, direction
        self.impact, self.symbol, self.confirmed = impact, symbol, confirmed
        self.price_24h_pct, self.rel_volume = price_24h_pct, rel_volume
        self.atr_pct, self.coins, self.reason = None, ["ANT"], "delist"


@pytest.mark.parametrize("title,source,direction,expected", [
    ("Binance Will Delist ANT, MULTI, VAI, XMR on 2024-02-20", "Binance", "bearish", True),
    ("Binance Will Delist ICX, SCRT, STORJ on 2026-09-03", "⚡Binance EN", "bearish", True),
    ("Binance Announced the First Batch of Vote to Delist Results and Will Delist BADGER, BAL",
     "Binance", "bearish", True),
    ("Binance Futures Will Delist Multiple USDⓈ-M Perpetual Contracts", "Binance", "bearish", False),
    ("Binance Margin Will Delist the JASMY/BTC Cross Margin Pair", "Binance", "bearish", False),
    ("Binance Will Delist ANT on 2024-02-20", "CoinDesk", "bearish", False),     # resmi değil
    ("Binance Will Delist ANT on 2024-02-20", "Binance", "neutral", False),      # yön yok
    ("Binance Will List Bonk (BONK)", "Binance", "bullish", False),
])
def test_is_delist_signal(title, source, direction, expected):
    assert trader.is_delist_signal(_Item(title=title, source=source, direction=direction)) is expected


@pytest.fixture()
def gates(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    for k, v in {
        "market": "futures", "auto_min_impact": 6, "auto_require_confirm": True,
        "tier1_skip_confirm_impact": 0, "skip_already_priced_pct": 8.0, "min_rel_volume": 1.2,
        "max_same_direction": 0, "suppress_losing_sources": False, "use_learned_vetoes": False,
        "max_funding_rate_pct": 0.0, "size_by_impact": False, "size_by_kelly": False,
        "size_by_volume": False, "risk_parity": False, "reduce_after_losses": 0,
        "derisk_on_drawdown": False, "portfolio_risk": False, "risk_per_trade_pct": 0.0,
        "blocked_coins": "", "delist_playbook": True,
    }.items():
        monkeypatch.setattr(trader.S, k, v)


def test_playbook_bypasses_confirm_chase_rvol(gates):
    d = trader.auto_decision(_Item())   # teyitsiz, 24s −%15, RVOL 0.5 — hepsi normalde blok
    assert d["would_trade"] is True
    assert d["reason"] == "delist-senaryosu" and d["side"] == "short"


def test_playbook_off_keeps_normal_gates(gates, monkeypatch):
    monkeypatch.setattr(trader.S, "delist_playbook", False)
    assert trader.auto_decision(_Item())["reason"] == "fiyat teyidi yok"


def test_playbook_needs_futures_for_short(gates, monkeypatch):
    monkeypatch.setattr(trader.S, "market", "spot")
    assert trader.auto_decision(_Item())["reason"] == "spot'ta short yok"


def test_playbook_respects_impact_and_blocklist(gates, monkeypatch):
    assert trader.auto_decision(_Item(impact=5))["would_trade"] is False
    monkeypatch.setattr(trader.S, "blocked_coins", "ANT")
    assert "kara liste" in trader.auto_decision(_Item())["reason"]


# ── Senaryo çıkışı ───────────────────────────────────────────────────────────
@pytest.fixture()
def book(monkeypatch, gates):
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    monkeypatch.setattr(trader, "_estimate_fill", lambda *a, **k: None)
    monkeypatch.setattr(trader, "get_price", lambda s: 100.0)
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    for k, v in {
        "paper_trading": True, "auto_trade": True, "use_sl_tp": True, "trade_usdt": 100.0,
        "leverage": 1, "order_type": "market", "max_positions": 20, "min_orderbook_usd": 0.0,
        "slippage_guard_pct": 0.0, "stop_loss_pct": 3.0, "take_profit_pct": 6.0,
        "trailing_stop_pct": 2.0, "breakeven_pct": 1.0, "partial_tp_pct": 5.0,
        "partial_tp_levels": "", "use_atr_exits": False, "use_atr_trailing": False,
        "daily_loss_limit_usdt": 0.0, "max_total_exposure_usdt": 0.0, "max_per_coin_usdt": 0.0,
        "max_open_risk_usdt": 0.0, "max_drawdown_pct": 0.0, "use_entry_brain": False,
        "exchange_native_stops": False, "time_stop_min": 0,
    }.items():
        monkeypatch.setattr(trader.S, k, v)


def test_playbook_position_uses_own_exits(book):
    pos = trader.maybe_auto_trade(_Item())
    assert pos["playbook"] == "delist" and pos["side"] == "short"
    assert pos["sl_price"] == pytest.approx(108.0)   # short: +%8
    assert pos["tp_price"] == pytest.approx(80.0)    # short: −%20
    assert pos["time_stop_min"] == 240
    assert pos["trailing_pct"] == 0.0


def test_monitor_skips_global_trailing_breakeven_partial(book, monkeypatch):
    pos = trader.maybe_auto_trade(_Item())
    monkeypatch.setattr(trader, "get_prices", lambda syms: {"ANTUSDT": 90.0})   # short +%10
    closed = trader.monitor_positions()
    assert closed == []                      # global kısmi TP (%5) senaryoya uygulanmadı
    assert pos["sl_price"] == pytest.approx(108.0)   # trailing/breakeven SL'i çekmedi
    assert not pos.get("partial_levels_done")


def test_normal_position_still_gets_global_exits(book, monkeypatch):
    monkeypatch.setattr(trader.S, "delist_playbook", False)
    pos = trader.place_trade("FOOUSDT", "long")
    assert pos["playbook"] is None
    assert pos["sl_price"] == pytest.approx(97.0) and pos["tp_price"] == pytest.approx(106.0)
