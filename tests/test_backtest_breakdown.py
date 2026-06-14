"""news_backtest edge analitiği: simulate_all + _summarize + breakdown."""

from __future__ import annotations

import news_backtest as nbt


def _win_candles():
    return [[0, 100, 100, 100, 100, 0], [60_000, 100, 130, 99, 125, 0]]


def _loss_candles():
    return [[0, 100, 100, 100, 100, 0], [60_000, 100, 101, 90, 92, 0]]


def _sig(impact, direction="bullish", source="TreeNews", win=True):
    return {"symbol": "X", "direction": direction, "time": 0, "impact": impact,
            "title": "t", "source": source, "candles": _win_candles() if win else _loss_candles()}


def test_simulate_all_filters_invalid():
    sigs = [_sig(8), {"direction": "bullish", "candles": [[0, 1, 1, 1, 1, 0]]}]  # 2. çok kısa
    out = nbt.simulate_all(sigs, sl=3, tp=6, fee=0.2)
    assert len(out) == 1


def test_summarize_basic():
    res = [{"outcome": "tp", "net_pct": 5.0}, {"outcome": "sl", "net_pct": -3.0}]
    s = nbt._summarize(res, usdt=100.0)
    assert s["n"] == 2 and s["win_rate"] == 50.0
    assert s["tp"] == 1 and s["sl"] == 1
    assert nbt._summarize([], 100.0) == {"n": 0}


def test_breakdown_by_impact_direction_source():
    results = [
        {"impact": 9, "direction": "bullish", "source": "A", "outcome": "tp", "net_pct": 6.0},
        {"impact": 9, "direction": "bullish", "source": "A", "outcome": "tp", "net_pct": 4.0},
        {"impact": 7, "direction": "bearish", "source": "B", "outcome": "sl", "net_pct": -3.0},
    ]
    b = nbt.breakdown(results, usdt=100.0)
    assert b["by_impact"]["9"]["n"] == 2 and b["by_impact"]["9"]["win_rate"] == 100.0
    assert b["by_impact"]["7"]["win_rate"] == 0.0
    assert b["by_direction"]["bullish"]["n"] == 2
    assert b["by_source"]["A"]["avg_net_pct"] == 5.0
    assert b["by_source"]["B"]["avg_net_pct"] == -3.0


def test_breakdown_integrates_with_simulate():
    sigs = [_sig(9, win=True), _sig(9, win=True), _sig(7, win=False)]
    results = nbt.simulate_all(sigs, sl=3, tp=6, fee=0.2)
    b = nbt.breakdown(results)
    # güç 9 hep kazanır, güç 7 hep kaybeder → dilim ayrımı net
    assert b["by_impact"]["9"]["win_rate"] == 100.0
    assert b["by_impact"]["7"]["win_rate"] == 0.0


def test_signals_from_rows_keeps_source():
    rows = [{"symbol": "X", "direction": "bullish", "impact": 8, "title": "t",
             "source": "Binance", "published": "2020-01-01T00:00:00+00:00"}]
    out = nbt._signals_from_rows(rows)
    assert out[0]["source"] == "Binance"
