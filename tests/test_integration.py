"""news_bot uçtan uca uç testleri (TestClient) + token koruması + arşiv budama."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader
from storage import Store


@pytest.fixture()
def client(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "int.db"))
    monkeypatch.setattr(nb, "_store", store)
    monkeypatch.setattr(nb, "API_TOKEN", None)            # varsayılan: açık
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    monkeypatch.setattr(trader, "_save_state", lambda: None)   # ayar yazma yan etkisi yok
    c = TestClient(nb.app)
    yield c
    store.close()


# ── Okuma uçları ───────────────────────────────────────────────────────────
def test_health(client):
    d = client.get("/health").json()
    assert d["ok"] is True
    assert "uptime_sec" in d and "scorer" in d


def test_news_empty(client):
    monkeypatch_news = client.get("/news").json()
    assert "news" in monkeypatch_news and "alert_threshold" in monkeypatch_news


def test_signals_and_risk(client):
    assert "signals" in client.get("/signals").json()
    r = client.get("/risk").json()
    assert "total_exposure_usdt" in r and "trading_halted" in r


def test_settings_roundtrip(client):
    assert client.get("/settings").status_code == 200
    ns = client.get("/news-settings").json()
    assert "alert_threshold" in ns
    r = client.patch("/news-settings", json={"alert_threshold": 9})
    assert r.status_code == 200 and r.json()["alert_threshold"] == 9


def test_trades_closed(client):
    assert "trades" in client.get("/trades/closed").json()


def test_backtest_empty_archive(client):
    d = client.get("/backtest?min_impact=10").json()
    assert d["ok"] is False and d["n"] == 0     # boş arşiv → ağ çağrısı yok


# ── Token koruması ─────────────────────────────────────────────────────────
def test_token_blocks_mutations(client, monkeypatch):
    monkeypatch.setattr(nb, "API_TOKEN", "secret")
    # token yok → 401
    assert client.patch("/news-settings", json={"alert_threshold": 8}).status_code == 401
    assert client.post("/trade", json={"symbol": "BTCUSDT", "side": "long"}).status_code == 401
    # okuma açık kalır
    assert client.get("/settings").status_code == 200
    # doğru token → geçer
    r = client.patch("/news-settings", json={"alert_threshold": 8}, headers={"X-API-Token": "secret"})
    assert r.status_code == 200


def test_no_token_open_by_default(client):
    # API_TOKEN None → başlıksız mutasyon serbest
    assert client.patch("/news-settings", json={"alert_threshold": 7}).status_code == 200


# ── Arşiv budama ───────────────────────────────────────────────────────────
def test_prune_signals(tmp_path):
    s = Store(str(tmp_path / "prune.db"))
    for i in range(10):
        s.add_signal({"id": f"s{i}", "source": "X", "title": "t", "impact": 8,
                      "direction": "bullish", "coins": [], "confirmed": False})
    removed = s.prune_signals(4)
    assert removed == 6
    assert s.signal_span()["count"] == 4
    assert s.prune_signals(0) == 0              # keep<=0 → no-op
    s.close()


def test_healthz_liveness(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
