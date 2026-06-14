"""Sinyal arşivi: news_bot kalıcılık + /signals + news_backtest --db modu."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import news_backtest as nbt
import news_bot as nb
from news_bot import NewsItem
from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "sig.db"))
    yield s
    s.close()


def _item(sid="s1", impact=8, **kw) -> NewsItem:
    base = dict(
        id=sid, source="TreeNews", title="Binance lists FOO — listing",
        url="https://x/foo", published=None,
        fetched_at="2026-06-14T00:00:00+00:00",
        coins=["FOO"], impact=impact, direction="bullish",
        symbol="FOOUSDT", confirmed=True,
    )
    base.update(kw)
    return NewsItem(**base)


# ── news_bot arşivleme bağlantısı ──────────────────────────────────────────
def test_archive_signal_writes_to_store(monkeypatch, store):
    monkeypatch.setattr(nb, "_store", store)
    nb._archive_signal(_item("a", impact=9))
    rows = store.list_signals()
    assert [r["id"] for r in rows] == ["a"]
    assert rows[0]["symbol"] == "FOOUSDT"


def test_archive_signal_swallows_errors(monkeypatch):
    class _Boom:
        def add_signal(self, _):
            raise RuntimeError("db down")
    monkeypatch.setattr(nb, "_store", _Boom())
    nb._archive_signal(_item("x"))  # exception fırlatmamalı


def test_get_signals_endpoint(monkeypatch, store):
    monkeypatch.setattr(nb, "_store", store)
    store.add_signal(_item("low", impact=5).to_dict())
    store.add_signal(_item("high", impact=9).to_dict())
    resp = nb.get_signals(min_impact=7)
    assert [s["id"] for s in resp["signals"]] == ["high"]
    assert resp["count"] == 2          # span tüm kayıtları sayar


# ── news_backtest --db modu ────────────────────────────────────────────────
def _old_iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_fetch_signals_from_db_roundtrip(tmp_path):
    path = str(tmp_path / "bt.db")
    s = Store(path)
    # yeterince eski (>30dk) → backtest'e girer
    s.add_signal(_item("old", impact=8, published=_old_iso(120)).to_dict())
    # çok yeni (<30dk) → atlanır
    s.add_signal(_item("fresh", impact=8, published=_old_iso(5)).to_dict())
    # yön neutral / symbol yok → atlanır
    s.add_signal(_item("neutral", impact=8, direction="neutral",
                       published=_old_iso(120)).to_dict())
    s.close()

    sigs = nbt.fetch_signals_from_db(path, min_impact=7)
    assert [x["symbol"] for x in sigs] == ["FOOUSDT"]
    assert sigs[0]["direction"] == "bullish"


def test_signals_from_rows_filters():
    rows = [
        {"symbol": "FOOUSDT", "direction": "bullish", "impact": 8,
         "title": "t", "published": _old_iso(120)},
        {"symbol": None, "direction": "bullish", "impact": 8,
         "title": "t", "published": _old_iso(120)},          # symbol yok
        {"symbol": "X", "direction": "neutral", "impact": 8,
         "title": "t", "published": _old_iso(120)},          # neutral
    ]
    out = nbt._signals_from_rows(rows)
    assert len(out) == 1 and out[0]["symbol"] == "FOOUSDT"
