"""Operasyonel olay zaman çizelgesi: storage + geçiş tespiti + /events endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader
from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "ops.db"))
    yield s
    s.close()


# ── storage ──────────────────────────────────────────────────────────────
def test_add_and_list(store):
    store.add_ops_event("feed_stale", "warn", "WS kopuk", "treenews")
    store.add_ops_event("feed_recovered", "info", "geri geldi", "treenews")
    rows = store.list_ops_events()
    assert len(rows) == 2
    assert rows[0]["kind"] == "feed_recovered"   # en yeni başta
    assert rows[1]["severity"] == "warn"


def test_filter_by_kind_and_severity(store):
    store.add_ops_event("source_disabled", "warn", "", "RSS")
    store.add_ops_event("halt_tripped", "critical", "emir hatası", "")
    assert len(store.list_ops_events(kind="halt_tripped")) == 1
    crit = store.list_ops_events(severity="critical")
    assert len(crit) == 1 and crit[0]["kind"] == "halt_tripped"


def test_span_counts_last24h(store):
    store.add_ops_event("latency_breach", "warn", "", "pipeline")
    store.add_ops_event("halt_tripped", "critical", "", "")
    span = store.ops_event_span()
    assert span["count"] == 2
    assert span["last24h"].get("critical") == 1
    assert span["last24h"].get("warn") == 1


def test_prune_keeps_recent(store):
    for i in range(8):
        store.add_ops_event("latency_breach", "warn", str(i), "pipeline")
    assert store.prune_ops_events(keep=3) == 5
    assert store.ops_event_span()["count"] == 3


# ── _record_event ────────────────────────────────────────────────────────
def test_record_event_writes(monkeypatch, store):
    monkeypatch.setattr(nb, "get_store", lambda: store)
    nb._record_event("feed_stale", "warn", "test", "treenews")
    rows = store.list_ops_events()
    assert rows[0]["kind"] == "feed_stale" and rows[0]["detail"] == "test"


# ── _check_ops_transitions (geçiş tespiti) ───────────────────────────────
@pytest.fixture()
def ops_env(monkeypatch, store):
    monkeypatch.setattr(nb, "get_store", lambda: store)
    monkeypatch.setattr(nb, "_ops_state",
                        {"latency_breaches": set(), "halt_active": False, "drawdown_halt": False})
    yield store


def test_latency_breach_onset_and_clear(ops_env, monkeypatch):
    monkeypatch.setattr(trader, "get_halt", lambda: {"active": False, "reason": ""})
    # Aşama yavaşladı → tek breach olayı
    monkeypatch.setattr(nb, "_latency_breaches", lambda: ["pipeline"])
    nb._check_ops_transitions()
    # Aynı durum devam → YENİ olay yok (spam yok)
    nb._check_ops_transitions()
    breaches = ops_env.list_ops_events(kind="latency_breach")
    assert len(breaches) == 1
    # Düzeldi → clear olayı
    monkeypatch.setattr(nb, "_latency_breaches", lambda: [])
    nb._check_ops_transitions()
    assert len(ops_env.list_ops_events(kind="latency_clear")) == 1


def test_halt_trip_and_clear(ops_env, monkeypatch):
    monkeypatch.setattr(nb, "_latency_breaches", lambda: [])
    halt = {"active": True, "reason": "emir-hata serisi"}
    monkeypatch.setattr(trader, "get_halt", lambda: halt)
    nb._check_ops_transitions()
    trips = ops_env.list_ops_events(kind="halt_tripped")
    assert len(trips) == 1 and trips[0]["severity"] == "critical"
    # Temizlendi
    halt2 = {"active": False, "reason": ""}
    monkeypatch.setattr(trader, "get_halt", lambda: halt2)
    nb._check_ops_transitions()
    assert len(ops_env.list_ops_events(kind="halt_cleared")) == 1


# ── /events endpoint ─────────────────────────────────────────────────────
def test_events_endpoint(monkeypatch, store):
    monkeypatch.setattr(nb, "get_store", lambda: store)
    store.add_ops_event("source_disabled", "warn", "boom", "RSS")
    d = TestClient(nb.app).get("/events?kind=source_disabled").json()
    assert d["ok"] is True
    assert len(d["events"]) == 1
    assert d["span"]["count"] == 1
