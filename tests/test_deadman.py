"""Ölü-adam anahtarı: WS akışı durunca uzak kanaldan uyar, düzelince haber ver."""

from __future__ import annotations

import pytest

import news_bot as nb


@pytest.fixture()
def env(monkeypatch):
    notes: list[str] = []
    monkeypatch.setattr(nb, "notify_remote", lambda m: notes.append(m))
    monkeypatch.setattr(nb, "USE_TREENEWS", True)
    monkeypatch.setattr(nb, "WS_STALE_ALERT_SEC", 600.0)
    monkeypatch.setattr(nb, "_started_at", 0.0)        # grace dışı (now büyük)
    monkeypatch.setattr(nb, "_ws_alert_active", False)
    monkeypatch.setattr(nb, "_ws_state", {"connected": True, "last_msg_at": None})
    return notes


def test_healthy_feed_no_alert(env):
    nb._ws_state.update(connected=True, last_msg_at=10_000.0)
    nb._maybe_deadman_alert(now=10_010.0)   # 10s yaş
    assert env == [] and nb._ws_feed_stale(now=10_010.0) is False


def test_disconnected_alerts_once(env):
    nb._ws_state.update(connected=False, last_msg_at=10_000.0)
    nb._maybe_deadman_alert(now=10_000.0)
    nb._maybe_deadman_alert(now=10_001.0)   # ikinci çağrı tekrar uyarmaz
    assert len(env) == 1 and "HABER AKIŞI DURDU" in env[0]


def test_stale_by_age_alerts(env):
    nb._ws_state.update(connected=True, last_msg_at=10_000.0)
    nb._maybe_deadman_alert(now=10_700.0)   # 700s > 600 eşik
    assert len(env) == 1 and "dk önce" in env[0]


def test_recovery_notice(env):
    nb._ws_state.update(connected=False, last_msg_at=10_000.0)
    nb._maybe_deadman_alert(now=10_000.0)             # uyarı
    nb._ws_state.update(connected=True, last_msg_at=20_000.0)
    nb._maybe_deadman_alert(now=20_001.0)             # toparlama
    assert len(env) == 2 and "geri geldi" in env[1]


def test_grace_period_suppresses(env):
    monkeypatch_started = 9_900.0
    nb._started_at = monkeypatch_started
    nb._ws_state.update(connected=False, last_msg_at=None)
    nb._maybe_deadman_alert(now=10_000.0)   # uptime 100s < 600 grace
    assert env == []


def test_disabled_when_no_treenews(env, monkeypatch):
    monkeypatch.setattr(nb, "USE_TREENEWS", False)
    nb._ws_state.update(connected=False, last_msg_at=None)
    nb._maybe_deadman_alert(now=10_000.0)
    assert env == [] and nb._ws_feed_stale(now=10_000.0) is False
