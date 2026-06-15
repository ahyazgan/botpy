"""Çıkış preset'leri: trader.apply_preset + POST /settings/preset/{name}."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader


_PRESET_KEYS = set(trader.PRESETS["news"]) | set(trader.PRESETS["safe"])


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(trader, "_positions", [])
    snap = {k: getattr(trader.S, k) for k in _PRESET_KEYS}
    yield
    for k, v in snap.items():   # global S'i geri yükle (suite kirlenmesin)
        setattr(trader.S, k, v)


def test_apply_news_preset(env):
    out = trader.apply_preset("news")
    assert out["breakeven_pct"] == 1.5
    assert out["partial_tp_pct"] == 2.5
    assert out["trailing_stop_pct"] == 1.5
    assert out["time_stop_min"] == 60
    assert out["size_by_impact"] is True
    assert out["tier1_skip_confirm_impact"] == 9


def test_apply_safe_preset_reverts(env):
    trader.apply_preset("news")
    out = trader.apply_preset("safe")
    assert out["breakeven_pct"] == 0.0
    assert out["partial_tp_pct"] == 0.0
    assert out["trailing_stop_pct"] == 0.0
    assert out["time_stop_min"] == 0
    assert out["size_by_impact"] is False
    assert out["tier1_skip_confirm_impact"] == 0


def test_apply_unknown_preset_raises(env):
    with pytest.raises(ValueError, match="bilinmeyen preset"):
        trader.apply_preset("bogus")


def test_preset_endpoint(env, monkeypatch):
    monkeypatch.setattr(nb, "API_TOKEN", None)
    c = TestClient(nb.app)
    r = c.post("/settings/preset/news")
    assert r.status_code == 200 and r.json()["time_stop_min"] == 60


def test_preset_endpoint_unknown(env, monkeypatch):
    monkeypatch.setattr(nb, "API_TOKEN", None)
    c = TestClient(nb.app)
    assert c.post("/settings/preset/bogus").status_code == 400


def test_preset_endpoint_token_protected(env, monkeypatch):
    monkeypatch.setattr(nb, "API_TOKEN", "secret")
    c = TestClient(nb.app)
    assert c.post("/settings/preset/news").status_code == 401
    assert c.post("/settings/preset/news", headers={"X-API-Token": "secret"}).status_code == 200
