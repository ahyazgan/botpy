"""_create_order_idempotent: çift-emir'e karşı (sahte borsa ile)."""

from __future__ import annotations

import pytest

import trader


class FakeEx:
    """create_order ardışık etkileri ('ok' veya Exception); fetch_order sabit sonuç."""

    def __init__(self, create_effects, fetch_result=None):
        self.create_effects = list(create_effects)
        self.fetch_result = fetch_result
        self.create_calls = 0
        self.fetch_calls = 0
        self.last_params: dict | None = None

    def create_order(self, symbol, otype, side, amount, price=None, params=None):
        self.create_calls += 1
        self.last_params = params
        eff = self.create_effects.pop(0)
        if isinstance(eff, Exception):
            raise eff
        return {"id": "live1", "symbol": symbol, "filled": amount, "params": params}

    def fetch_order(self, oid, symbol, params=None):
        self.fetch_calls += 1
        if self.fetch_result is None:
            raise RuntimeError("order not found")
        return self.fetch_result


def _no_sleep(_):
    pass


def test_success_first_try():
    ex = FakeEx(["ok"])
    out = trader._create_order_idempotent(ex, "FOO/USDT", "market", "buy", 1.0, sleep=_no_sleep)
    assert out["id"] == "live1" and ex.create_calls == 1
    assert ex.last_params["newClientOrderId"].startswith("botpy")   # idempotent anahtar


def test_fixed_client_order_id_across_retries():
    # ilk create patlar, fetch bulamaz, ikinci create başarılı → aynı coid kullanılmalı
    ex = FakeEx([ConnectionError("x"), "ok"], fetch_result=None)
    coids = []
    orig = ex.create_order
    def spy(symbol, otype, side, amount, price=None, params=None):
        coids.append(params["newClientOrderId"])
        return orig(symbol, otype, side, amount, price, params)
    ex.create_order = spy
    trader._create_order_idempotent(ex, "FOO/USDT", "market", "buy", 1.0, sleep=_no_sleep)
    assert ex.create_calls == 2 and len(set(coids)) == 1   # iki denemede AYNI coid


def test_no_double_when_order_already_on_exchange():
    # create yanıtı kaybolur (exception) AMA emir borsada var → fetch bulur, tekrar create YOK
    ex = FakeEx([ConnectionError("timeout")], fetch_result={"id": "live1", "status": "closed"})
    out = trader._create_order_idempotent(ex, "FOO/USDT", "market", "buy", 1.0, sleep=_no_sleep)
    assert out["id"] == "live1"
    assert ex.create_calls == 1          # ikinci kez gönderilmedi (çift emir yok)
    assert ex.fetch_calls == 1


def test_raises_after_exhausting_retries():
    ex = FakeEx([ConnectionError("a"), ConnectionError("b"), ConnectionError("c")], fetch_result=None)
    with pytest.raises(ConnectionError):
        trader._create_order_idempotent(ex, "FOO/USDT", "market", "buy", 1.0, retries=3, sleep=_no_sleep)
    assert ex.create_calls == 3


def test_find_order_swallows_errors():
    ex = FakeEx([], fetch_result=None)
    assert trader._find_order(ex, "FOO/USDT", "coid") is None
