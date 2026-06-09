"""
Strateji parametre optimizasyonu — geçmiş veride grid-search.

run_backtest'i parametre kombinasyonları üzerinde koşturup bir hedef
metriğe göre sıralar. Aşırı-uydurmayı (overfitting) azaltmak için
min_trades filtresi vardır.

Uyarı: optimizasyon geçmişe uydurur; bulunan parametreler ileriye dönük
garanti vermez. Walk-forward / out-of-sample doğrulama önerilir.
"""

from __future__ import annotations

from itertools import product
from typing import Any

from backtest import run_backtest


def grid_search(
    market_series: dict[str, list[dict[str, Any]]],
    grid: dict[str, list[float]],
    *,
    amount: float = 10.0,
    objective: str = "total_pnl",
    min_trades: int = 1,
    top: int = 10,
) -> list[dict[str, Any]]:
    """grid: {param_adı: [değerler]} (run_backtest kwargs ile eşleşir).

    Her kombinasyon için backtest koşar; `objective` metriğine göre azalan
    sıralar. `min_trades` altındaki sonuçlar (gürültü) elenir.
    Dönen: en iyi `top` sonuç — [{params, score, stats}, ...].
    """
    if not grid:
        return []
    keys = list(grid.keys())
    results: list[dict[str, Any]] = []

    for combo in product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        res = run_backtest(market_series, amount=amount, **params)
        stats = res["stats"]
        if stats["count"] < min_trades:
            continue
        score = stats.get(objective)
        score = float(score) if isinstance(score, (int, float)) else 0.0
        results.append({"params": params, "score": score, "stats": stats})

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[: max(1, top)]


# Makul varsayılan ızgara (TP/SL taraması)
DEFAULT_GRID: dict[str, list[float]] = {
    "take_profit_pct": [10.0, 15.0, 20.0, 25.0, 30.0],
    "stop_loss_pct": [10.0, 15.0, 20.0],
}
