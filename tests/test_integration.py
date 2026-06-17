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


def test_volume_brain_settings_patch(client):
    """Hacim Beyni ayarları API üzerinden gerçekten yazılmalı (SettingsPatch'te tanımlı).

    Regresyon: alanlar Pydantic modelinde yoksa FastAPI sessizce düşürür → ayar tutmaz.
    """
    saved = {k: getattr(trader.S, k) for k in ("size_by_volume", "min_rel_volume", "max_book_frac")}
    try:
        r = client.patch("/settings", json={"size_by_volume": True, "min_rel_volume": 1.5, "max_book_frac": 0.10})
        assert r.status_code == 200
        body = r.json()
        assert body["size_by_volume"] is True
        assert body["min_rel_volume"] == 1.5
        assert body["max_book_frac"] == 0.10
    finally:
        for k, v in saved.items():   # global S'i kirletme (test izolasyonu)
            setattr(trader.S, k, v)


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


def test_security_headers(client):
    h = client.get("/healthz").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in h["Content-Security-Policy"]
    assert "max-age" in h["Strict-Transport-Security"]


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "# TYPE botpy_alerts_total counter" in body
    assert "botpy_uptime_seconds " in body
    assert "botpy_open_positions " in body


def test_render_metrics_pure():
    out = nb._render_metrics({"botpy_alerts_total": 3, "botpy_uptime_seconds": 10})
    assert "# HELP botpy_alerts_total" in out
    assert "botpy_alerts_total 3" in out
    assert out.endswith("\n")


def test_stream_diff_pure():
    # snapshot en-yeni-başta; seen'de olmayanlar en-eski-önce döner
    snap = [{"id": "c"}, {"id": "b"}, {"id": "a"}]
    assert nb._stream_diff(snap, {"a", "b", "c"}) == []         # hepsi görüldü
    assert nb._stream_diff(snap, {"a"}) == [{"id": "b"}, {"id": "c"}]  # b,c yeni → eski-önce
    assert nb._stream_diff([], set()) == []


def test_ws_last_msg_age_pure(monkeypatch):
    monkeypatch.setitem(nb._ws_state, "last_msg_at", None)
    assert nb._ws_last_msg_age() is None
    monkeypatch.setitem(nb._ws_state, "last_msg_at", 1000.0)
    assert nb._ws_last_msg_age(now=1012.0) == 12.0


def test_health_and_metrics_expose_ws(client, monkeypatch):
    monkeypatch.setitem(nb._ws_state, "connected", True)
    monkeypatch.setitem(nb._ws_state, "last_msg_at", None)
    h = client.get("/health").json()
    assert h["ws_connected"] is True and h["ws_last_msg_age_sec"] is None
    body = client.get("/metrics").text
    assert "botpy_ws_connected 1" in body
    # mesaj yokken age gauge'i atlanır
    assert "botpy_ws_last_msg_age_seconds" not in body


def test_metrics_exposes_rate_limit(client, monkeypatch):
    import netutil
    monkeypatch.setitem(netutil._stats, "rate_limited", 4)
    monkeypatch.setitem(netutil._stats, "retries", 7)
    body = client.get("/metrics").text
    assert "botpy_rate_limited_total 4" in body
    assert "botpy_http_retries_total 7" in body
