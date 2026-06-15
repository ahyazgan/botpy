"""Backtest çalıştırma kalıcılığı: storage + /backtest persist + /backtest/runs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_backtest as nbt
import news_bot as nb
from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "bt.db"))
    yield s
    s.close()


def test_add_and_list_backtest_run(store):
    rid = store.add_backtest_run({
        "mode": "simple", "sl": 3.0, "tp": 6.0, "fee": 0.2, "usdt": 100.0,
        "hours": 4.0, "min_impact": 7, "n": 5, "win_rate": 60.0,
        "avg_net_pct": 1.2, "total_pnl_usdt": 6.0, "note": "",
    })
    assert rid > 0
    runs = store.list_backtest_runs()
    assert len(runs) == 1 and runs[0]["mode"] == "simple" and runs[0]["n"] == 5
    assert runs[0]["ts"] is not None


def test_list_newest_first(store):
    for i in range(3):
        store.add_backtest_run({"mode": "simple", "n": i, "win_rate": 0.0,
                                "total_pnl_usdt": 0.0, "avg_net_pct": 0.0})
    runs = store.list_backtest_runs()
    assert [r["n"] for r in runs] == [2, 1, 0]


def _win_candles():
    return [[0, 100, 100, 100, 100, 0], [60_000, 100, 130, 99, 125, 0]]


def test_backtest_persists_and_runs_endpoint(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "e.db"))
    monkeypatch.setattr(nb, "_store", s)
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    for i in range(4):
        s.add_signal({"id": f"s{i}", "source": "TreeNews", "title": "t", "impact": 9,
                      "direction": "bullish", "symbol": "FOOUSDT", "coins": [],
                      "confirmed": True, "published": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(nbt, "prefetch", lambda sigs, m: [{**x, "candles": _win_candles()} for x in sigs])
    c = TestClient(nb.app)
    # simple backtest → kayıt yazılmalı
    d = c.get("/backtest?mode=simple").json()
    assert d["ok"] is True and d["n"] == 4
    runs = c.get("/backtest/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["mode"] == "simple" and runs[0]["n"] == 4
    # grid de ayrı kayıt yazar
    c.get("/backtest?mode=grid")
    assert len(c.get("/backtest/runs").json()["runs"]) == 2
    s.close()


def test_empty_backtest_no_persist(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "e2.db"))
    monkeypatch.setattr(nb, "_store", s)
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    c = TestClient(nb.app)
    assert c.get("/backtest?min_impact=10").json()["ok"] is False
    assert c.get("/backtest/runs").json()["runs"] == []   # boş → kayıt yok
    s.close()
