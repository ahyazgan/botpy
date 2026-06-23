"""Canlıya geçiş operasyonel ön-uçuş: trader.preflight + /preflight verdikt mantığı."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setattr(trader, "_halt", {"active": False, "reason": "", "since": ""})
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SECRET", raising=False)
    yield


@pytest.fixture()
def client():
    return TestClient(nb.app)


def _by_name(checks, prefix):
    return next(c for c in checks if c["check"].startswith(prefix))


# ── trader.preflight (saf) ───────────────────────────────────────────────
def test_safe_defaults_have_no_critical_in_paper():
    # Varsayılan paper: SL açık, native-stop açık, limitler dolu → kritik yok
    checks = trader.preflight()
    assert all(c["status"] != "critical" for c in checks)
    assert _by_name(checks, "İşlem modu")["detail"].startswith("PAPER")


def test_sl_off_is_critical():
    trader.S.use_sl_tp = False
    checks = trader.preflight()
    assert _by_name(checks, "Zarar durdurma")["status"] == "critical"


def test_unbounded_daily_loss_is_critical():
    trader.S.daily_loss_limit_usdt = 0.0
    assert _by_name(trader.preflight(), "Günlük zarar")["status"] == "critical"


def test_live_without_keys_is_critical():
    trader.S.paper_trading = False
    assert _by_name(trader.preflight(), "Borsa API")["status"] == "critical"


def test_live_with_keys_ok(monkeypatch):
    trader.S.paper_trading = False
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_SECRET", "s")
    assert _by_name(trader.preflight(), "Borsa API")["status"] == "ok"


def test_native_stops_off_live_critical():
    trader.S.paper_trading = False
    trader.S.exchange_native_stops = False
    assert _by_name(trader.preflight(), "Borsa koruyucu stop")["status"] == "critical"


def test_active_halt_is_critical():
    trader._halt = {"active": True, "reason": "emir-hata serisi", "since": "now"}
    assert _by_name(trader.preflight(), "Devre kesici")["status"] == "critical"


# ── /preflight endpoint verdikt ──────────────────────────────────────────
def test_endpoint_ready_with_safe_defaults(client, monkeypatch):
    # WS bayat olmasın; uzak bildirim/feed uyarısı verdikti bloke etmemeli
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: False)
    monkeypatch.setitem(nb._ws_state, "connected", True)
    monkeypatch.setitem(nb._ws_state, "last_msg_at", None)
    d = client.get("/preflight").json()
    assert d["counts"]["critical"] == 0
    assert "HAZIR DEĞİL" not in d["verdict"]   # kritik yok → bloke değil (warn olabilir)


def test_endpoint_not_ready_when_sl_off(client, monkeypatch):
    trader.S.use_sl_tp = False
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: False)
    d = client.get("/preflight").json()
    assert d["counts"]["critical"] >= 1
    assert "HAZIR DEĞİL" in d["verdict"]


def test_endpoint_stale_feed_is_critical(client, monkeypatch):
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: True)
    d = client.get("/preflight").json()
    feed = _by_name(d["checks"], "Haber beslemesi")
    assert feed["status"] == "critical"
    assert "HAZIR DEĞİL" in d["verdict"]
