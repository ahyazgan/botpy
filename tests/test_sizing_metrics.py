"""Conviction sizing + profesyonel risk metrikleri (drawdown, profit factor)."""

from __future__ import annotations

import pytest

import trader


# ── Conviction sizing ──────────────────────────────────────────────────────
def test_size_multiplier_curve():
    assert trader._size_multiplier(8) == 1.0          # taban
    assert trader._size_multiplier(10) == 1.5         # üst sınır
    assert trader._size_multiplier(9) == 1.25
    assert trader._size_multiplier(7) == 0.75
    assert trader._size_multiplier(2) == 0.5          # alt sınır (clamp)
    assert trader._size_multiplier(20) == 1.5         # üst clamp


class _Item:
    def __init__(self, impact, direction="bullish"):
        self.impact = impact
        self.direction = direction
        self.symbol = "FOOUSDT"
        self.confirmed = True
        self.reason = ""


@pytest.fixture()
def auto_env(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    trader.S.auto_trade = True
    trader.S.paper_trading = True
    trader.S.auto_min_impact = 7
    trader.S.auto_require_confirm = True
    trader.S.market = "spot"
    trader.S.trade_usdt = 100.0
    captured = {}

    def fake_place(symbol, side, usdt=None, source="manual", reason="", news_source="",
                   impact=None, atr_pct=None, sl_mult=1.0, time_stop_min=None, **kwargs):
        captured["usdt"] = usdt
        captured["side"] = side
        captured["impact"] = impact
        return {"id": "x", "symbol": symbol, "side": side, "usdt": usdt, "mode": "paper"}

    monkeypatch.setattr(trader, "place_trade", fake_place)
    monkeypatch.setattr(trader, "_can_auto_trade", lambda s: True)
    trader.S.reduce_after_losses = 0
    trader.S.suppress_losing_sources = False
    trader.S.skip_already_priced_pct = 0.0
    yield captured
    trader.S.auto_trade = False
    trader.S.size_by_impact = False


def test_auto_trade_fixed_size_by_default(auto_env):
    trader.S.size_by_impact = False
    trader.maybe_auto_trade(_Item(impact=10))
    assert auto_env["usdt"] == 100.0         # taban trade_usdt (artık explicit)


def test_auto_trade_conviction_size(auto_env):
    trader.S.size_by_impact = True
    trader.maybe_auto_trade(_Item(impact=10))
    assert auto_env["usdt"] == 150.0         # 100 * 1.5
    trader.maybe_auto_trade(_Item(impact=8))
    assert auto_env["usdt"] == 100.0         # 100 * 1.0


# ── Drawdown + profit factor ───────────────────────────────────────────────
def _eq(*cumulatives):
    return [{"cumulative": c} for c in cumulatives]


def test_max_drawdown():
    # 0 → 10 → 4 (tepe 10, dip 4) → 12: en büyük düşüş -6
    assert trader._max_drawdown(_eq(10, 4, 12)) == -6.0
    assert trader._max_drawdown(_eq(5, 10, 15)) == 0.0     # hep yükseliş
    assert trader._max_drawdown([]) == 0.0


def test_profit_factor():
    scored = [{"pnl": 10.0}, {"pnl": 5.0}, {"pnl": -3.0}, {"pnl": -2.0}]
    assert trader._profit_factor(scored) == 3.0            # 15 / 5
    assert trader._profit_factor([{"pnl": 5.0}]) is None   # zarar yok → tanımsız
    assert trader._profit_factor([]) is None


def test_get_performance_has_metrics(monkeypatch):
    monkeypatch.setattr(trader, "_closed", [
        {"pnl": 10.0, "closed_at": "t1"}, {"pnl": -6.0, "closed_at": "t2"},
    ])
    perf = trader.get_performance()
    assert "max_drawdown" in perf and "profit_factor" in perf
    assert perf["max_drawdown"] == -6.0
    assert perf["profit_factor"] == round(10 / 6, 2)
    # gelişmiş oranlar
    assert perf["avg_win"] == 10.0 and perf["avg_loss"] == -6.0
    assert perf["payoff_ratio"] == round(10 / 6, 2)
    assert perf["sharpe"] is not None


# ── Gelişmiş performans oranları ───────────────────────────────────────────
def test_perf_ratios_payoff_and_sharpe():
    scored = [{"pnl": 8.0}, {"pnl": 12.0}, {"pnl": -4.0}, {"pnl": -6.0}]
    r = trader._perf_ratios(scored)
    assert r["avg_win"] == 10.0 and r["avg_loss"] == -5.0
    assert r["payoff_ratio"] == 2.0          # 10 / |−5|
    assert r["sharpe"] is not None


def test_perf_ratios_edge_cases():
    # sadece kazanç → payoff None (kayıp yok), tek işlem → sharpe None
    only_win = trader._perf_ratios([{"pnl": 5.0}])
    assert only_win["avg_loss"] is None and only_win["payoff_ratio"] is None
    assert only_win["sharpe"] is None
    assert trader._perf_ratios([]) == {"avg_win": None, "avg_loss": None, "payoff_ratio": None, "sharpe": None}


def test_by_impact_attribution(monkeypatch):
    monkeypatch.setattr(trader, "_closed", [
        {"pnl": 10.0, "impact": 9, "closed_at": "t1"},
        {"pnl": -4.0, "impact": 9, "closed_at": "t2"},
        {"pnl": 6.0, "impact": 7, "closed_at": "t3"},
    ])
    bi = trader.get_performance()["by_impact"]
    assert bi["9"]["count"] == 2 and bi["9"]["pnl"] == 6.0 and bi["9"]["wins"] == 1
    assert bi["7"]["count"] == 1 and bi["7"]["pnl"] == 6.0
