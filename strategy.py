"""
Saf strateji mantığı — sinyal üretimi ve çıkış (TP/SL) kararları.

Ağ/durum/web bağımlılığı yoktur; hem canlı tarayıcı (bot.py) hem backtest
(backtest.py) bu tek kaynağı kullanır. Böylece simülasyon ile canlı davranış
birebir aynıdır.
"""

from __future__ import annotations

from typing import Any

# Otomatik strateji eşikleri — likit + dar spread + makul fiyat bandı
AUTO_MAX_SPREAD: float = 0.03     # bu spread üstündekileri atla
AUTO_MIN_PRICE: float = 0.10      # fiyat bandı alt sınırı (ask)
AUTO_MAX_PRICE: float = 0.90      # fiyat bandı üst sınırı (ask)

# Çıkış eşikleri
TAKE_PROFIT_PCT: float = 20.0     # +%20 kârda kapat
STOP_LOSS_PCT: float = 15.0       # -%15 zararda kapat


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def entry_price(market: dict[str, Any], side: str) -> float | None:
    """Verilen taraf için giriş fiyatı (YES=ask, NO=1-bid)."""
    if side == "YES":
        return to_float(market.get("ask"))
    bid = to_float(market.get("bid"))
    return (1.0 - bid) if bid is not None else None


def evaluate_signal(
    row: dict[str, Any],
    *,
    max_spread: float = AUTO_MAX_SPREAD,
    min_price: float = AUTO_MIN_PRICE,
    max_price: float = AUTO_MAX_PRICE,
) -> str | None:
    """Likidite-alıcı sinyal: dar spread + makul fiyat bandı → YES, yoksa None.

    (Hacim filtresi yukarı akışta uygulanır.)
    """
    ask = to_float(row.get("ask"))
    spread = to_float(row.get("spread"))
    if ask is None or spread is None:
        return None
    if 0.0 <= spread <= max_spread and min_price <= ask <= max_price:
        return "YES"
    return None


def should_close(
    entry: float,
    current: float | None,
    *,
    take_profit_pct: float = TAKE_PROFIT_PCT,
    stop_loss_pct: float = STOP_LOSS_PCT,
) -> str | None:
    """Açık pozisyon kapatılmalı mı? "take_profit" / "stop_loss" / None.

    PnL% = (current/entry - 1) * 100.
    """
    if current is None or entry <= 0:
        return None
    pnl_pct = (current / entry - 1.0) * 100.0
    if pnl_pct >= take_profit_pct:
        return "take_profit"
    if pnl_pct <= -stop_loss_pct:
        return "stop_loss"
    return None
