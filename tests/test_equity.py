"""trader equity (kümülatif P&L) eğrisi testleri (saf fonksiyon)."""

from __future__ import annotations

import trader


def _c(pnl, closed_at="2026-06-14T00:00:00+00:00"):
    return {"pnl": pnl, "closed_at": closed_at}


def test_equity_cumulative():
    closed = [_c(5.0), _c(-2.0), _c(3.0)]
    curve = trader._equity_from(closed)
    assert [p["cumulative"] for p in curve] == [5.0, 3.0, 6.0]
    assert [p["pnl"] for p in curve] == [5.0, -2.0, 3.0]


def test_equity_skips_none_pnl():
    closed = [_c(4.0), {"pnl": None, "closed_at": "x"}, _c(1.0)]
    curve = trader._equity_from(closed)
    assert len(curve) == 2
    assert [p["cumulative"] for p in curve] == [4.0, 5.0]


def test_equity_empty():
    assert trader._equity_from([]) == []


def test_equity_preserves_closed_at():
    curve = trader._equity_from([_c(1.0, "2026-06-14T10:00:00+00:00")])
    assert curve[0]["closed_at"] == "2026-06-14T10:00:00+00:00"


def test_get_performance_includes_equity():
    perf = trader.get_performance()
    assert "equity" in perf and isinstance(perf["equity"], list)
