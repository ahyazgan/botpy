"""bot.py saf yardımcı fonksiyon testleri."""

from __future__ import annotations

import pytest

import bot


def test_to_float_valid():
    assert bot.to_float("1.5") == pytest.approx(1.5)
    assert bot.to_float(2) == pytest.approx(2.0)


def test_to_float_invalid():
    assert bot.to_float(None) is None
    assert bot.to_float("abc") is None
    assert bot.to_float([]) is None


def test_build_rows_filters_and_sorts():
    raw = [
        {"id": "a", "question": "A", "volume24hr": bot.MIN_VOLUME_24HR - 1},  # elenir
        {"id": "b", "question": "B", "volume24hr": 20_000, "bestBid": 0.4, "bestAsk": 0.45},
        {"id": "c", "question": "C", "volume24hr": 50_000, "bestBid": 0.1, "bestAsk": 0.2},
    ]
    rows = bot.build_rows(raw)

    # Düşük hacimli "a" elendi
    assert [r["id"] for r in rows] == ["c", "b"]  # hacme göre azalan sıra
    # spread = ask - bid
    assert rows[0]["spread"] == pytest.approx(0.1)
    assert rows[1]["spread"] == pytest.approx(0.05)


def test_build_rows_spread_fallback_when_no_bid_ask():
    raw = [{"id": "x", "question": "X", "volume24hr": 99_999, "spread": 0.07}]
    rows = bot.build_rows(raw)
    assert rows[0]["bid"] is None and rows[0]["ask"] is None
    assert rows[0]["spread"] == pytest.approx(0.07)
