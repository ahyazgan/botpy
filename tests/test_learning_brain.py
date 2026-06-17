"""Öğrenen Beyin v2 — çok-boyutlu öğrenme + RVOL + kapalı döngü oto-uygula.

Kapanan işlemlerden yön/coin/saat/RVOL dilimi beklentisi çıkarır; min_rel_volume
ve time_stop önerir; auto_tune açıkken korkuluklarla oto-uygular (para-büyüklüğü
ayarlarına asla dokunmaz).
"""

from __future__ import annotations

import trader


def _trade(pnl, *, impact=8, side="long", symbol="AUSDT", source="X",
           rel_volume=None, hold_min=60.0):
    open_at = "2026-06-17T10:00:00+00:00"
    close_at = f"2026-06-17T{10 + int(hold_min // 60):02d}:{int(hold_min % 60):02d}:00+00:00"
    return {"pnl": pnl, "impact": impact, "side": side, "symbol": symbol,
            "news_source": source, "rel_volume": rel_volume,
            "opened_at": open_at, "closed_at": close_at}


# ── RVOL band + hold yardımcıları ───────────────────────────────────────────
def test_rvol_band():
    assert trader._rvol_band(0.5) == "<1.0"
    assert trader._rvol_band(1.2) == "1.0-1.5"
    assert trader._rvol_band(2.0) == "1.5-3"
    assert trader._rvol_band(4.0) == ">=3"
    assert trader._rvol_band(None) is None


def test_hold_minutes():
    c = {"opened_at": "2026-06-17T10:00:00+00:00", "closed_at": "2026-06-17T10:45:00+00:00"}
    assert trader._hold_minutes(c) == 45.0
    assert trader._hold_minutes({"opened_at": None, "closed_at": None}) is None


# ── Çok-boyutlu öğrenme ─────────────────────────────────────────────────────
def test_suggest_has_all_dimensions():
    trades = [_trade(1.0, rel_volume=2.0) for _ in range(10)]
    out = trader._suggest_from_trades(trades, value_key="pnl", source_key="news_source",
                                      tier_of=None, unit=" USDT")
    for dim in ("by_impact", "by_direction", "by_coin", "by_hour", "by_rvol", "by_source"):
        assert dim in out


def test_rvol_band_expectancy_split():
    # Düşük hacim kaybettirir, yüksek kazandırır → by_rvol bunu ayırmalı
    trades = ([_trade(-2.0, rel_volume=0.8) for _ in range(6)]
              + [_trade(5.0, rel_volume=2.5) for _ in range(6)])
    out = trader._suggest_from_trades(trades, value_key="pnl", source_key="news_source",
                                      tier_of=None, unit=" USDT")
    assert out["by_rvol"]["<1.0"]["avg_pnl"] == -2.0
    assert out["by_rvol"]["1.5-3"]["avg_pnl"] == 5.0


def test_min_rel_volume_suggestion(monkeypatch):
    monkeypatch.setattr(trader.S, "min_rel_volume", 0.0)
    trades = ([_trade(-2.0, rel_volume=0.8) for _ in range(6)]
              + [_trade(5.0, rel_volume=2.5) for _ in range(6)])
    out = trader._suggest_from_trades(trades, value_key="pnl", source_key="news_source",
                                      tier_of=None, unit=" USDT")
    rv = [s for s in out["suggestions"] if s["type"] == "min_rel_volume"]
    assert rv and rv[0]["suggested"] >= 1.0


def test_time_stop_suggestion():
    # Kaybedenler uzun (120dk), kazananlar kısa (30dk) tutuluyor → süre-stop öner
    trades = ([_trade(-2.0, hold_min=120.0) for _ in range(6)]
              + [_trade(5.0, hold_min=30.0) for _ in range(6)])
    out = trader._suggest_from_trades(trades, value_key="pnl", source_key="news_source",
                                      tier_of=None, unit=" USDT")
    ts = [s for s in out["suggestions"] if s["type"] == "time_stop"]
    assert ts and 0 < ts[0]["suggested"] < 120


# ── Korkuluklar (apply_tuning) ──────────────────────────────────────────────
def test_apply_min_rel_volume_clamped(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(trader.S, "min_rel_volume", 0.0)
    sug = {"ready": True, "samples": 12, "suggestions": [
        {"type": "min_rel_volume", "suggested": 99.0}]}    # absürt → [0,5] kıstır
    res = trader.apply_tuning(sug)
    assert res["applied"] and trader.S.min_rel_volume == 5.0


def test_apply_time_stop_clamped(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(trader.S, "time_stop_min", 0)
    sug = {"ready": True, "samples": 12, "suggestions": [
        {"type": "time_stop", "suggested": 9999}]}         # absürt → [0,720] kıstır
    res = trader.apply_tuning(sug)
    assert res["applied"] and trader.S.time_stop_min == 720


def test_apply_does_not_touch_money_settings(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    before = (trader.S.trade_usdt, trader.S.max_total_exposure_usdt, trader.S.leverage)
    sug = {"ready": True, "samples": 12, "suggestions": [
        {"type": "auto_min_impact", "suggested": 9},
        {"type": "min_rel_volume", "suggested": 1.5}]}
    trader.apply_tuning(sug)
    assert (trader.S.trade_usdt, trader.S.max_total_exposure_usdt, trader.S.leverage) == before


# ── Kapalı döngü (auto_apply_tuning) ────────────────────────────────────────
def test_auto_apply_noop_when_off(monkeypatch):
    monkeypatch.setattr(trader.S, "auto_tune", False)
    res = trader.auto_apply_tuning()
    assert res["applied"] is False and "kapalı" in res["reason"]


def test_auto_apply_runs_when_on(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(trader.S, "auto_tune", True)
    monkeypatch.setattr(trader.S, "auto_min_impact", 8)
    trades = [_trade(5.0, impact=9, rel_volume=2.5) for _ in range(12)]
    monkeypatch.setattr(trader, "_closed", trades)
    res = trader.auto_apply_tuning()
    assert "changes" in res                              # açık → suggest+apply zinciri koştu
