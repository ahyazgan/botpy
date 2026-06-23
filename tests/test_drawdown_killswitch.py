"""Drawdown kill-switch: _drawdown_state + _check_risk gate + get_risk + preflight."""

from __future__ import annotations

import pytest

import trader


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    yield


def _closed(*pnls):
    return [{"pnl": p, "closed_at": f"2026-01-0{i}"} for i, p in enumerate(pnls, 1)]


# ── _drawdown_state ──────────────────────────────────────────────────────
def test_no_drawdown_when_only_gains():
    dd = trader._drawdown_state(_closed(100, 50), account_base=10000)
    assert dd["drawdown_pct"] == 0.0
    assert dd["equity"] == 10150.0
    assert dd["peak"] == 10150.0


def test_drawdown_from_peak():
    # +500 (peak 10500) sonra -800 → equity 9700, dd = 800/10500 = %7.62
    dd = trader._drawdown_state(_closed(500, -800), account_base=10000)
    assert dd["peak"] == 10500.0
    assert dd["equity"] == 9700.0
    assert dd["drawdown_pct"] == pytest.approx(7.62, abs=0.01)


def test_empty_is_zero():
    dd = trader._drawdown_state([], account_base=10000)
    assert dd["drawdown_pct"] == 0.0
    assert dd["equity"] == 10000.0


# ── _check_risk gate ─────────────────────────────────────────────────────
def test_check_risk_blocks_when_drawdown_exceeds():
    trader.S.max_drawdown_pct = 5.0
    trader.S.account_equity_usdt = 10000
    trader._closed = _closed(500, -1100)   # dd = 1100/10500 ≈ %10.5 > %5
    with pytest.raises(RuntimeError, match="Drawdown kill-switch"):
        trader._check_risk("BTCUSDT", 100)


def test_check_risk_allows_under_limit():
    trader.S.max_drawdown_pct = 20.0
    trader._closed = _closed(500, -1100)   # ≈%10.5 < %20
    trader._check_risk("BTCUSDT", 100)     # raise YOK


def test_disabled_when_zero():
    trader.S.max_drawdown_pct = 0.0        # kapalı
    trader._closed = _closed(-9000)        # büyük düşüş ama kapalı
    trader._check_risk("BTCUSDT", 100)     # raise YOK


# ── get_risk surfacing ───────────────────────────────────────────────────
def test_get_risk_exposes_drawdown():
    trader.S.max_drawdown_pct = 5.0
    trader._closed = _closed(500, -1100)
    r = trader.get_risk()
    assert "drawdown" in r
    assert r["drawdown"]["halted"] is True
    assert r["drawdown"]["max_drawdown_pct"] == 5.0


def test_get_risk_not_halted_under_limit():
    trader.S.max_drawdown_pct = 50.0
    trader._closed = _closed(-100)
    assert trader.get_risk()["drawdown"]["halted"] is False


# ── preflight ────────────────────────────────────────────────────────────
def test_preflight_warns_when_off():
    trader.S.max_drawdown_pct = 0.0
    chk = next(c for c in trader.preflight() if c["check"] == "Drawdown kill-switch")
    assert chk["status"] == "warn"


def test_preflight_ok_when_set():
    trader.S.max_drawdown_pct = 15.0
    chk = next(c for c in trader.preflight() if c["check"] == "Drawdown kill-switch")
    assert chk["status"] == "ok"
