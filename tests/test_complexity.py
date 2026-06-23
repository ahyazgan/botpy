"""Karmaşıklık/overfitting denetimi + lean preset."""

from __future__ import annotations

import pytest

import trader


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    yield


# ── lean preset ──────────────────────────────────────────────────────────
def test_lean_preset_strips_speculative_layers():
    # Önce her şeyi aç
    for f in ("use_entry_brain", "brain_escalate", "size_by_kelly", "risk_parity",
              "portfolio_risk", "regime_adapt", "auto_tune", "suppress_losing_sources",
              "use_learned_vetoes", "size_by_impact", "size_by_volume", "brain_self_improve"):
        setattr(trader.S, f, True)
    trader.S.brain_vote_count = 5
    trader.S.time_stop_min = 60
    trader.apply_preset("lean")
    # Hepsi kapanmalı
    assert trader.S.use_entry_brain is False
    assert trader.S.size_by_kelly is False
    assert trader.S.regime_adapt is False
    assert trader.S.brain_vote_count == 1
    assert trader.S.time_stop_min == 0
    # Çekirdek güvenlik korunur
    assert trader.S.auto_require_confirm is True


def test_lean_is_registered():
    assert "lean" in trader.PRESETS


# ── complexity_audit ─────────────────────────────────────────────────────
def test_clean_default_is_lean():
    # Varsayılan Settings: tüm opt-in katmanlar kapalı → YALIN
    out = trader.complexity_audit(0)
    assert out["n_active_layers"] == 0
    assert out["verdict"].startswith("YALIN")
    assert out["premature"] == []


def test_premature_layer_flagged():
    trader.S.size_by_kelly = True          # kelly_min_trades=20 gerekli
    out = trader.complexity_audit(5)       # yalnız 5 işlem
    assert any("Kelly" in p for p in out["premature"])
    assert out["verdict"].startswith("ERKEN KARMAŞIKLIK")
    kelly = next(x for x in out["active_layers"] if "Kelly" in x["layer"])
    assert kelly["category"] == "premature"


def test_data_ready_when_enough_trades():
    trader.S.size_by_kelly = True
    out = trader.complexity_audit(50)      # bol veri
    assert out["premature"] == []
    kelly = next(x for x in out["active_layers"] if "Kelly" in x["layer"])
    assert kelly["category"] == "data-ready"


def test_structural_layer_not_premature():
    trader.S.portfolio_risk = True         # yapısal — geçmiş veri gerektirmez
    out = trader.complexity_audit(0)
    pr = next(x for x in out["active_layers"] if "Portföy" in x["layer"])
    assert pr["category"] == "structural"
    assert out["premature"] == []


def test_claude_cost_multiplier():
    trader.S.use_entry_brain = True
    trader.S.brain_vote_count = 3
    trader.S.brain_escalate = True
    out = trader.complexity_audit(100)
    assert out["claude_cost"]["calls_per_qualifying_entry"] == pytest.approx(3.3)
    assert any("Claude maliyeti" in a for a in out["advice"])


def test_no_brain_zero_cost():
    out = trader.complexity_audit(100)
    assert out["claude_cost"]["calls_per_qualifying_entry"] == 0
