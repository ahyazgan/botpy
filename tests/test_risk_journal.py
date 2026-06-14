"""Risk özeti + işlem günlüğü (closed_trades) + CSV dışa aktarım testleri."""

from __future__ import annotations

import csv
import io

import pytest

import news_bot as nb
import trader


@pytest.fixture()
def clean_trader(monkeypatch):
    """trader iç durumunu izole et (gerçek pozisyon/işlem sızmasın)."""
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": 0.0})
    yield


def _pos(symbol="FOOUSDT", usdt=100.0):
    return {"symbol": symbol, "usdt": usdt, "side": "long"}


def _closed(symbol="FOOUSDT", pnl=5.0):
    return {
        "closed_at": "2026-06-14T12:00:00+00:00", "opened_at": "2026-06-14T11:00:00+00:00",
        "symbol": symbol, "side": "long", "mode": "paper", "usdt": 100.0,
        "entry_price": 1.0, "close_price": 1.05, "pnl": pnl, "pnl_pct": 5.0,
        "close_reason": "take-profit", "source": "auto",
    }


# ── get_risk ───────────────────────────────────────────────────────────────
def test_get_risk_exposure(clean_trader, monkeypatch):
    monkeypatch.setattr(trader, "_positions", [_pos("FOOUSDT", 100), _pos("FOOUSDT", 50), _pos("BARUSDT", 30)])
    r = trader.get_risk()
    assert r["open_positions"] == 3
    assert r["total_exposure_usdt"] == 180.0
    assert r["per_coin_exposure"]["FOOUSDT"] == 150.0
    assert r["trading_halted"] is False


def test_get_risk_kill_switch(clean_trader, monkeypatch):
    trader.S.daily_loss_limit_usdt = 200.0
    monkeypatch.setattr(trader, "_daily", {"date": trader._today(), "realized": -250.0})
    assert trader.get_risk()["trading_halted"] is True


def test_risk_endpoint(clean_trader):
    out = nb.risk()
    assert "total_exposure_usdt" in out and "trading_halted" in out


# ── closed_trades + CSV ────────────────────────────────────────────────────
def test_closed_trades_newest_first(clean_trader, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_closed("A", 1), _closed("B", 2), _closed("C", 3)])
    rows = trader.closed_trades()
    assert [r["symbol"] for r in rows] == ["C", "B", "A"]


def test_trades_closed_endpoint(clean_trader, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_closed("A", 1)])
    out = nb.trades_closed()
    assert len(out["trades"]) == 1 and out["trades"][0]["symbol"] == "A"


def test_csv_export(clean_trader, monkeypatch):
    monkeypatch.setattr(trader, "_closed", [_closed("A", 1.5), _closed("B", -2.0)])
    resp = nb.trades_closed_csv()
    assert resp.media_type == "text/csv"
    text = resp.body.decode()
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 2
    assert {r["symbol"] for r in rows} == {"A", "B"}
    assert rows[0]["symbol"] == "B"   # en yeni başta (closed_trades ters sıralar)
    by_sym = {r["symbol"]: r["pnl"] for r in rows}
    assert by_sym["A"] == "1.5"
    assert "attachment" in resp.headers["content-disposition"]


def test_csv_empty(clean_trader):
    resp = nb.trades_closed_csv()
    rows = list(csv.DictReader(io.StringIO(resp.body.decode())))
    assert rows == []
