"""Operasyonel sağlamlaştırma: devre kesici + emir dolum doğrulama (OrderError)."""

from __future__ import annotations

import pytest

import trader


@pytest.fixture(autouse=True)
def reset_halt(monkeypatch):
    monkeypatch.setattr(trader, "_order_fail_streak", 0)
    monkeypatch.setattr(trader, "_halts", 0)
    monkeypatch.setattr(trader, "_order_rejects", 0)
    monkeypatch.setattr(trader, "_halt", {"active": False, "reason": "", "since": ""})
    monkeypatch.setattr(trader.S, "auto_halt_on_anomaly", True)
    yield


# ── Devre kesici durum makinesi ───────────────────────────────────────────
def test_trip_and_clear_halt():
    assert trader.trip_halt("test") is True
    assert trader.get_halt()["active"] is True and trader._halts == 1
    assert trader.trip_halt("ikinci") is False     # zaten aktif → yeni tetikleme yok
    trader.clear_halt()
    assert trader.get_halt()["active"] is False and trader._order_fail_streak == 0


def test_halt_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(trader.S, "auto_halt_on_anomaly", False)
    assert trader.trip_halt("x") is False and trader.get_halt()["active"] is False


def test_order_fail_streak_trips_halt():
    assert trader._note_order_result(False) is False   # 1
    assert trader._note_order_result(False) is False   # 2
    assert trader._note_order_result(False) is True    # 3 → halt
    assert trader.get_halt()["active"] is True and trader._order_rejects == 3


def test_order_success_resets_streak():
    trader._note_order_result(False)
    trader._note_order_result(True)        # sıfırla
    assert trader._order_fail_streak == 0
    trader._note_order_result(False)
    trader._note_order_result(False)
    assert trader.get_halt()["active"] is False   # üst üste değil → halt yok


def test_auto_decision_blocked_when_halted():
    trader.trip_halt("anomali")

    class _Item:
        impact = 10
        direction = "bullish"
        confirmed = True
        symbol = "FOOUSDT"
        source = "x"
        reason = ""
        price_24h_pct = None
    d = trader.auto_decision(_Item())
    assert d["would_trade"] is False and "durdurma" in d["reason"]


# ── Emir dolum doğrulama (_verify_fill) ───────────────────────────────────
class Ex:
    def __init__(self, fetch=None):
        self._fetch = fetch
    def fetch_order(self, oid, sym):
        return self._fetch


def test_verify_fill_passes_on_filled():
    order = {"id": "o1", "filled": 5.0, "status": "closed"}
    assert trader._verify_fill(Ex(), order, "FOO/USDT")["filled"] == 5.0


def test_verify_fill_raises_on_zero_fill():
    order = {"id": "o1", "filled": 0, "status": "open"}
    ex = Ex(fetch={"id": "o1", "filled": 0, "status": "canceled"})
    with pytest.raises(trader.OrderError, match="dolmadı"):
        trader._verify_fill(ex, order, "FOO/USDT")


def test_verify_fill_uses_fetch_when_missing():
    order = {"id": "o1", "filled": 0, "status": None}
    ex = Ex(fetch={"id": "o1", "filled": 3.0, "status": "closed"})
    assert trader._verify_fill(ex, order, "FOO/USDT")["filled"] == 3.0


def test_verify_fill_uncertain_does_not_raise():
    """fetch_order başarısızsa (belirsiz) dolmuş varsayar, raise etmez (mutabakat yakalar)."""
    class Boom:
        def fetch_order(self, oid, sym): raise RuntimeError("ağ")
    order = {"id": "o1", "filled": 0, "status": None}
    out = trader._verify_fill(Boom(), order, "FOO/USDT")   # raise YOK
    assert out is order


def test_verify_fill_cancels_resting_order_on_reject():
    """Limit emir dinleniyor (open, dolmamış) → reddederken DURAN emri iptal et (ters-hayalet önle)."""
    cancelled = []

    class Ex2:
        def fetch_order(self, oid, sym):
            return {"id": oid, "filled": 0, "status": "open"}   # hâlâ duruyor
        def cancel_order(self, oid, sym):
            cancelled.append(oid)

    order = {"id": "o1", "filled": 0, "status": "open"}
    with pytest.raises(trader.OrderError):
        trader._verify_fill(Ex2(), order, "FOO/USDT")
    assert cancelled == ["o1"]   # duran emir iptal edildi


def test_verify_fill_no_cancel_when_already_terminal():
    """Zaten iptal/red ise tekrar iptal etmeye çalışma."""
    cancelled = []

    class Ex3:
        def cancel_order(self, oid, sym):
            cancelled.append(oid)

    order = {"id": "o1", "filled": 0, "status": "canceled"}
    with pytest.raises(trader.OrderError):
        trader._verify_fill(Ex3(), order, "FOO/USDT")
    assert cancelled == []   # terminal → iptal yok
