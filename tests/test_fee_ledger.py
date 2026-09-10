"""Komisyon-dürüst defter: gerçekleşen P&L NET tutulur (backtest ile aynı birim).

Regresyon zemini: defter brüt tutulursa profit-factor/readiness/Kelly/Monte Carlo
hepsi sistematik iyimser okur ve "canlıya geçeyim mi" kararı yanlış çıkar.
"""

from __future__ import annotations

import pytest

import trader


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(trader, "_save_state", lambda: None)
    monkeypatch.setattr(trader.S, "paper_trading", True)
    monkeypatch.setattr(trader.S, "taker_fee_pct", 0.1)
    trader._positions.clear()
    trader._closed.clear()
    trader._daily["realized"] = 0.0
    yield
    trader._positions.clear()
    trader._closed.clear()


def test_roundtrip_fee_notional_not_margin():
    """Komisyon MARJDAN değil NOMİNALDEN alınır — 10x'te 10 kat fark eder."""
    assert trader._roundtrip_fee(100.0, 1, 0.1) == 0.2      # 100 × %0.1 × 2 bacak
    assert trader._roundtrip_fee(100.0, 10, 0.1) == 2.0     # nominal 1000 → 10 katı
    assert trader._roundtrip_fee(100.0, 1, 0.0) == 0.0      # kapalı
    assert trader._roundtrip_fee(100.0, None, 0.1) == 0.2   # kaldıraç yoksa 1x


def test_net_of_fees_pct_is_margin_relative():
    pos = {"leverage": 1, "fee_pct": 0.1}
    net, pct, fees = trader._net_of_fees(pos, 10.0, 10.0, 100.0)
    assert fees == 0.2
    assert net == 9.8                 # brüt 10 − 0.2
    assert pct == 9.8                 # yüzde de marja oranlı düşer
    # Fiyat yoksa (pnl None) komisyon yine raporlanır ama P&L None kalır
    assert trader._net_of_fees(pos, None, None, 100.0)[0] is None


def test_close_position_records_net_and_gross(monkeypatch):
    monkeypatch.setattr(trader, "get_price", lambda s: 110.0)
    pos = {"id": "t1", "symbol": "BTCUSDT", "side": "long", "market": "spot",
           "mode": "paper", "usdt": 100.0, "entry_price": 100.0, "amount": 1.0,
           "leverage": 1, "fee_pct": 0.1, "opened_at": trader._now()}
    trader._positions.append(pos)
    out = trader.close_position("t1", reason="test")
    assert out["gross_pnl"] == 10.0            # brüt şeffaflık için saklanır
    assert out["fees_usdt"] == 0.2
    assert out["pnl"] == 9.8                   # defterdeki P&L NET
    assert trader._daily["realized"] == 9.8    # günlük zarar freni de net görür


def test_partial_closes_do_not_double_count_entry_fee(monkeypatch):
    """Her dilim yalnız KENDİ giriş+çıkış payını öder — toplam tam bir gidiş-dönüş."""
    monkeypatch.setattr(trader, "get_price", lambda s: 100.0)
    p = {"id": "t2", "symbol": "BTCUSDT", "side": "long", "market": "spot",
         "mode": "paper", "usdt": 100.0, "entry_price": 100.0, "amount": 1.0,
         "leverage": 1, "fee_pct": 0.1, "opened_at": trader._now()}
    trader._positions.append(p)
    trader._partial_close(p, 0.5, "kısmi", 100.0)     # yarısı
    trader.close_position("t2", reason="kalan")       # kalanı
    total_fees = sum(c["fees_usdt"] for c in trader._closed)
    assert total_fees == pytest.approx(0.2)           # 100 USDT'lik tam tur = 0.2


def test_fee_rate_sealed_at_open(monkeypatch):
    """Ayar sonradan değişse de açık pozisyonun muhasebesi kaymaz."""
    monkeypatch.setattr(trader, "get_price", lambda s: 100.0)
    pos = {"id": "t3", "symbol": "BTCUSDT", "side": "long", "market": "spot",
           "mode": "paper", "usdt": 100.0, "entry_price": 100.0, "amount": 1.0,
           "leverage": 1, "fee_pct": 0.1, "opened_at": trader._now()}
    trader._positions.append(pos)
    monkeypatch.setattr(trader.S, "taker_fee_pct", 0.9)   # açılıştan SONRA değişti
    assert trader.close_position("t3")["fees_usdt"] == 0.2  # mühürlü oran geçerli


def test_place_trade_seals_fee_and_preflight_flags_zero(monkeypatch):
    monkeypatch.setattr(trader, "get_price", lambda s: 100.0)
    monkeypatch.setattr(trader, "_estimate_fill", lambda *a, **k: None)
    pos = trader.place_trade("BTCUSDT", "long", usdt=50.0, source="test")
    assert pos["fee_pct"] == 0.1

    checks = {c["check"]: c for c in trader.preflight()}
    assert checks["İşlem maliyeti"]["status"] == "ok"
    monkeypatch.setattr(trader.S, "taker_fee_pct", 0.0)
    assert trader.preflight.__name__ and \
        {c["check"]: c for c in trader.preflight()}["İşlem maliyeti"]["status"] in ("warn", "critical")


def test_backtest_default_fee_matches_live_roundtrip():
    """Backtest uçlarının varsayılan komisyonu canlı gidiş-dönüş maliyetiyle aynı olmalı.

    FastAPI sorgu varsayılanları import anında sabitlenir (S'ten türetilemez), bu
    yüzden bağ testle korunur: biri değiştirilip diğeri unutulursa backtest ile
    canlı defter farklı birimde olur ve karşılaştırılamaz.
    """
    import inspect

    import news_bot as nb

    live_roundtrip = trader.S.taker_fee_pct * 2
    for fn in (nb.run_backtest, nb.ablation, nb.alpha):
        default = inspect.signature(fn).parameters["fee"].default
        assert default == pytest.approx(live_roundtrip), (
            f"{fn.__name__} fee varsayılanı {default}, canlı gidiş-dönüş {live_roundtrip}")
