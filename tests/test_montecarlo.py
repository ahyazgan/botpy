"""Monte Carlo risk simülasyonu: dağılım + iflas riski + determinizm."""

from __future__ import annotations

import pytest

from montecarlo import _run_path, monte_carlo


# ── _run_path ────────────────────────────────────────────────────────────
def test_path_final_and_drawdown():
    # base 1000; +100, -300, +50 → eq: 1100, 800, 850; peak 1100; dd = 300/1100 = %27.3
    final, dd, ruined = _run_path([100, -300, 50], base=1000, ruin_level=0)
    assert final == pytest.approx(-150)
    assert dd == pytest.approx(27.27, abs=0.01)
    assert ruined is False


def test_path_ruin_detected():
    # base 1000, ruin_level 500; -600 → eq 400 <= 500 → iflas
    _, _, ruined = _run_path([-600], base=1000, ruin_level=500)
    assert ruined is True


# ── monte_carlo ──────────────────────────────────────────────────────────
def test_empty_returns_not_ok():
    out = monte_carlo([])
    assert out["ok"] is False


def test_deterministic_with_seed():
    pnls = [10, -5, 8, -3, 12, -7, 6, -4, 9, -2]
    a = monte_carlo(pnls, runs=500, seed=42)
    b = monte_carlo(pnls, runs=500, seed=42)
    assert a["final_pnl"] == b["final_pnl"]
    assert a["risk_of_ruin"] == b["risk_of_ruin"]


def test_all_winners_no_ruin():
    out = monte_carlo([10] * 30, runs=500, account_equity=1000, seed=1)
    assert out["risk_of_ruin"] == 0.0
    assert out["prob_profit"] == 100.0
    assert out["final_pnl"]["p50"] > 0


def test_all_losers_high_ruin():
    # Sürekli zarar → sermaye hızla erir → iflas riski yüksek
    out = monte_carlo([-100] * 30, runs=500, account_equity=1000, ruin_pct=50, seed=1)
    assert out["risk_of_ruin"] == 100.0
    assert out["prob_profit"] == 0.0


def test_reliable_flag():
    assert monte_carlo([5, -3] * 3, runs=100, seed=1)["reliable"] is False   # 6 < 20
    assert monte_carlo([5, -3] * 15, runs=100, seed=1)["reliable"] is True   # 30 >= 20


def test_distribution_ordering():
    # p5 <= p50 <= p95 (dağılım sıralı)
    out = monte_carlo([20, -15, 10, -8, 12, -10] * 5, runs=1000, seed=7)
    f = out["final_pnl"]
    assert f["p5"] <= f["p50"] <= f["p95"]
    dd = out["max_drawdown_pct"]
    assert dd["p50"] <= dd["p95"] <= dd["worst"]


def test_runs_clamped():
    out = monte_carlo([1, -1] * 10, runs=10_000_000, seed=1)
    assert out["runs"] <= 100_000
