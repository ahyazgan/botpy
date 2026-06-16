"""Borsa-native koruyucu stop: place/cancel + place_trade(live) + close/update entegrasyonu."""

from __future__ import annotations

import pytest

import trader


class FakeEx:
    """Minimal ccxt-benzeri borsa: emirleri/iptalleri kaydeder."""

    def __init__(self):
        self.created: list[dict] = []
        self.cancelled: list[tuple] = []

    def create_order(self, symbol, otype, side, amount, price=None, params=None):
        self.created.append({"symbol": symbol, "type": otype, "side": side,
                             "amount": amount, "price": price, "params": params or {}})
        return {"id": f"ord{len(self.created)}", "average": None, "filled": amount}

    def cancel_order(self, oid, symbol):
        self.cancelled.append((oid, symbol))

    def set_leverage(self, lev, symbol):
        pass


# ── helper birim testleri ─────────────────────────────────────────────────
def test_place_protective_orders_futures_reduce_only():
    ex = FakeEx()
    pos = {"side": "long", "amount": 10.0, "market": "futures",
           "sl_price": 95.0, "tp_price": 110.0}
    trader._place_protective_orders(ex, "FOO/USDT", pos)
    assert pos["exchange_sl_id"] == "ord1" and pos["exchange_tp_id"] == "ord2"
    sl = ex.created[0]
    assert sl["side"] == "sell" and sl["params"]["stopLossPrice"] == 95.0
    assert sl["params"]["reduceOnly"] is True               # futures reduceOnly
    assert ex.created[1]["params"]["takeProfitPrice"] == 110.0


def test_place_protective_orders_spot_no_reduce_only():
    ex = FakeEx()
    pos = {"side": "long", "amount": 10.0, "market": "spot", "sl_price": 95.0, "tp_price": None}
    trader._place_protective_orders(ex, "FOO/USDT", pos)
    assert "reduceOnly" not in ex.created[0]["params"]      # spot'ta reduceOnly yok
    assert "exchange_tp_id" not in pos                       # tp_price yok → TP emri yok


def test_cancel_protective_orders_clears_ids():
    ex = FakeEx()
    pos = {"exchange_sl_id": "ord1", "exchange_tp_id": "ord2"}
    trader._cancel_protective_orders(ex, "FOO/USDT", pos)
    assert {c[0] for c in ex.cancelled} == {"ord1", "ord2"}
    assert "exchange_sl_id" not in pos and "exchange_tp_id" not in pos


def test_cancel_swallows_already_filled():
    class Boom(FakeEx):
        def cancel_order(self, oid, symbol):
            raise RuntimeError("order already filled")
    pos = {"exchange_sl_id": "ord1"}
    trader._cancel_protective_orders(Boom(), "FOO/USDT", pos)   # hata yutulmalı
    assert "exchange_sl_id" not in pos


# ── place_trade canlı yolda koruyucu emir koyar ───────────────────────────
@pytest.fixture()
def live(monkeypatch):
    ex = FakeEx()
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    monkeypatch.setattr(trader, "_get_exchange", lambda: ex)
    monkeypatch.setattr(trader, "_ccxt_symbol", lambda s: "FOO/USDT")
    monkeypatch.setattr(trader, "_estimate_fill", lambda *a, **k: None)
    monkeypatch.setattr(trader, "get_price", lambda s: 100.0)
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    # entry emri _create_order_idempotent üzerinden → fill döndür
    monkeypatch.setattr(trader, "_create_order_idempotent",
                        lambda ex, csym, otype, side, amount, price=None, params=None:
                        {"average": 100.0, "filled": amount})
    for k, v in {
        "paper_trading": False, "market": "futures", "leverage": 1, "use_sl_tp": True,
        "trade_usdt": 100.0, "order_type": "market", "max_positions": 20,
        "min_orderbook_usd": 0.0, "slippage_guard_pct": 0.0, "trailing_stop_pct": 0.0,
        "stop_loss_pct": 3.0, "take_profit_pct": 6.0, "use_atr_exits": False,
        "exchange_native_stops": True,
        "daily_loss_limit_usdt": 0.0, "max_total_exposure_usdt": 0.0, "max_per_coin_usdt": 0.0,
    }.items():
        setattr(trader.S, k, v)
    return ex


def test_place_trade_live_sets_protective_stops(live):
    pos = trader.place_trade("FOOUSDT", "long")
    # giriş + SL + TP = 3 emir? entry _create_order_idempotent mock'landı (ex.create_order'a girmez)
    # → ex.created yalnız koruyucu SL+TP içerir
    assert pos["exchange_sl_id"] and pos["exchange_tp_id"]
    assert "protect_error" not in pos
    skews = {c["params"].get("stopLossPrice") or c["params"].get("takeProfitPrice") for c in live.created}
    assert pos["sl_price"] in skews and pos["tp_price"] in skews


def test_place_trade_protect_failure_flags_not_raises(live, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("borsa reddetti")
    monkeypatch.setattr(trader, "_place_protective_orders", boom)
    pos = trader.place_trade("FOOUSDT", "long")        # girişi BOZMAMALI
    assert pos["protect_error"] == "borsa reddetti"    # ama flag bırakmalı


def test_close_position_cancels_protective(live):
    pos = trader.place_trade("FOOUSDT", "long")
    sl_id = pos["exchange_sl_id"]
    trader.close_position(pos["id"], reason="manuel")
    assert (sl_id, "FOO/USDT") in live.cancelled        # kapanışta duran stop iptal edildi


def test_paper_mode_no_protective_orders(live):
    trader.S.paper_trading = True
    pos = trader.place_trade("FOOUSDT", "long")
    assert "exchange_sl_id" not in pos and live.created == []   # paper → borsaya dokunmaz
