"""Gecikme kalıcılığı: storage latency_snapshots + snapshot throttle + /latency/history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import latency as lat
import news_bot as nb
from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "lat.db"))
    yield s
    s.close()


def _summary():
    return {
        "ingest": {"p50_ms": 100.0, "p95_ms": 200.0, "max_ms": 300.0, "count": 10},
        "pipeline": {"p50_ms": 500.0, "p95_ms": 900.0, "max_ms": 1200.0, "count": 8},
        "score": {"count": 0},   # örnek yok → yazılmaz
    }


# ── storage ──────────────────────────────────────────────────────────────
def test_add_snapshot_writes_per_stage(store):
    n = store.add_latency_snapshot(_summary())
    assert n == 2   # ingest + pipeline (score count=0 atlandı)
    span = store.latency_span()
    assert span["count"] == 2


def test_empty_summary_writes_nothing(store):
    assert store.add_latency_snapshot({}) == 0
    assert store.add_latency_snapshot({"x": {"count": 0}}) == 0


def test_history_filter_by_stage(store):
    store.add_latency_snapshot(_summary())
    rows = store.latency_history(stage="pipeline")
    assert len(rows) == 1
    assert rows[0]["stage"] == "pipeline"
    assert rows[0]["p95"] == pytest.approx(900.0)


def test_history_respects_time_window(store):
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    store.add_latency_snapshot({"ingest": {"p95_ms": 1.0, "count": 1}}, ts=old_ts)
    store.add_latency_snapshot({"ingest": {"p95_ms": 2.0, "count": 1}})  # şimdi
    recent = store.latency_history(hours=24)
    assert len(recent) == 1
    assert recent[0]["p95"] == pytest.approx(2.0)
    allrows = store.latency_history(hours=72)
    assert len(allrows) == 2


def test_history_ordered_ascending(store):
    t1 = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    t2 = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.add_latency_snapshot({"pipeline": {"p95_ms": 1.0, "count": 1}}, ts=t1)
    store.add_latency_snapshot({"pipeline": {"p95_ms": 2.0, "count": 1}}, ts=t2)
    rows = store.latency_history()
    assert [r["p95"] for r in rows] == [1.0, 2.0]


def test_prune_keeps_recent(store):
    for i in range(10):
        store.add_latency_snapshot({"pipeline": {"p95_ms": float(i), "count": 1}})
    deleted = store.prune_latency_snapshots(keep=4)
    assert deleted == 6
    assert store.latency_span()["count"] == 4


def test_prune_noop_when_keep_zero(store):
    store.add_latency_snapshot({"pipeline": {"p95_ms": 1.0, "count": 1}})
    assert store.prune_latency_snapshots(0) == 0


# ── snapshot throttle (_maybe_snapshot_latency) ──────────────────────────
def test_snapshot_throttled(monkeypatch, store):
    monkeypatch.setattr(nb, "get_store", lambda: store)
    monkeypatch.setattr(nb, "_last_latency_snapshot", 0.0)
    monkeypatch.setattr(nb, "LATENCY_SNAPSHOT_EVERY_SEC", 300.0)
    lat.reset()
    lat.record("pipeline", 500.0)
    nb._maybe_snapshot_latency()              # ilk çağrı yazar
    assert store.latency_span()["count"] == 1
    lat.record("pipeline", 600.0)
    nb._maybe_snapshot_latency()              # hemen tekrar → throttle, yazmaz
    assert store.latency_span()["count"] == 1
    lat.reset()


def test_snapshot_skips_empty(monkeypatch, store):
    monkeypatch.setattr(nb, "get_store", lambda: store)
    monkeypatch.setattr(nb, "_last_latency_snapshot", 0.0)
    lat.reset()                                # örnek yok
    nb._maybe_snapshot_latency()
    assert store.latency_span()["count"] == 0


# ── /latency/history endpoint ─────────────────────────────────────────────
def test_history_endpoint(monkeypatch, store):
    monkeypatch.setattr(nb, "get_store", lambda: store)
    store.add_latency_snapshot(_summary())
    d = TestClient(nb.app).get("/latency/history?stage=pipeline").json()
    assert d["ok"] is True
    assert len(d["points"]) == 1
    assert d["span"]["count"] == 2
