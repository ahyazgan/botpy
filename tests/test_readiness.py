"""Canlıya hazırlık kokpiti: /readiness verdikt mantığı."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(nb, "_settings_loaded", True)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    return TestClient(nb.app)


def _perf(n, pf, wr=55.0, dd=-12.0):
    return {"total_trades": n, "profit_factor": pf, "win_rate": wr, "max_drawdown": dd}


def test_insufficient_data(client, monkeypatch):
    monkeypatch.setattr(trader, "get_performance", lambda: _perf(10, None))
    monkeypatch.setattr(trader, "brain_scorecard", lambda: {"samples": 0, "calibrated": None})
    d = client.get("/readiness").json()
    assert d["verdict"].startswith("VERİ YETERSİZ")
    assert all(c["status"] == "pending" for c in d["checks"])


def test_not_ready_weak_pf(client, monkeypatch):
    monkeypatch.setattr(trader, "get_performance", lambda: _perf(60, 0.9))
    monkeypatch.setattr(trader, "brain_scorecard", lambda: {"samples": 20, "calibrated": True})
    d = client.get("/readiness").json()
    assert d["verdict"].startswith("HENÜZ DEĞİL")
    pf_check = next(c for c in d["checks"] if c["check"].startswith("Profit"))
    assert pf_check["status"] == "fail"


def test_promising_all_pass(client, monkeypatch):
    monkeypatch.setattr(trader, "get_performance", lambda: _perf(80, 1.8))
    monkeypatch.setattr(trader, "brain_scorecard", lambda: {"samples": 30, "calibrated": True})
    d = client.get("/readiness").json()
    assert d["verdict"].startswith("UMUT VERİCİ")
    assert all(c["status"] == "pass" for c in d["checks"])


def test_developing_pending_calibration(client, monkeypatch):
    # yeterli işlem + iyi pf ama beyin örneği az → GELİŞİYOR
    monkeypatch.setattr(trader, "get_performance", lambda: _perf(60, 1.5))
    monkeypatch.setattr(trader, "brain_scorecard", lambda: {"samples": 2, "calibrated": None})
    d = client.get("/readiness").json()
    assert d["verdict"].startswith("GELİŞİYOR")
