"""ATR-uyarlamalı trailing stop: sabit % yerine atr_trailing_mult × ATR%.

Oynak coinde (ATR yüksek) trailing geniş → trend tutulur; sakinde dar → erken kilitlenir.
ATR yoksa veya use_atr_trailing kapalıysa sabit trailing_pct'e düşülür (geriye uyum).
"""

from __future__ import annotations

import trader


def _pos(trailing_pct=2.0, atr_pct=None):
    return {"id": "p1", "symbol": "FOOUSDT", "side": "long", "trailing_pct": trailing_pct,
            "atr_pct": atr_pct}


# ── _effective_trailing_pct ──────────────────────────────────────────────────
def test_fixed_when_atr_trailing_off():
    trader.S.use_atr_trailing = False
    assert trader._effective_trailing_pct(_pos(trailing_pct=2.0, atr_pct=5.0)) == 2.0


def test_atr_scaled_when_on():
    trader.S.use_atr_trailing = True
    trader.S.atr_trailing_mult = 1.5
    # ATR %4 → trailing = 1.5 × 4 = 6%
    assert trader._effective_trailing_pct(_pos(atr_pct=4.0)) == 6.0
    trader.S.use_atr_trailing = False


def test_atr_clamped():
    trader.S.use_atr_trailing = True
    trader.S.atr_trailing_mult = 1.0
    assert trader._effective_trailing_pct(_pos(atr_pct=50.0)) == 10.0   # üst clamp
    assert trader._effective_trailing_pct(_pos(atr_pct=0.1)) == 0.3     # alt clamp
    trader.S.use_atr_trailing = False


def test_falls_back_to_fixed_without_atr():
    trader.S.use_atr_trailing = True
    trader.S.atr_trailing_mult = 1.0
    # ATR yok → sabit trailing_pct'e düş
    assert trader._effective_trailing_pct(_pos(trailing_pct=2.5, atr_pct=None)) == 2.5
    assert trader._effective_trailing_pct(_pos(trailing_pct=2.5, atr_pct=0)) == 2.5
    trader.S.use_atr_trailing = False


def test_volatile_wider_than_calm():
    trader.S.use_atr_trailing = True
    trader.S.atr_trailing_mult = 1.0
    volatile = trader._effective_trailing_pct(_pos(atr_pct=8.0))
    calm = trader._effective_trailing_pct(_pos(atr_pct=2.0))
    assert volatile > calm   # oynak coin daha geniş trailing
    trader.S.use_atr_trailing = False
