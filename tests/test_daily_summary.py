"""Günlük özet (daily_summary) + gün dönümü digest'i."""

from __future__ import annotations

import pytest

import news_bot as nb
import trader


@pytest.fixture()
def clean(monkeypatch):
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader, "_closed", [])
    yield


def _c(pnl, date="2026-06-14"):
    return {"pnl": pnl, "closed_at": f"{date}T12:00:00+00:00", "symbol": "X"}


def test_daily_summary_filters_by_date(clean, monkeypatch):
    monkeypatch.setattr(trader, "_closed",
                        [_c(5.0), _c(-2.0), _c(3.0, date="2026-06-13")])
    s = trader.daily_summary("2026-06-14")
    assert s["trades"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert s["realized"] == 3.0 and s["best"] == 5.0 and s["worst"] == -2.0


def test_daily_summary_empty(clean):
    s = trader.daily_summary("2026-06-14")
    assert s["trades"] == 0 and s["realized"] == 0.0


def test_fmt_summary_msg():
    s = {"date": "2026-06-14", "trades": 3, "wins": 2, "losses": 1,
         "realized": 7.5, "best": 6.0, "worst": -2.0,
         "open_positions": 1, "open_exposure_usdt": 100.0}
    msg = nb._fmt_summary_msg(s)
    assert "2026-06-14" in msg and "+7.5 USDT" in msg
    assert "2K/1Z" in msg and "100.0 USDT maruziyet" in msg


# ── Gün dönümü digest'i ────────────────────────────────────────────────────
def test_digest_fires_on_day_change(monkeypatch):
    sent = []
    monkeypatch.setattr(nb, "notify_remote", lambda m: sent.append(m))
    monkeypatch.setattr(trader, "daily_summary", lambda d=None: {
        "date": d, "trades": 2, "wins": 1, "losses": 1, "realized": 1.0,
        "best": 3.0, "worst": -2.0, "open_positions": 0, "open_exposure_usdt": 0.0})

    monkeypatch.setattr(nb, "_last_summary_date", None)
    monkeypatch.setattr(trader, "_today", lambda: "2026-06-14")
    nb._maybe_daily_digest()                 # ilk tur → sadece tarihi kaydet
    assert sent == []
    nb._maybe_daily_digest()                 # aynı gün → tetikleme yok
    assert sent == []
    monkeypatch.setattr(trader, "_today", lambda: "2026-06-15")
    nb._maybe_daily_digest()                 # gün değişti → dünün özeti
    assert len(sent) == 1 and "2026-06-14" in sent[0]


def test_digest_skips_empty_day(monkeypatch):
    sent = []
    monkeypatch.setattr(nb, "notify_remote", lambda m: sent.append(m))
    monkeypatch.setattr(trader, "daily_summary", lambda d=None: {"date": d, "trades": 0,
        "wins": 0, "losses": 0, "realized": 0.0, "best": 0.0, "worst": 0.0,
        "open_positions": 0, "open_exposure_usdt": 0.0})
    monkeypatch.setattr(nb, "_last_summary_date", "2026-06-14")
    monkeypatch.setattr(trader, "_today", lambda: "2026-06-15")
    nb._maybe_daily_digest()
    assert sent == []                        # işlemsiz gün → özet yok
