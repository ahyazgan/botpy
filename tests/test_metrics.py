"""metrics.compute_stats ve backtest.run_backtest testleri."""

from __future__ import annotations

import pytest

from backtest import run_backtest
from metrics import compute_stats


# ── compute_stats ────────────────────────────────────────────────────────
def test_stats_empty():
    s = compute_stats([])
    assert s["count"] == 0
    assert s["profit_factor"] is None
    assert s["sharpe"] == 0.0


def test_stats_basic():
    # +2, +4, -3, +1, -2  → toplam 2
    s = compute_stats([2.0, 4.0, -3.0, 1.0, -2.0])
    assert s["count"] == 5
    assert s["wins"] == 3 and s["losses"] == 2
    assert s["win_rate"] == pytest.approx(60.0)
    assert s["total_pnl"] == pytest.approx(2.0)
    assert s["avg_win"] == pytest.approx((2 + 4 + 1) / 3)
    assert s["avg_loss"] == pytest.approx((3 + 2) / 2)
    # PF = 7 / 5
    assert s["profit_factor"] == pytest.approx(7.0 / 5.0)
    assert s["expectancy"] == pytest.approx(0.4)


def test_stats_max_drawdown():
    # kümülatif: 5, 3, 8, 2 → tepe 8, dip 2 → DD = 6
    s = compute_stats([5.0, -2.0, 5.0, -6.0])
    assert s["max_drawdown"] == pytest.approx(6.0)


def test_stats_no_losses_profit_factor_none():
    s = compute_stats([1.0, 2.0, 3.0])
    assert s["profit_factor"] is None
    assert s["win_rate"] == pytest.approx(100.0)


# ── run_backtest ─────────────────────────────────────────────────────────
def test_backtest_take_profit_path():
    # Sinyal: dar spread + makul ask → YES @ 0.45 aç.
    # Sonra bid 0.60'a fırlar → YES current=bid → +%33 > TP(%20) → kapat (kâr).
    series = {
        "m1": [
            {"bid": 0.44, "ask": 0.45, "spread": 0.01},   # sinyal → aç
            {"bid": 0.60, "ask": 0.61, "spread": 0.01},   # TP → kapat
        ]
    }
    res = run_backtest(series, amount=10.0)
    assert len(res["pnls"]) == 1
    assert res["pnls"][0] > 0
    assert res["trades"][0]["reason"] == "take_profit"
    assert res["stats"]["wins"] == 1


def test_backtest_stop_loss_path():
    series = {
        "m1": [
            {"bid": 0.44, "ask": 0.45, "spread": 0.01},   # aç @ 0.45
            {"bid": 0.36, "ask": 0.37, "spread": 0.01},   # -%20 → SL → kapat
        ]
    }
    res = run_backtest(series, amount=10.0)
    assert len(res["pnls"]) == 1
    assert res["pnls"][0] < 0
    assert res["trades"][0]["reason"] == "stop_loss"


def test_backtest_no_signal_no_trade():
    series = {"m1": [{"bid": 0.2, "ask": 0.5, "spread": 0.30}]}  # geniş spread
    res = run_backtest(series)
    assert res["pnls"] == []
    assert res["stats"]["count"] == 0
