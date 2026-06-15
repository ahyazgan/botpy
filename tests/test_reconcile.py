"""Canlı pozisyon mutabakatı (read-only): reconcile_positions + /reconcile."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader


@pytest.fixture()
def clean(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [
        {"id": "p1", "symbol": "FOOUSDT"},
        {"id": "p2", "symbol": "BARUSDT"},
    ])
    yield


def test_matched_and_orphans(clean):
    # borsa yalnızca FOOUSDT biliyor → BARUSDT orphan
    r = trader.reconcile_positions(exchange_symbols={"FOOUSDT"})
    assert r["checked"] is True
    assert [o["symbol"] for o in r["orphans"]] == ["BARUSDT"]
    assert [m["symbol"] for m in r["matched"]] == ["FOOUSDT"]


def test_all_matched(clean):
    r = trader.reconcile_positions(exchange_symbols={"FOOUSDT", "BARUSDT"})
    assert r["orphans"] == [] and len(r["matched"]) == 2


def test_skips_in_paper_mode(clean, monkeypatch):
    monkeypatch.setattr(trader.S, "paper_trading", True)
    r = trader.reconcile_positions()           # exchange_symbols=None → canlı sorgu denenir
    assert r["checked"] is False and "paper" in r["reason"]
    assert r["orphans"] == []


def test_no_auto_close(clean):
    """Mutabakat pozisyonları kapatmamalı (yalnızca rapor)."""
    trader.reconcile_positions(exchange_symbols=set())   # hepsi orphan
    assert len(trader._positions) == 2                   # dokunulmadı


def test_endpoint(clean, monkeypatch):
    monkeypatch.setattr(trader.S, "paper_trading", True)
    c = TestClient(nb.app)
    d = c.get("/reconcile").json()
    assert d["checked"] is False
