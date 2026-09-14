"""Binance testnet modu görünürlüğü: env bayrağı + /settings + /health + preflight."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader
from storage import Store


@pytest.fixture()
def client(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "tn.db"))
    monkeypatch.setattr(nb, "_store", store)
    monkeypatch.setattr(nb, "API_TOKEN", None)
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    c = TestClient(nb.app)
    yield c
    store.close()


def test_testnet_enabled_env_variants(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("BINANCE_TESTNET", v)
        assert trader.testnet_enabled() is True
    for v in ("", "0", "false", "no", "kapalı"):
        monkeypatch.setenv("BINANCE_TESTNET", v)
        assert trader.testnet_enabled() is False
    monkeypatch.delenv("BINANCE_TESTNET")
    assert trader.testnet_enabled() is False


def test_settings_and_health_expose_testnet(client, monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    assert client.get("/settings").json()["testnet"] is True
    assert client.get("/health").json()["testnet"] is True
    monkeypatch.delenv("BINANCE_TESTNET")
    assert client.get("/settings").json()["testnet"] is False
    assert client.get("/health").json()["testnet"] is False


def test_preflight_warns_on_testnet(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    checks = {c["check"]: c for c in trader.preflight()}
    assert checks["Binance testnet"]["status"] == "warn"
    # Canlı modda "İşlem modu" satırı testnet'i açıkça söylemeli
    monkeypatch.setattr(trader.S, "paper_trading", False)
    checks = {c["check"]: c for c in trader.preflight()}
    assert "TESTNET" in checks["İşlem modu"]["detail"]
    monkeypatch.setattr(trader.S, "paper_trading", True)


def test_preflight_no_testnet_row_when_off(monkeypatch):
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    names = [c["check"] for c in trader.preflight()]
    assert "Binance testnet" not in names
