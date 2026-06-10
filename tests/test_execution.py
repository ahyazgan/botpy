"""Yürütme gerçekçiliği: VWAP/slippage, maliyet, derinlik-farkındalıklı arb."""

from __future__ import annotations

import pytest

import arb_bot as ab


# ── book_levels ──────────────────────────────────────────────────────────
def test_book_levels_filters_invalid():
    raw = [
        {"price": "0.45", "size": "100"},
        {"price": None, "size": "5"},
        {"price": "0.46", "size": "0"},     # size 0 → atılır
        {"price": "0.47", "size": "20"},
    ]
    assert ab.book_levels(raw) == [(0.45, 100.0), (0.47, 20.0)]


# ── vwap_for_size ────────────────────────────────────────────────────────
def test_vwap_single_level():
    vwap, filled = ab.vwap_for_size([(0.45, 100.0)], 50.0)
    assert vwap == pytest.approx(0.45)
    assert filled == pytest.approx(50.0)


def test_vwap_walks_levels():
    # 30 @ 0.40, 30 @ 0.50 → 60 adet için VWAP = (30*0.4 + 30*0.5)/60 = 0.45
    vwap, filled = ab.vwap_for_size([(0.40, 30.0), (0.50, 30.0)], 60.0)
    assert vwap == pytest.approx(0.45)
    assert filled == pytest.approx(60.0)


def test_vwap_insufficient_depth():
    vwap, filled = ab.vwap_for_size([(0.40, 10.0)], 50.0)
    assert filled == pytest.approx(10.0)   # talep 50 ama 10 var
    assert vwap == pytest.approx(0.40)


def test_vwap_empty():
    assert ab.vwap_for_size([], 50.0) == (None, 0.0)


# ── net_arb_profit ───────────────────────────────────────────────────────
def test_net_profit_buy_after_gas():
    # 100 çift, yes+no=0.90 → brüt 100*0.10=10; gas 2*0.05=0.1 → ~9.9
    net = ab.net_arb_profit("buy", 0.45, 0.45, 100.0, fee_pct=0.0, gas_usdc=0.05)
    assert net == pytest.approx(10.0 - 0.1)


def test_net_profit_can_go_negative_on_costs():
    # ince kâr gas'i çıkarmaz
    net = ab.net_arb_profit("buy", 0.499, 0.499, 1.0, fee_pct=0.0, gas_usdc=0.05)
    assert net < 0


def test_net_profit_sell():
    net = ab.net_arb_profit("sell", 0.55, 0.55, 100.0, fee_pct=0.0, gas_usdc=0.05)
    assert net == pytest.approx(10.0 - 0.1)


# ── evaluate_book_arb (derinlik + maliyet) ───────────────────────────────
def test_evaluate_book_arb_buy_with_depth():
    yes_asks = [(0.45, 1000.0)]
    no_asks = [(0.45, 1000.0)]
    res = ab.evaluate_book_arb([], yes_asks, [], no_asks)
    assert res is not None
    direction, profit_pct, ya, na = res
    assert direction == "buy"
    assert ya == pytest.approx(0.45) and na == pytest.approx(0.45)


def test_evaluate_book_arb_rejects_thin_depth():
    # En iyi fiyat cazip ama derinlik yok → reddet
    yes_asks = [(0.45, 1.0)]   # sadece 1 adet
    no_asks = [(0.45, 1.0)]
    assert ab.evaluate_book_arb([], yes_asks, [], no_asks) is None


def test_evaluate_book_arb_rejects_when_net_below_min():
    # Küçük notional (max_trade=5) → %2 edge'de net ~0.10 USDC < 0.5 eşiği → None
    yes_asks = [(0.49, 100000.0)]
    no_asks = [(0.49, 100000.0)]
    assert ab.evaluate_book_arb([], yes_asks, [], no_asks, max_trade=5.0) is None
    # Aynı kitap, büyük notional → net eşiği geçer
    assert ab.evaluate_book_arb([], yes_asks, [], no_asks, max_trade=50.0) is not None


def test_evaluate_book_arb_sell():
    yes_bids = [(0.55, 1000.0)]
    no_bids = [(0.55, 1000.0)]
    res = ab.evaluate_book_arb(yes_bids, [], no_bids, [])
    assert res is not None and res[0] == "sell"
