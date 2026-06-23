"""Fiyat verisi doğrulama (_stats_sane) + API güvenlik probu (çekim izni)."""

from __future__ import annotations

import math

import pytest

import news_bot as nb
import trader


# ── _stats_sane ──────────────────────────────────────────────────────────
def _stats(**kw):
    base = {"pct24": 1.0, "move15": 0.5, "move60": 0.3, "atr_pct": 1.0, "rvol": 1.2, "vol": 5e6}
    base.update(kw)
    return base


def test_clean_stats_pass():
    assert nb._stats_sane(_stats()) is None


def test_nan_rejected():
    assert nb._stats_sane(_stats(move15=math.nan)) is not None


def test_inf_rejected():
    assert nb._stats_sane(_stats(pct24=math.inf)) is not None


def test_negative_volume_rejected():
    r = nb._stats_sane(_stats(vol=-1.0))
    assert r is not None and "hacim" in r


def test_absurd_move_rejected():
    r = nb._stats_sane(_stats(pct24=900.0))   # > %500 → imkânsız/bozuk
    assert r is not None and "imkânsız" in r


def test_large_but_plausible_move_ok():
    # %80 hareket gerçek olabilir (küçük cap) → reddedilmez
    assert nb._stats_sane(_stats(pct24=80.0, move15=40.0)) is None


def test_fetch_symbol_stats_rejects_anomaly(monkeypatch):
    # _fetch_symbol_stats bozuk veride None döner + sayaç artar
    monkeypatch.setitem(nb._metrics, "price_anomaly_total", 0)

    def fake_get_json(url, **kw):
        if "ticker/24hr" in url:
            return {"priceChangePercent": "9999", "quoteVolume": "1000000"}  # imkânsız %
        return [[0, "100", "101", "99", "100", "10"]]
    monkeypatch.setattr(nb, "get_json", fake_get_json)
    out = nb._fetch_symbol_stats(object(), "FOOUSDT")  # type: ignore[arg-type]
    assert out is None
    assert nb._metrics["price_anomaly_total"] == 1


# ── connectivity_probe: API güvenlik (çekim izni) ────────────────────────
class _Ex:
    def __init__(self, withdraw=False, ip=True):
        self._w = withdraw
        self._ip = ip

    def fetch_time(self):
        import time
        return int(time.time() * 1000)

    def fetch_balance(self):
        return {"free": {"USDT": 100.0}}

    def sapiGetAccountApiRestrictions(self):
        return {"enableWithdrawals": self._w, "ipRestrict": self._ip}


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(trader, "S", trader.Settings())
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_SECRET", "s")
    yield


def test_probe_withdrawal_open_is_critical(monkeypatch):
    monkeypatch.setattr(trader, "_get_exchange", lambda: _Ex(withdraw=True))
    pr = trader.connectivity_probe()
    assert pr["ok"] is False
    w = next(c for c in pr["checks"] if c["check"] == "Çekim izni (API)")
    assert w["status"] == "critical"


def test_probe_withdrawal_closed_ok(monkeypatch):
    monkeypatch.setattr(trader, "_get_exchange", lambda: _Ex(withdraw=False, ip=True))
    pr = trader.connectivity_probe()
    w = next(c for c in pr["checks"] if c["check"] == "Çekim izni (API)")
    assert w["status"] == "ok"
    ipc = next(c for c in pr["checks"] if c["check"] == "IP kısıtlaması (API)")
    assert ipc["status"] == "ok"


def test_probe_no_ip_restrict_warns(monkeypatch):
    monkeypatch.setattr(trader, "_get_exchange", lambda: _Ex(withdraw=False, ip=False))
    pr = trader.connectivity_probe()
    ipc = next(c for c in pr["checks"] if c["check"] == "IP kısıtlaması (API)")
    assert ipc["status"] == "warn"
