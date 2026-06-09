"""Audit log, emir-niyet günlüğü (crash recovery) ve mutabakat testleri."""

from __future__ import annotations

import asyncio

import pytest

import arb_bot as ab
from reconcile import diff_positions
from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "audit.db"))
    yield s
    s.close()


# ── audit log ────────────────────────────────────────────────────────────
def test_audit_log_records(store):
    store.log_event("order_send", market_id="m1", side="buy", price=5.0, detail="x")
    store.log_event("order_result", market_id="m1", status="filled")
    rows = store.list_audit()
    assert [r["event"] for r in rows] == ["order_result", "order_send"]  # DESC
    assert rows[1]["market_id"] == "m1"


# ── emir niyet günlüğü ───────────────────────────────────────────────────
def test_intent_open_close(store):
    store.open_intent("i1", "m1", "buy", detail="soru")
    assert [i["id"] for i in store.list_open_intents()] == ["i1"]
    assert store.close_intent("i1", "yes=fill no=fill") is True
    assert store.list_open_intents() == []
    # tekrar kapatma → False (zaten done)
    assert store.close_intent("i1", "x") is False


def test_open_intents_detect_orphans(store):
    store.open_intent("i1", "m1", "buy")
    store.open_intent("i2", "m2", "sell")
    store.close_intent("i1", "done")
    orphans = store.list_open_intents()
    assert [o["id"] for o in orphans] == ["i2"]   # i1 kapandı, i2 öksüz


# ── reconcile.diff_positions ─────────────────────────────────────────────
def test_reconcile_all_match():
    r = diff_positions({"a": 10.0, "b": 5.0}, {"a": 10.0, "b": 5.0})
    assert r["ok"] is True


def test_reconcile_missing_on_chain():
    # yerelde var, zincirde yok → sahip sandığımız ama yokmuş
    r = diff_positions({"a": 10.0}, {})
    assert r["ok"] is False
    assert "a" in r["missing_on_chain"]


def test_reconcile_unexpected_on_chain():
    r = diff_positions({}, {"a": 7.0})
    assert "a" in r["unexpected_on_chain"]


def test_reconcile_mismatch():
    r = diff_positions({"a": 10.0}, {"a": 8.0})
    assert "a" in r["mismatched"]
    assert r["mismatched"]["a"] == {"local": 10.0, "chain": 8.0}


# ── execute_arb audit + intent entegrasyonu ──────────────────────────────
class _AuditStore:
    def __init__(self):
        self.events = []
        self.intents = {}

    def open_intent(self, iid, market_id, direction, detail=None):
        self.intents[iid] = "open"
        return iid

    def close_intent(self, iid, result):
        if self.intents.get(iid) == "open":
            self.intents[iid] = "done"
            return True
        return False

    def log_event(self, event, **kw):
        self.events.append(event)
        return len(self.events)


@pytest.mark.asyncio
async def test_execute_arb_writes_audit(monkeypatch):
    def _ok(client, token_id, side, price, size):
        return {"success": True, "status": "matched"}

    monkeypatch.setattr(ab, "_place_order_sync", _ok)
    store = _AuditStore()
    m = ab.Market(
        id="m1", question="Q?", yes_token_id="YT", no_token_id="NT",
        yes_bid=0.4, yes_ask=0.45, no_bid=0.4, no_ask=0.45, volume24h=1.0,
    )
    opp = ab.ArbOpportunity(m, "buy", 10.0, 0.45, 0.45)

    await ab.execute_arb(
        client=None, opp=opp, loop=asyncio.get_event_loop(),
        budget=ab.Budget(1000.0), dry_run=False, store=store,
    )

    # niyet açıldı ve kapandı
    assert store.intents and all(v == "done" for v in store.intents.values())
    assert "order_send" in store.events and "order_result" in store.events
