"""Paralel fiyat teyidi: _confirm_alerts (çoklu alert'te eşzamanlı)."""

from __future__ import annotations

import threading
import time

import news_bot as nb
from news_bot import NewsItem


def _item(sid):
    return NewsItem(id=sid, source="S", title="t", url="u", published=None,
                    fetched_at="2026-06-15T00:00:00+00:00", coins=["FOO"],
                    impact=9, direction="bullish")


def test_empty_no_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(nb, "confirm_with_price", lambda s, it: calls.append(it.id))
    nb._confirm_alerts(None, [])
    assert calls == []


def test_single_runs_inline(monkeypatch):
    calls = []
    monkeypatch.setattr(nb, "confirm_with_price", lambda s, it: calls.append(it.id))
    nb._confirm_alerts(None, [_item("a")])
    assert calls == ["a"]


def test_all_confirmed_in_parallel(monkeypatch):
    seen: set[str] = set()
    threads: set[int] = set()
    lock = threading.Lock()

    def fake(s, it):
        time.sleep(0.02)
        with lock:
            seen.add(it.id)
            threads.add(threading.get_ident())

    monkeypatch.setattr(nb, "confirm_with_price", fake)
    items = [_item(f"s{i}") for i in range(5)]
    t0 = time.monotonic()
    nb._confirm_alerts(None, items)
    elapsed = time.monotonic() - t0
    assert seen == {f"s{i}" for i in range(5)}   # hepsi teyit edildi
    assert len(threads) > 1                       # gerçekten paralel
    assert elapsed < 0.08                         # seri olsa ~0.10s; paralel < 0.08


def test_exception_does_not_break_others(monkeypatch):
    seen: set[str] = set()

    def fake(s, it):
        if it.id == "bad":
            raise RuntimeError("teyit patladı")
        seen.add(it.id)

    monkeypatch.setattr(nb, "confirm_with_price", fake)
    nb._confirm_alerts(None, [_item("a"), _item("bad"), _item("c")])
    assert seen == {"a", "c"}                     # patlayan diğerlerini etkilemedi
