"""Çok-kademeli scale-out: tek-kademe partial_tp → çok-kademe (her eşik ayrı tetiklenir).

Geriye uyum: partial_tp_levels boşsa eski partial_tp_pct/frac tek-kademe davranışı.
Her eşik bir kez tetiklenir (partial_levels_done izler); en düşük eşik önce.
"""

from __future__ import annotations

import pytest

import trader


# ── _parse_tp_levels ─────────────────────────────────────────────────────────
def test_parse_levels_basic():
    assert trader._parse_tp_levels("3:0.33,6:0.33,10:0.34") == [(3.0, 0.33), (6.0, 0.33), (10.0, 0.34)]


def test_parse_levels_sorts_ascending():
    assert trader._parse_tp_levels("10:0.5,3:0.3") == [(3.0, 0.3), (10.0, 0.5)]


def test_parse_levels_skips_invalid():
    # bozuk parçalar atlanır; frac>1 veya pct<=0 elenir
    assert trader._parse_tp_levels("3:0.5,bad,6:2.0,0:0.3,10:0.4") == [(3.0, 0.5), (10.0, 0.4)]


def test_parse_levels_empty():
    assert trader._parse_tp_levels("") == []
    assert trader._parse_tp_levels("   ") == []


# ── _tp_levels: çok-kademe önceliği + geriye uyum ────────────────────────────
def test_tp_levels_prefers_multi():
    trader.S.partial_tp_levels = "3:0.33,6:0.33"
    trader.S.partial_tp_pct = 5.0
    trader.S.partial_tp_frac = 0.5
    assert trader._tp_levels() == [(3.0, 0.33), (6.0, 0.33)]
    trader.S.partial_tp_levels = ""


def test_tp_levels_falls_back_to_single():
    trader.S.partial_tp_levels = ""
    trader.S.partial_tp_pct = 5.0
    trader.S.partial_tp_frac = 0.5
    assert trader._tp_levels() == [(5.0, 0.5)]


def test_tp_levels_none_when_disabled():
    trader.S.partial_tp_levels = ""
    trader.S.partial_tp_pct = 0.0
    assert trader._tp_levels() == []


# ── Entegrasyon: monitor_positions çok-kademe ────────────────────────────────
@pytest.fixture()
def pos_env(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    trader.S.paper_trading = True
    trader.S.partial_tp_levels = "3:0.5,6:0.5"
    trader.S.partial_tp_pct = 0.0
    trader.S.breakeven_pct = 0.0
    trader.S.trailing_stop_pct = 0.0
    trader.S.time_stop_min = 0
    closed_recs = []

    def fake_partial(p, frac, reason, cur):
        amt = round(p["amount"] * frac, 8)
        p["amount"] = round(p["amount"] - amt, 8)
        rec = {"id": p["id"], "close_reason": reason, "frac": frac}
        closed_recs.append(rec)
        return rec

    monkeypatch.setattr(trader, "_partial_close", fake_partial)
    yield closed_recs
    trader.S.partial_tp_levels = ""


def _pos(entry=100.0):
    return {"id": "p1", "symbol": "FOOUSDT", "side": "long", "entry_price": entry,
            "amount": 1.0, "usdt": 100.0, "mode": "paper", "market": "spot",
            "opened_at": trader._now(), "sl_price": None, "tp_price": None}


def test_first_level_triggers_at_threshold(pos_env, monkeypatch):
    pos = _pos()
    monkeypatch.setattr(trader, "_positions", [pos])
    monkeypatch.setattr(trader, "get_prices", lambda syms: {"FOOUSDT": 103.5})  # +3.5% gain
    trader.monitor_positions()
    # sadece 3% kademesi tetiklenir (6% henüz değil)
    assert len(pos_env) == 1
    assert "partial-tp-3%" in pos_env[0]["close_reason"]
    assert 3.0 in pos["partial_levels_done"]
    assert 6.0 not in pos["partial_levels_done"]


def test_both_levels_trigger_when_gain_high(pos_env, monkeypatch):
    pos = _pos()
    monkeypatch.setattr(trader, "_positions", [pos])
    monkeypatch.setattr(trader, "get_prices", lambda syms: {"FOOUSDT": 107.0})  # +7% → her iki kademe
    trader.monitor_positions()
    assert len(pos_env) == 2
    assert pos["partial_levels_done"] == [3.0, 6.0]


def test_level_not_retriggered(pos_env, monkeypatch):
    pos = _pos()
    pos["partial_levels_done"] = [3.0]   # 3% zaten yapıldı
    monkeypatch.setattr(trader, "_positions", [pos])
    monkeypatch.setattr(trader, "get_prices", lambda syms: {"FOOUSDT": 103.5})
    trader.monitor_positions()
    assert len(pos_env) == 0   # 3% tekrar tetiklenmez, 6% henüz değil
