"""Ablation önerisi uygulama: korkuluklar + /ablation/apply endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    yield


# ── apply_ablation_recommendation (korkuluklar) ──────────────────────────
def test_applies_gate_settings():
    trader.S.auto_min_impact = 7
    trader.S.min_rel_volume = 0.0
    out = trader.apply_ablation_recommendation(
        {"auto_min_impact": 9, "auto_require_confirm": True, "min_rel_volume": 1.5})
    assert out["applied"] is True
    assert trader.S.auto_min_impact == 9
    assert trader.S.min_rel_volume == 1.5
    fields = {c["field"] for c in out["changes"]}
    assert "auto_min_impact" in fields


def test_clamps_out_of_range():
    trader.S.auto_min_impact = 8
    trader.apply_ablation_recommendation({"auto_min_impact": 99, "min_rel_volume": 100.0})
    assert trader.S.auto_min_impact == 10          # [7,10] tavan
    assert trader.S.min_rel_volume == 5.0          # [0,5] tavan


def test_floor_clamp_impact():
    trader.apply_ablation_recommendation({"auto_min_impact": 2})
    assert trader.S.auto_min_impact == 7           # taban 7


def test_ignores_unknown_keys():
    # Para-büyüklüğü/risk alanları öneride olsa bile UYGULANMAZ (yalnız izinli gate'ler)
    trader.S.trade_usdt = 100.0
    trader.S.leverage = 1
    out = trader.apply_ablation_recommendation(
        {"trade_usdt": 9999, "leverage": 50, "max_total_exposure_usdt": 0})
    assert out["applied"] is False
    assert trader.S.trade_usdt == 100.0
    assert trader.S.leverage == 1


def test_require_confirm_only_tightens():
    # auto_require_confirm yalnız True'ya çevrilir (False ile gevşetilemez)
    trader.S.auto_require_confirm = True
    out = trader.apply_ablation_recommendation({"auto_require_confirm": False})
    assert out["applied"] is False
    assert trader.S.auto_require_confirm is True


def test_noop_when_already_at_target():
    trader.S.auto_min_impact = 9
    out = trader.apply_ablation_recommendation({"auto_min_impact": 9})
    assert out["applied"] is False
    assert out["changes"] == []


# ── /ablation/apply endpoint ─────────────────────────────────────────────
@pytest.fixture()
def client():
    return TestClient(nb.app)


def test_endpoint_applies_recommendation(client, monkeypatch):
    trader.S.auto_min_impact = 7
    monkeypatch.setattr(nb, "_ablation_search_impl",
                        lambda *a, **k: {"ok": True, "recommended_settings": {"auto_min_impact": 9},
                                         "improvement_pct": 1.2})
    monkeypatch.setattr(nb, "notify_remote", lambda m: None)
    d = client.post("/ablation/apply").json()
    assert d["applied"] is True
    assert trader.S.auto_min_impact == 9


def test_endpoint_noop_when_no_recommendation(client, monkeypatch):
    monkeypatch.setattr(nb, "_ablation_search_impl",
                        lambda *a, **k: {"ok": True, "recommended_settings": {}, "verdict": "yok"})
    d = client.post("/ablation/apply").json()
    assert d["applied"] is False
    assert "öneri yok" in d["reason"]


def test_endpoint_handles_search_failure(client, monkeypatch):
    monkeypatch.setattr(nb, "_ablation_search_impl",
                        lambda *a, **k: {"ok": False, "reason": "arşiv boş"})
    d = client.post("/ablation/apply").json()
    assert d["applied"] is False
    assert d["reason"] == "arşiv boş"
