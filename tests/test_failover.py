"""Kaynak redundansı: WS bayatken yedek tarama cadence'i hızlanır (_scan_interval)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb


@pytest.fixture()
def client():
    return TestClient(nb.app)


def test_normal_cadence_when_feed_healthy(monkeypatch):
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: False)
    assert nb._scan_interval() == nb.SCAN_INTERVAL_SEC


def test_fast_cadence_when_feed_stale(monkeypatch):
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: True)
    iv = nb._scan_interval()
    assert iv == nb.SCAN_INTERVAL_FAST_SEC
    assert iv < nb.SCAN_INTERVAL_SEC   # failover gerçekten hızlandırıyor


def test_fast_never_exceeds_normal(monkeypatch):
    # Yanlış yapılandırma (FAST > NORMAL) bile normalden yavaş tarama üretmemeli
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: True)
    monkeypatch.setattr(nb, "SCAN_INTERVAL_FAST_SEC", 999)
    assert nb._scan_interval() <= nb.SCAN_INTERVAL_SEC


def test_health_exposes_active_interval(client, monkeypatch):
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: True)
    d = client.get("/health").json()
    assert d["backup_scan_interval_sec"] == nb.SCAN_INTERVAL_FAST_SEC


def test_metrics_exposes_failover_gauge(client, monkeypatch):
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: False)
    text = client.get("/metrics").text
    assert "botpy_failover_scans_total" in text
    assert "botpy_backup_scan_interval_seconds" in text
