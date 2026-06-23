"""Ablation derinliği: açgözlü çok-gate araması + beyin-katman atıfı."""

from __future__ import annotations

import pytest

import trader
from news_backtest import ablation_search


def _res(net, *, impact=8, confirmed=None, rvol=None, p24=None, direction="bullish"):
    return {"net_pct": net, "outcome": "tp" if net > 0 else "sl", "impact": impact,
            "direction": direction, "source": "X", "confirmed": confirmed,
            "rel_volume": rvol, "price_24h_pct": p24}


# ── ablation_search ──────────────────────────────────────────────────────
def test_search_selects_paying_gate():
    # Teyitsizler kaybeden → confirmed gate seçilmeli + öneri üretmeli
    res = [_res(4.0, confirmed=True) for _ in range(8)]
    res += [_res(-3.0, confirmed=False) for _ in range(8)]
    out = ablation_search(res)
    gates = [s["gate"] for s in out["selected"]]
    assert "confirmed" in gates
    assert out["recommended_settings"].get("auto_require_confirm") is True
    assert out["improvement_pct"] > 0


def test_search_selects_nothing_when_no_edge():
    # Tüm alt-kümeler benzer kazançlı → hiçbir gate anlamlı iyileşme katmaz
    res = [_res(3.0, confirmed=True) for _ in range(8)]
    res += [_res(3.0, confirmed=False) for _ in range(8)]
    out = ablation_search(res)
    assert out["selected"] == []
    assert out["recommended_settings"] == {}
    assert "hiçbir gate" in out["verdict"]


def test_search_combines_multiple_gates():
    # Hem teyitsiz hem düşük-rvol kaybeden → ikisi de seçilebilir
    res = [_res(5.0, confirmed=True, rvol=3.0) for _ in range(10)]
    res += [_res(-3.0, confirmed=False, rvol=3.0) for _ in range(6)]
    res += [_res(-2.0, confirmed=True, rvol=0.4) for _ in range(6)]
    out = ablation_search(res)
    assert len(out["selected"]) >= 1
    assert out["final"]["avg_net_pct"] >= out["baseline"]["avg_net_pct"]


def test_search_respects_min_subset():
    # Çok az kaybeden (< min_subset) → gate seçilmez (aşırı-uydurma freni)
    res = [_res(3.0, confirmed=True) for _ in range(20)]
    res += [_res(-3.0, confirmed=False) for _ in range(2)]
    out = ablation_search(res, min_subset=5)
    assert out["selected"] == []


# ── brain_attribution ────────────────────────────────────────────────────
def _closed(pnl, brain):
    return {"pnl": pnl, "brain": brain}


@pytest.fixture()
def closed_env(monkeypatch):
    monkeypatch.setattr(trader, "_lock", trader.threading.Lock())
    yield


def test_attribution_empty_when_no_brain_trades(monkeypatch, closed_env):
    monkeypatch.setattr(trader, "_closed", [])
    out = trader.brain_attribution()
    assert out["samples"] == 0
    assert out["layers"]["escalation"]["verdict"] == "yetersiz-veri"


def test_attribution_escalation_edge(monkeypatch, closed_env):
    # Eskale edilenler kazanıyor, taban kaybediyor → escalation edge+
    rows = [_closed(5.0, {"conviction": 0.7, "escalated": True}) for _ in range(6)]
    rows += [_closed(-2.0, {"conviction": 0.5, "escalated": False}) for _ in range(6)]
    monkeypatch.setattr(trader, "_closed", rows)
    out = trader.brain_attribution()
    assert out["layers"]["escalation"]["verdict"] == "edge+"


def test_attribution_recalibration_shift(monkeypatch, closed_env):
    rows = [_closed(1.0, {"conviction": 0.4, "conviction_raw": 0.8}) for _ in range(6)]
    monkeypatch.setattr(trader, "_closed", rows)
    out = trader.brain_attribution()
    rec = out["layers"]["recalibration"]
    assert rec["n"] == 6
    assert rec["avg_shift"] == pytest.approx(-0.4)


def test_attribution_voting_layer(monkeypatch, closed_env):
    rows = [_closed(4.0, {"conviction": 0.7, "vote": {"agreement": 1.0}}) for _ in range(6)]
    rows += [_closed(-2.0, {"conviction": 0.6, "vote": {"agreement": 0.6}}) for _ in range(6)]
    monkeypatch.setattr(trader, "_closed", rows)
    out = trader.brain_attribution()
    v = out["layers"]["voting"]
    assert v["n"] == 12
    assert v["verdict"] == "edge+"   # oybirliği bölünmüşten iyi
