"""Likidasyon-baskısı sinyali: funding + premium aşırılığı → squeeze setup (futures, auth'suz).

Aşırı-kalabalık yön zorla kapanmaya yatkın → ters yönde patlama. funding negatif&aşırı =
short-squeeze (long lehine); pozitif&aşırı = long-squeeze (short lehine). Orderbook/premium teyit.
"""

from __future__ import annotations

import trader


def _patch_pi(monkeypatch, funding, premium):
    monkeypatch.setattr(trader, "_premium_index",
                        lambda sym: {"funding_pct": funding, "premium_pct": premium})


# ── liquidation_pressure: squeeze yönü ───────────────────────────────────────
def test_short_squeeze_supports_long(monkeypatch):
    _patch_pi(monkeypatch, funding=-0.05, premium=-0.05)  # shortlar aşırı kalabalık
    out = trader.liquidation_pressure("BTCUSDT", "long")
    assert out["squeeze"] == "short"
    assert out["supports_side"] == "long"
    assert out["score"] > 0
    assert out["aligned"] is True   # long girişi short-squeeze ile uyumlu


def test_long_squeeze_supports_short(monkeypatch):
    _patch_pi(monkeypatch, funding=0.06, premium=0.05)  # longlar aşırı kalabalık
    out = trader.liquidation_pressure("BTCUSDT", "short")
    assert out["squeeze"] == "long"
    assert out["supports_side"] == "short"
    assert out["aligned"] is True


def test_no_squeeze_when_funding_normal(monkeypatch):
    _patch_pi(monkeypatch, funding=0.01, premium=0.02)  # normal
    out = trader.liquidation_pressure("BTCUSDT", "long")
    assert out["squeeze"] is None
    assert out["supports_side"] is None
    assert out["score"] == 0.0


def test_misaligned_side(monkeypatch):
    _patch_pi(monkeypatch, funding=-0.05, premium=0.0)  # short-squeeze (long lehine)
    out = trader.liquidation_pressure("BTCUSDT", "short")  # ama biz short giriyoruz
    assert out["supports_side"] == "long"
    assert out["aligned"] is False   # short girişi short-squeeze ile UYUMSUZ


def test_score_grows_with_extremity(monkeypatch):
    _patch_pi(monkeypatch, funding=-0.03, premium=0.0)
    mild = trader.liquidation_pressure("BTCUSDT", "long")["score"]
    _patch_pi(monkeypatch, funding=-0.09, premium=0.0)
    extreme = trader.liquidation_pressure("BTCUSDT", "long")["score"]
    assert extreme > mild   # daha aşırı funding → daha yüksek skor


def test_premium_confirmation_boosts(monkeypatch):
    # short-squeeze (long lehine): negatif premium teyit eder → skor artar
    _patch_pi(monkeypatch, funding=-0.04, premium=0.0)
    no_conf = trader.liquidation_pressure("BTCUSDT", "long")["score"]
    _patch_pi(monkeypatch, funding=-0.04, premium=-0.15)
    with_conf = trader.liquidation_pressure("BTCUSDT", "long")["score"]
    assert with_conf > no_conf


def test_orderbook_confirmation_boosts(monkeypatch):
    _patch_pi(monkeypatch, funding=-0.04, premium=0.0)
    base = trader.liquidation_pressure("BTCUSDT", "long", book=None)["score"]
    # alıcı baskın orderbook (skew>0.2) yukarı patlamayı teyit → skor artar
    boosted = trader.liquidation_pressure("BTCUSDT", "long", book={"skew": 0.5})["score"]
    assert boosted > base


def test_returns_none_without_data(monkeypatch):
    monkeypatch.setattr(trader, "_premium_index", lambda sym: None)
    assert trader.liquidation_pressure("BTCUSDT", "long") is None


# ── _premium_index ───────────────────────────────────────────────────────────
def test_premium_index_parse(monkeypatch):
    monkeypatch.setattr(trader, "get_json", lambda *a, **k: {
        "markPrice": "102.0", "indexPrice": "100.0", "lastFundingRate": "0.0001"})
    pi = trader._premium_index("BTCUSDT")
    assert pi["premium_pct"] == 2.0           # (102-100)/100 × 100
    assert pi["funding_pct"] == 0.01          # 0.0001 × 100


def test_premium_index_none_on_empty(monkeypatch):
    monkeypatch.setattr(trader, "get_json", lambda *a, **k: None)
    assert trader._premium_index("BTCUSDT") is None
