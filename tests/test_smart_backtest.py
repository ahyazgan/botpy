"""Akıllı-çıkış backtest: simulate_smart (breakeven/kısmi TP/trailing/time-stop)."""

from __future__ import annotations

import pytest

import news_backtest as nbt


def _k(o, hi, lo, c):
    return [0, str(o), str(hi), str(lo), str(c), "0"]


def _sig(candles, direction="bullish"):
    return {"symbol": "FOOUSDT", "direction": direction, "time": 0,
            "impact": 9, "title": "t", "source": "s", "candles": candles}


P = {"sl_pct": 3.0, "tp_pct": 6.0, "breakeven_pct": 0.0, "partial_tp_pct": 0.0,
     "partial_tp_frac": 0.5, "trailing_stop_pct": 0.0, "time_stop_min": 0}


def test_smart_tp_hit():
    sig = _sig([_k(100, 100, 100, 100), _k(100, 107, 99, 105)])
    r = nbt.simulate_smart(sig, P, fee_pct=0.0)
    assert r["outcome"] == "tp" and r["net_pct"] == pytest.approx(6.0)


def test_smart_sl_hit():
    sig = _sig([_k(100, 100, 100, 100), _k(100, 101, 96, 97)])
    r = nbt.simulate_smart(sig, P, fee_pct=0.0)
    assert r["outcome"] == "sl" and r["net_pct"] == pytest.approx(-3.0)


def test_smart_timeout_exits_at_last_close():
    sig = _sig([_k(100, 100, 100, 100), _k(100, 102, 99, 101), _k(101, 102, 100, 102)])
    r = nbt.simulate_smart(sig, P, fee_pct=0.0)
    assert r["outcome"] == "timeout" and r["net_pct"] == pytest.approx(2.0)


def test_smart_time_stop():
    p = {**P, "tp_pct": 20.0, "time_stop_min": 1}
    sig = _sig([_k(100, 100, 100, 100), _k(100, 103, 100, 102), _k(102, 105, 101, 104)])
    r = nbt.simulate_smart(sig, p, fee_pct=0.0)
    # idx=1 >= time_stop_min=1 → o mumun kapanışında (102) çık = +%2
    assert r["outcome"] == "time-stop" and r["net_pct"] == pytest.approx(2.0)


def test_smart_breakeven_protects():
    # +%2'de breakeven → SL girişe; sonra geri düşünce ~0 (zarar yerine)
    p = {**P, "tp_pct": 20.0, "breakeven_pct": 2.0}
    sig = _sig([_k(100, 100, 100, 100), _k(100, 103, 101, 102), _k(102, 102, 99, 99)])
    r = nbt.simulate_smart(sig, p, fee_pct=0.0)
    assert r["outcome"] == "be-stop" and r["net_pct"] == pytest.approx(0.0)


def test_smart_partial_then_trailing():
    # +%3'te yarısını al (+1.25 kilit), trailing SL'i +%1.5'e çek, geri düşünce kalan +0.75
    p = {**P, "tp_pct": 20.0, "partial_tp_pct": 2.5, "partial_tp_frac": 0.5, "trailing_stop_pct": 1.5}
    sig = _sig([_k(100, 100, 100, 100), _k(100, 103, 100, 103), _k(103, 103, 101, 101)])
    r = nbt.simulate_smart(sig, p, fee_pct=0.0)
    assert r["partial"] is True
    assert r["net_pct"] == pytest.approx(2.0)   # 0.5*2.5 + 0.5*1.5


def test_smart_short_tp():
    sig = _sig([_k(100, 100, 100, 100), _k(100, 101, 93, 94)], direction="bearish")
    r = nbt.simulate_smart(sig, P, fee_pct=0.0)
    assert r["outcome"] == "tp" and r["net_pct"] == pytest.approx(6.0)


def test_smart_fee_applied_with_partial():
    p = {**P, "tp_pct": 20.0, "partial_tp_pct": 2.5, "partial_tp_frac": 0.5, "trailing_stop_pct": 1.5}
    sig = _sig([_k(100, 100, 100, 100), _k(100, 103, 100, 103), _k(103, 103, 101, 101)])
    r = nbt.simulate_smart(sig, p, fee_pct=0.2)
    # gross 2.0 - (0.2 + 0.2/2*0.5) = 2.0 - 0.25 = 1.75
    assert r["net_pct"] == pytest.approx(1.75)


def test_simulate_smart_all_skips_short_series():
    good = _sig([_k(100, 100, 100, 100), _k(100, 107, 99, 105)])
    bad = _sig([_k(100, 100, 100, 100)])   # tek mum → atla
    out = nbt.simulate_smart_all([good, bad], P, fee=0.0)
    assert len(out) == 1 and out[0]["outcome"] == "tp"


# ── Canlı-gerçekçilik: slippage + gecikmeli giriş ──────────────────────────
def test_smart_slippage_reduces_net():
    sig = _sig([_k(100, 100, 100, 100), _k(100, 107, 99, 105)])
    p = {**P, "slip_pct": 0.1}
    r = nbt.simulate_smart(sig, p, fee_pct=0.0)
    # gross 6 - slip 0.1*2 bacak = 5.8
    assert r["net_pct"] == pytest.approx(5.8)


def test_smart_entry_delay_changes_entry_price():
    # gecikme: 1. mumdan değil 2. mumun açılışından (105) gir → TP/SL ona göre
    sig = _sig([_k(100, 100, 100, 100), _k(100, 100, 100, 105), _k(105, 112, 104, 110)])
    p = {**P, "entry_delay_min": 1}
    r = nbt.simulate_smart(sig, p, fee_pct=0.0)
    # entry=105, tp=105*1.06=111.3; 3. mum high=112 → tp +%6
    assert r["outcome"] == "tp" and r["net_pct"] == pytest.approx(6.0)


def test_smart_entry_delay_insufficient_candles():
    sig = _sig([_k(100, 100, 100, 100), _k(100, 107, 99, 105)])
    assert nbt.simulate_smart(sig, {**P, "entry_delay_min": 5}, fee_pct=0.0) is None


def test_simple_slippage_and_delay():
    sig = _sig([_k(100, 100, 100, 100), _k(100, 100, 100, 101), _k(101, 108, 100, 107)])
    # gecikme 1 → entry=101, tp=101*1.06≈107.06; 3.mum high=108 ≥ → tp; slip 0.1*2=0.2
    r = nbt.simulate(sig, 3.0, 6.0, 0.0, slip_pct=0.1, entry_delay_min=1)
    assert r["outcome"] == "tp" and r["net_pct"] == pytest.approx(5.8)
