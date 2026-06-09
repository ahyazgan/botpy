"""optimize.grid_search ve parametrik backtest testleri."""

from __future__ import annotations

from backtest import run_backtest
from optimize import DEFAULT_GRID, grid_search


def _series_tp_path():
    # Sinyal → fiyat yükselir; farklı TP eşikleri farklı sonuç verir
    return {
        "m1": [
            {"bid": 0.44, "ask": 0.45, "spread": 0.01},   # aç @ 0.45
            {"bid": 0.50, "ask": 0.51, "spread": 0.01},   # +%11
            {"bid": 0.60, "ask": 0.61, "spread": 0.01},   # +%33
        ]
    }


def test_run_backtest_signal_params_gate_entry():
    series = {"m1": [{"bid": 0.44, "ask": 0.45, "spread": 0.01},
                     {"bid": 0.60, "ask": 0.61, "spread": 0.01}]}
    # max_spread çok düşük → sinyal yok → işlem yok
    res = run_backtest(series, max_spread=0.0001)
    assert res["stats"]["count"] == 0
    # normal eşik → işlem var
    res2 = run_backtest(series)
    assert res2["stats"]["count"] == 1


def test_grid_search_ranks_by_objective():
    series = _series_tp_path()
    grid = {"take_profit_pct": [10.0, 25.0], "stop_loss_pct": [15.0]}
    results = grid_search(series, grid, objective="total_pnl", min_trades=1)
    assert len(results) == 2
    # azalan sıralı
    assert results[0]["score"] >= results[1]["score"]
    assert "take_profit_pct" in results[0]["params"]


def test_grid_search_min_trades_filters():
    series = _series_tp_path()
    grid = {"take_profit_pct": [20.0], "stop_loss_pct": [15.0]}
    # min_trades çok yüksek → hiçbir kombinasyon geçemez
    assert grid_search(series, grid, min_trades=999) == []


def test_grid_search_empty_grid():
    assert grid_search(_series_tp_path(), {}) == []


def test_grid_search_default_grid_runs():
    series = _series_tp_path()
    results = grid_search(series, DEFAULT_GRID, min_trades=1, top=5)
    assert 1 <= len(results) <= 5
    assert all("score" in r and "stats" in r for r in results)


def test_grid_search_objective_sharpe():
    series = _series_tp_path()
    grid = {"take_profit_pct": [10.0, 30.0], "stop_loss_pct": [15.0]}
    results = grid_search(series, grid, objective="sharpe", min_trades=1)
    assert all(isinstance(r["score"], float) for r in results)
