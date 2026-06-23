"""Bot iç-watchdog: Watchdog heartbeat/staleness + _check_watchdog geçişi + halt."""

from __future__ import annotations

import pytest

import news_bot as nb
import trader
from watchdog import Watchdog


# ── Watchdog (saf) ───────────────────────────────────────────────────────
def test_fresh_after_register():
    w = Watchdog()
    w.register("monitor", 60, now=1000)
    assert w.health(now=1000)["monitor"]["stale"] is False
    assert w.stale(now=1000) == []


def test_goes_stale_past_threshold():
    w = Watchdog()
    w.register("monitor", 60, now=1000)
    assert w.health(now=1055)["monitor"]["stale"] is False   # 55s < 60s
    assert w.health(now=1075)["monitor"]["stale"] is True     # 75s > 60s
    assert w.stale(now=1075) == ["monitor"]


def test_beat_refreshes():
    w = Watchdog()
    w.register("monitor", 60, now=1000)
    w.beat("monitor", now=1100)         # 1100'de canlılık
    assert w.health(now=1150)["monitor"]["stale"] is False   # 50s < 60s
    assert w.health(now=1170)["monitor"]["stale"] is True     # 70s > 60s


def test_age_reported():
    w = Watchdog()
    w.register("scan", 120, now=1000)
    assert w.health(now=1042)["scan"]["age_sec"] == pytest.approx(42.0)


def test_independent_loops():
    w = Watchdog()
    w.register("monitor", 60, now=1000)
    w.register("scan", 120, now=1000)
    w.beat("scan", now=1100)
    h = w.health(now=1130)
    assert h["monitor"]["stale"] is True    # 130s > 60s
    assert h["scan"]["stale"] is False       # 30s < 120s


# ── _check_watchdog: geçiş + halt ────────────────────────────────────────
@pytest.fixture()
def wd_env(monkeypatch):
    w = Watchdog()
    monkeypatch.setattr(nb, "_watchdog", w)
    monkeypatch.setattr(nb, "_watchdog_stale_prev", set())
    monkeypatch.setattr(nb, "get_store", lambda: _FakeStore())
    monkeypatch.setattr(nb, "notify_remote", lambda m: _notes.append(m))
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setattr(trader, "_halt", {"active": False, "reason": "", "since": ""})
    monkeypatch.setitem(nb._metrics, "loop_stalls_total", 0)
    _notes.clear()
    return w


_notes: list[str] = []


class _FakeStore:
    def add_ops_event(self, *a, **k):
        return 1
    def prune_ops_events(self, *a, **k):
        return 0


def test_monitor_stall_alerts_and_halts(wd_env, monkeypatch):
    wd_env.register("monitor", 60, now=1000)
    monkeypatch.setattr(wd_env, "stale", lambda now=None: ["monitor"])
    nb._check_watchdog()
    assert nb._metrics["loop_stalls_total"] == 1
    assert any("WATCHDOG" in m and "TAKILDI" in m for m in _notes)
    # halt_on_monitor_stall vars. açık → devre kesici tetiklenir
    assert trader.get_halt()["active"] is True


def test_monitor_stall_no_halt_when_disabled(wd_env, monkeypatch):
    trader.S.halt_on_monitor_stall = False
    monkeypatch.setattr(wd_env, "stale", lambda now=None: ["monitor"])
    nb._check_watchdog()
    assert trader.get_halt()["active"] is False


def test_transition_fires_once(wd_env, monkeypatch):
    monkeypatch.setattr(wd_env, "stale", lambda now=None: ["monitor"])
    nb._check_watchdog()
    nb._check_watchdog()    # hâlâ takılı → YENİ uyarı yok
    assert nb._metrics["loop_stalls_total"] == 1


def test_recovery_transition(wd_env, monkeypatch):
    monkeypatch.setattr(wd_env, "stale", lambda now=None: ["monitor"])
    nb._check_watchdog()
    _notes.clear()
    monkeypatch.setattr(wd_env, "stale", lambda now=None: [])   # toparlandı
    nb._check_watchdog()
    assert any("yeniden çalışıyor" in m for m in _notes)


def test_scan_stall_no_halt(wd_env, monkeypatch):
    # scan döngüsü takılması halt tetiklemez (yalnız monitor)
    monkeypatch.setattr(wd_env, "stale", lambda now=None: ["scan"])
    nb._check_watchdog()
    assert trader.get_halt()["active"] is False
    assert nb._metrics["loop_stalls_total"] == 1
