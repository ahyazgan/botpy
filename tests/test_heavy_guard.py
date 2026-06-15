"""Ağ-yoğun uçlarda eşzamanlılık koruması (/backtest, /scorecard → 409)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
from storage import Store


@pytest.fixture()
def client(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "h.db"))
    monkeypatch.setattr(nb, "_store", s)
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    c = TestClient(nb.app)
    yield c
    s.close()


def test_409_when_heavy_in_flight(client):
    assert nb._heavy_lock.acquire(blocking=False) is True   # başka istek koşuyormuş gibi
    try:
        assert client.get("/backtest").status_code == 409
        assert client.get("/scorecard").status_code == 409
    finally:
        nb._heavy_lock.release()


def test_releases_after_call(client):
    # boş arşiv → ağ çağrısı yok, hızlı döner; kilit serbest kalmalı
    assert client.get("/backtest?min_impact=10").status_code == 200
    # kilit tekrar alınabiliyor → serbest bırakılmış
    assert nb._heavy_lock.acquire(blocking=False) is True
    nb._heavy_lock.release()


def test_releases_after_exception(client, monkeypatch):
    import news_backtest as nbt

    def boom(*a, **k):
        raise RuntimeError("patladı")

    # prefetch'i patlat ama önce arşivde sinyal olsun ki prefetch'e ulaşsın
    nb._store.add_signal({"id": "s1", "source": "X", "title": "t", "impact": 9,
                          "direction": "bullish", "symbol": "FOOUSDT", "coins": [],
                          "confirmed": True, "published": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(nbt, "prefetch", boom)
    safe = TestClient(nb.app, raise_server_exceptions=False)
    r = safe.get("/backtest")
    assert r.status_code == 500                              # iç hata
    # finally çalıştı → kilit serbest
    assert nb._heavy_lock.acquire(blocking=False) is True
    nb._heavy_lock.release()
