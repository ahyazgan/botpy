"""Ham sinyal scorecard: _directional_move + signal_scorecard + /scorecard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_backtest as nbt
import news_bot as nb
from storage import Store


def _candles(entry, last):
    return [[0, entry, entry, entry, entry, 0], [60_000, entry, max(entry, last), min(entry, last), last, 0]]


def _sig(direction, entry=100.0, last=110.0, source="TreeNews", impact=8):
    return {"symbol": "X", "direction": direction, "impact": impact, "source": source,
            "candles": _candles(entry, last)}


# ── _directional_move ──────────────────────────────────────────────────────
def test_directional_move_bullish_hit():
    assert nbt._directional_move(_sig("bullish", 100, 110)) == pytest.approx(10.0)


def test_directional_move_bearish_hit():
    # düşüş tahmini + fiyat düştü → pozitif (yön doğru)
    assert nbt._directional_move(_sig("bearish", 100, 90)) == pytest.approx(10.0)


def test_directional_move_miss():
    # yükseliş dedi ama düştü → negatif
    assert nbt._directional_move(_sig("bullish", 100, 95)) == pytest.approx(-5.0)


def test_directional_move_too_short():
    assert nbt._directional_move({"direction": "bullish", "candles": [[0, 1, 1, 1, 1, 0]]}) is None


# ── signal_scorecard ───────────────────────────────────────────────────────
def test_scorecard_grouping():
    sigs = [
        _sig("bullish", 100, 110, source="A", impact=9),   # hit +10
        _sig("bullish", 100, 90, source="A", impact=9),    # miss -10
        _sig("bearish", 100, 90, source="B", impact=7),    # hit +10
    ]
    sc = nbt.signal_scorecard(sigs)
    assert sc["n"] == 3
    assert sc["overall"]["hit_rate"] == round(2 / 3 * 100, 1)
    assert sc["by_source"]["A"]["n"] == 2 and sc["by_source"]["A"]["hit_rate"] == 50.0
    assert sc["by_source"]["B"]["hit_rate"] == 100.0
    assert sc["by_impact"]["9"]["avg_move_pct"] == 0.0   # (+10 −10)/2


def test_scorecard_empty():
    sc = nbt.signal_scorecard([])
    assert sc["n"] == 0 and sc["overall"]["hit_rate"] == 0.0


# ── endpoint ───────────────────────────────────────────────────────────────
def test_scorecard_endpoint(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "sc.db"))
    monkeypatch.setattr(nb, "_store", store)
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    store.add_signal({"id": "s1", "source": "TreeNews", "title": "t", "impact": 9,
                      "direction": "bullish", "symbol": "FOOUSDT", "coins": [],
                      "confirmed": True, "published": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(nbt, "prefetch", lambda sigs, m: [{**s, "candles": _candles(100, 110)} for s in sigs])
    c = TestClient(nb.app)
    d = c.get("/scorecard").json()
    assert d["ok"] is True and d["n"] == 1
    assert d["overall"]["hit_rate"] == 100.0
    store.close()


def test_scorecard_endpoint_empty(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "sc2.db"))
    monkeypatch.setattr(nb, "_store", store)
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    c = TestClient(nb.app)
    assert c.get("/scorecard?min_impact=10").json()["ok"] is False
    store.close()
