"""Geçmiş snapshot kaydı + gerçek-veri replay backtest testleri."""

from __future__ import annotations

import pytest

import bot
from backtest import run_backtest
from storage import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "hist.db"))
    yield s
    s.close()


def _row(mid, bid, ask, spread):
    return {"id": mid, "question": f"Q-{mid}", "bid": bid, "ask": ask,
            "spread": spread, "volume24h": 50_000.0}


def test_record_and_count(store):
    n = store.record_snapshots("2026-01-01T00:00:00+00:00",
                               [_row("m1", 0.44, 0.45, 0.01), _row("m2", 0.3, 0.5, 0.2)])
    assert n == 2
    assert store.count_snapshots() == 2


def test_history_series_chronological(store):
    store.record_snapshots("2026-01-01T00:00:01+00:00", [_row("m1", 0.44, 0.45, 0.01)])
    store.record_snapshots("2026-01-01T00:00:02+00:00", [_row("m1", 0.60, 0.61, 0.01)])
    series = store.history_series()
    assert list(series.keys()) == ["m1"]
    # kronolojik: önce 0.45, sonra 0.61
    assert [s["ask"] for s in series["m1"]] == pytest.approx([0.45, 0.61])


def test_history_series_limit_keeps_recent(store):
    for i in range(5):
        store.record_snapshots(f"2026-01-01T00:00:0{i}+00:00",
                               [_row("m1", 0.4 + i / 100, 0.41 + i / 100, 0.01)])
    series = store.history_series(limit_per_market=2)
    assert len(series["m1"]) == 2  # en yeni 2


def test_prune_snapshots(store):
    for i in range(10):
        store.record_snapshots(f"2026-01-01T00:00:0{i}+00:00", [_row("m1", 0.4, 0.41, 0.01)])
    deleted = store.prune_snapshots(keep=3)
    assert deleted == 7
    assert store.count_snapshots() == 3


def test_replay_backtest_from_recorded_history(store):
    # Kayıtlı gerçek seri: sinyal → TP yolu
    store.record_snapshots("2026-01-01T00:00:01+00:00", [_row("m1", 0.44, 0.45, 0.01)])
    store.record_snapshots("2026-01-01T00:00:02+00:00", [_row("m1", 0.60, 0.61, 0.01)])
    series = store.history_series()
    res = run_backtest(series, amount=10.0)
    assert len(res["pnls"]) == 1
    assert res["pnls"][0] > 0
    assert res["stats"]["wins"] == 1


def test_record_history_flag_off_by_default(tmp_path):
    state = bot.AppState(store=Store(str(tmp_path / "s.db")))
    assert state.record_history is False


def test_snapshot_span(store):
    store.record_snapshots("2026-01-01T00:00:01+00:00",
                           [_row("m1", 0.4, 0.41, 0.01), _row("m2", 0.5, 0.51, 0.01)])
    store.record_snapshots("2026-01-03T00:00:01+00:00", [_row("m1", 0.4, 0.41, 0.01)])
    span = store.snapshot_span()
    assert span["count"] == 3
    assert span["markets"] == 2
    assert span["first_ts"] == "2026-01-01T00:00:01+00:00"
    assert span["last_ts"] == "2026-01-03T00:00:01+00:00"


def test_settings_persist(store):
    assert store.get_setting("record_history") is None
    store.set_setting("record_history", "1")
    assert store.get_setting("record_history") == "1"
    store.set_setting("record_history", "0")          # upsert
    assert store.get_setting("record_history") == "0"


def test_record_history_persists_across_restart(tmp_path):
    path = str(tmp_path / "persist.db")
    s1 = bot.AppState(store=Store(path))
    assert s1.record_history is False
    s1.store.set_setting("record_history", "1")        # dashboard toggle benzeri
    # "restart": aynı DB ile yeni AppState
    s2 = bot.AppState(store=Store(path))
    assert s2.record_history is True


def test_record_history_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORD_HISTORY", "1")
    state = bot.AppState(store=Store(str(tmp_path / "env.db")))
    assert state.record_history is True
