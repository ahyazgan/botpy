"""Toplu fiyat çekimi + fiyat önbelleği — /positions gecikmesini O(N)→O(1) indirir.

İzleme döngüsü (8s) get_prices ile önbelleği tazeler; get_positions cached_prices
ile çoğu zaman ağ çağrısı yapmadan anında döner.
"""

from __future__ import annotations

import trader


def test_get_prices_single_call_dedup(monkeypatch):
    calls = []

    def fake(url, params=None, **kw):
        calls.append(params)
        return [{"symbol": "BTCUSDT", "price": "100"}, {"symbol": "ETHUSDT", "price": "50"}]

    monkeypatch.setattr(trader, "get_json", fake)
    out = trader.get_prices(["BTCUSDT", "ETHUSDT", "BTCUSDT", ""])
    assert out == {"BTCUSDT": 100.0, "ETHUSDT": 50.0}
    assert len(calls) == 1                          # tek HTTP çağrısı (seri N değil)
    assert calls[0]["symbols"] == '["BTCUSDT","ETHUSDT"]'   # dedupe + JSON dizi


def test_get_prices_empty_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(trader, "get_json", lambda *a, **k: calls.append(1))
    assert trader.get_prices([]) == {}
    assert calls == []                              # boş liste → ağ çağrısı yok


def test_get_prices_skips_malformed(monkeypatch):
    monkeypatch.setattr(trader, "get_json", lambda *a, **k: [
        {"symbol": "OKUSDT", "price": "5"}, {"symbol": "BADUSDT"}, {"price": "9"},
    ])
    assert trader.get_prices(["OKUSDT", "BADUSDT"]) == {"OKUSDT": 5.0}


def test_cached_prices_hits_cache(monkeypatch):
    # Önbelleği taze doldur → ağ çağrısı OLMAMALI
    monkeypatch.setattr(trader, "_price_cache", {"BTCUSDT": (123.0, trader.time.time())})
    calls = []
    monkeypatch.setattr(trader, "get_json", lambda *a, **k: calls.append(1) or [])
    out = trader.cached_prices(["BTCUSDT"])
    assert out == {"BTCUSDT": 123.0}
    assert calls == []                              # taze önbellek → ağ yok


def test_cached_prices_fetches_stale(monkeypatch):
    # Bayat önbellek (yaş > TTL) → çek
    old = trader.time.time() - (trader._PRICE_TTL + 5)
    monkeypatch.setattr(trader, "_price_cache", {"BTCUSDT": (1.0, old)})
    monkeypatch.setattr(trader, "get_json", lambda *a, **k: [{"symbol": "BTCUSDT", "price": "200"}])
    out = trader.cached_prices(["BTCUSDT"])
    assert out == {"BTCUSDT": 200.0}                # bayatı tazeledi
