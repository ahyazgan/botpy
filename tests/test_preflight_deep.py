"""Preflight derinliği: canlı bağlantı probu + canlıya-geçiş guard-rail + /golive kokpiti."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
import trader


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setattr(trader, "_halt", {"active": False, "reason": "", "since": ""})
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SECRET", raising=False)
    yield


# ── connectivity_probe ───────────────────────────────────────────────────
def test_probe_skipped_without_keys():
    pr = trader.connectivity_probe()
    assert pr["skipped"] is True


class _FakeEx:
    def __init__(self, skew_ms=0, free=100.0, auth_fail=False):
        self._skew = skew_ms
        self._free = free
        self._auth_fail = auth_fail

    def fetch_time(self):
        import time
        return int(time.time() * 1000) + self._skew

    def fetch_balance(self):
        if self._auth_fail:
            raise RuntimeError("Invalid API-key")
        return {"free": {"USDT": self._free}}


def _with_keys(monkeypatch, ex):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_SECRET", "s")
    monkeypatch.setattr(trader, "_get_exchange", lambda: ex)


def test_probe_all_ok(monkeypatch):
    _with_keys(monkeypatch, _FakeEx(skew_ms=200, free=50.0))
    pr = trader.connectivity_probe()
    assert pr["ok"] is True
    names = {c["check"]: c["status"] for c in pr["checks"]}
    assert names["Saat kayması"] == "ok"
    assert names["Kimlik doğrulama"] == "ok"


def test_probe_clock_skew_critical(monkeypatch):
    _with_keys(monkeypatch, _FakeEx(skew_ms=9000))
    pr = trader.connectivity_probe()
    assert pr["ok"] is False
    skew = next(c for c in pr["checks"] if c["check"] == "Saat kayması")
    assert skew["status"] == "critical"


def test_probe_auth_failure_critical(monkeypatch):
    _with_keys(monkeypatch, _FakeEx(auth_fail=True))
    pr = trader.connectivity_probe()
    assert pr["ok"] is False
    auth = next(c for c in pr["checks"] if c["check"] == "Kimlik doğrulama")
    assert auth["status"] == "critical"


# ── Guard-rail: canlıya geçiş kritik varken bloklanır ────────────────────
def test_golive_flip_blocked_when_preflight_critical(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_SECRET", "s")
    trader.S.use_sl_tp = False   # → preflight kritik (korumasız)
    with pytest.raises(RuntimeError, match="Canlıya geçiş engellendi"):
        trader.update_settings({"auto_trade": True, "paper_trading": False})
    # Rollback: kısmi commit olmamalı
    assert trader.S.auto_trade is False
    assert trader.S.paper_trading is True


def test_golive_flip_allowed_when_clean(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_SECRET", "s")
    # Varsayılan güvenli S (SL açık, limitler dolu, native-stop açık) → kritik yok
    out = trader.update_settings({"auto_trade": True, "paper_trading": False})
    assert out["auto_trade"] is True and out["paper_trading"] is False


def test_editing_while_live_not_blocked(monkeypatch):
    # Zaten canlı+oto iken başka alan düzenlemek kilitlenmemeli (enabling değil)
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_SECRET", "s")
    trader.S.paper_trading = False
    trader.S.auto_trade = True
    trader.S.use_sl_tp = False   # kritik olsa bile: bu patch live'ı ETKİNLEŞTİRMİYOR
    out = trader.update_settings({"trade_usdt": 50.0})
    assert out["trade_usdt"] == 50.0


# ── /golive kokpiti ──────────────────────────────────────────────────────
def test_golive_blocks_on_ops_critical(monkeypatch):
    trader.S.use_sl_tp = False
    monkeypatch.setattr(nb, "_ws_feed_stale", lambda *a, **k: False)
    d = TestClient(nb.app).get("/golive").json()
    assert "CANLIYA GEÇME" in d["verdict"]
    assert len(d["blockers"]) >= 1
