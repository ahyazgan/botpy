"""risk.RiskManager testleri + bot.py risk entegrasyonu."""

from __future__ import annotations

import pytest

import bot
from risk import RiskLimits, RiskManager
from storage import Store

D = "2026-01-01"


def _mgr(**lim):
    return RiskManager(RiskLimits(**lim), starting_equity=1000.0)


# ── check_open limitleri ─────────────────────────────────────────────────
def test_check_open_allows_within_limits():
    m = _mgr()
    assert m.check_open(50.0, open_positions=0, total_exposure=0.0, today=D).allowed


def test_check_open_blocks_position_size():
    m = _mgr(max_position_usdc=10.0)
    d = m.check_open(50.0, 0, 0.0, today=D)
    assert d.allowed is False and "pozisyon boyutu" in d.reason


def test_check_open_blocks_total_exposure():
    m = _mgr(max_total_exposure_usdc=100.0)
    assert m.check_open(50.0, 1, total_exposure=60.0, today=D).allowed is False


def test_check_open_blocks_max_positions():
    m = _mgr(max_open_positions=2)
    assert m.check_open(10.0, open_positions=2, total_exposure=0.0, today=D).allowed is False


# ── kill-switch ──────────────────────────────────────────────────────────
def test_daily_loss_halts():
    m = _mgr(max_daily_loss_usdc=30.0)
    m.on_close(-20.0, D)
    assert m.halted is False
    m.on_close(-15.0, D)            # toplam -35 < -30 → halt
    assert m.halted is True
    assert m.check_open(10.0, 0, 0.0).allowed is False


def test_daily_loss_resets_next_day():
    m = _mgr(max_daily_loss_usdc=30.0)
    m.on_close(-25.0, D)
    assert m.day_realized == pytest.approx(-25.0)
    m.on_close(-10.0, "2026-01-02")   # yeni gün → gün içi sıfırlanır
    assert m.day_realized == pytest.approx(-10.0)
    assert m.halted is False


def test_drawdown_halts():
    m = _mgr(max_drawdown_pct=20.0, max_daily_loss_usdc=10_000.0)
    m.on_close(200.0, D)            # equity 1200, peak 1200
    m.on_close(-260.0, D)           # equity 940 → dd = (1200-940)/1200 = 21.7% → halt
    assert m.halted is True
    assert m.drawdown_pct > 20.0


def test_reset_halt():
    m = _mgr(max_daily_loss_usdc=10.0)
    m.on_close(-20.0, D)
    assert m.halted is True
    m.reset_halt()
    assert m.halted is False
    assert m.check_open(10.0, 0, 0.0).allowed is True


def test_position_size_fixed_fractional():
    m = _mgr(max_position_usdc=50.0)
    # 1000 * 0.02 = 20
    assert m.position_size(0.02) == pytest.approx(20.0)
    # tavanla sınırlı
    assert m.position_size(0.10) == pytest.approx(50.0)


# ── bot.py entegrasyonu ──────────────────────────────────────────────────
@pytest.fixture()
def state(tmp_path):
    return bot.AppState(store=Store(str(tmp_path / "risk.db")))


def _row(mid, ask=0.45, spread=0.01):
    return {"id": mid, "question": f"Q-{mid}", "bid": ask - 0.01, "ask": ask,
            "spread": spread, "volume24h": 50_000.0}


def test_auto_trade_respects_exposure_cap(state):
    state.risk.limits.max_total_exposure_usdc = 25.0  # 2x10=20 ok, 3.sü 30>25
    rows = [_row("m1"), _row("m2"), _row("m3")]
    opened = bot.auto_trade_step(state, rows, amount=10.0)
    assert opened == 2  # üçüncü exposure limitine takılır


def test_auto_trade_blocked_when_halted(state):
    state.risk._halt("test")
    opened = bot.auto_trade_step(state, [_row("m1")], amount=10.0)
    assert opened == 0


def test_close_updates_risk(state):
    state.update_snapshot(
        None, [{"id": "m1", "question": "Q", "bid": 0.55, "ask": 0.45,
                "spread": 0.10, "volume24h": 1.0}], 1)
    state.add_trade(bot.new_trade({"id": "m1", "question": "Q"}, "YES", 10.0, 0.45))
    before = state.risk.realized_pnl
    bot.auto_close_step(state)
    assert state.risk.realized_pnl > before  # TP kârı risk'e işlendi
