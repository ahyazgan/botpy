"""
Strateji backtest motoru.

bot.py paper stratejisinin (sinyal → aç, TP/SL → kapat) aynı saf
fonksiyonlarını geçmiş/sentetik fiyat serileri üzerinde koşturur.
Gerçek geçmiş veri kaydı yoksa sentetik senaryolarla strateji
parametreleri test edilebilir.

market_series biçimi:
    { market_id: [ {bid, ask, spread}, ... zaman sıralı ], ... }
"""

from __future__ import annotations

from typing import Any

from metrics import compute_stats
from strategy import (
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    entry_price,
    evaluate_signal,
    should_close,
)


def _current_price(snap: dict[str, Any], side: str) -> float | None:
    """Açık pozisyonun güncel fiyatı (YES→bid, NO→1-ask)."""
    if side == "YES":
        v = snap.get("bid")
        return float(v) if v is not None else None
    ask = snap.get("ask")
    return (1.0 - float(ask)) if ask is not None else None


def run_backtest(
    market_series: dict[str, list[dict[str, Any]]],
    *,
    amount: float = 10.0,
    take_profit_pct: float = TAKE_PROFIT_PCT,
    stop_loss_pct: float = STOP_LOSS_PCT,
) -> dict[str, Any]:
    """Her market için en fazla bir açık pozisyon; sinyal→aç, TP/SL→kapat.

    Dönen: {"pnls": [...], "trades": [...], "stats": {...}}.
    """
    pnls: list[float] = []
    trades: list[dict[str, Any]] = []

    for mid, snaps in market_series.items():
        open_pos: tuple[str, float, float] | None = None  # (side, entry, shares)
        for snap in snaps:
            if open_pos is None:
                side = evaluate_signal(snap)
                if side is None:
                    continue
                entry = entry_price(snap, side)
                if entry is None or entry <= 0:
                    continue
                open_pos = (side, entry, amount / entry)
            else:
                side, entry, shares = open_pos
                current = _current_price(snap, side)
                reason = should_close(
                    entry, current,
                    take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                )
                if reason is not None and current is not None:
                    pnl = shares * current - amount
                    pnls.append(pnl)
                    trades.append({
                        "market_id": mid, "side": side, "entry": entry,
                        "close": current, "pnl": pnl, "reason": reason,
                    })
                    open_pos = None

    return {"pnls": pnls, "trades": trades, "stats": compute_stats(pnls)}


def _demo() -> None:
    """Sentetik veriyle hızlı gösterim: python backtest.py"""
    import json

    series = {
        "kazanan": [
            {"bid": 0.44, "ask": 0.45, "spread": 0.01},
            {"bid": 0.60, "ask": 0.61, "spread": 0.01},
        ],
        "kaybeden": [
            {"bid": 0.44, "ask": 0.45, "spread": 0.01},
            {"bid": 0.36, "ask": 0.37, "spread": 0.01},
        ],
    }
    res = run_backtest(series, amount=10.0)
    print("Backtest demo (sentetik):")
    print(json.dumps(res["stats"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _demo()
