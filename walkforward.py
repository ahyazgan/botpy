"""
Walk-forward doğrulama — backtest'in dürüst versiyonu.

Parametreleri verinin ilk kısmında (in-sample) optimize eder, sonra
*görmediği* son kısımda (out-of-sample) test eder. In-sample harika ama
out-of-sample kötüyse: strateji geçmişe uydurulmuş (overfit), gerçek
edge yok demektir.

Saf modül (ağ yok); optimize.grid_search + backtest.run_backtest kullanır.
"""

from __future__ import annotations

from typing import Any

from backtest import run_backtest
from optimize import grid_search


def split_series(
    series: dict[str, list[dict[str, Any]]], train_frac: float = 0.7,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Her marketin kronolojik serisini train/test olarak böl (zaman sıralı)."""
    train: dict[str, list[dict[str, Any]]] = {}
    test: dict[str, list[dict[str, Any]]] = {}
    for mid, snaps in series.items():
        n = len(snaps)
        cut = int(n * train_frac)
        if cut > 0:
            train[mid] = snaps[:cut]
        if cut < n:
            test[mid] = snaps[cut:]
    return train, test


def _verdict(is_exp: float, oos_exp: float, oos_count: int) -> tuple[str, float | None]:
    """In/out-sample beklentiyi karşılaştır → karar + zayıflama oranı."""
    if oos_count == 0:
        return "out-of-sample'da işlem yok — sonuç yok", None
    degradation = ((is_exp - oos_exp) / abs(is_exp)) if is_exp else None
    if oos_exp <= 0:
        return "OOS negatif/sıfır — overfit ya da edge yok", degradation
    if is_exp > 0 and oos_exp >= 0.5 * is_exp:
        return "tutarlı — umut verici (yine de canlı doğrula)", degradation
    return "OOS pozitif ama belirgin zayıflama var", degradation


def walk_forward(
    series: dict[str, list[dict[str, Any]]],
    grid: dict[str, list[float]],
    *,
    train_frac: float = 0.7,
    amount: float = 10.0,
    objective: str = "total_pnl",
    min_trades: int = 1,
) -> dict[str, Any]:
    """In-sample'da optimize, out-of-sample'da test et.

    Dönen: params, in_sample stats, out_of_sample stats, verdict, degradation.
    """
    train, test = split_series(series, train_frac)
    ranked = grid_search(
        train, grid, amount=amount, objective=objective,
        min_trades=min_trades, top=1,
    )
    if not ranked:
        return {
            "ok": False,
            "reason": "in-sample'da yeterli işlem yok",
            "params": None,
        }

    best = ranked[0]
    params = best["params"]
    is_stats = best["stats"]
    oos = run_backtest(test, amount=amount, **params)
    oos_stats = oos["stats"]

    verdict, degradation = _verdict(
        is_stats["expectancy"], oos_stats["expectancy"], oos_stats["count"],
    )
    return {
        "ok": True,
        "params": params,
        "objective": objective,
        "in_sample": is_stats,
        "out_of_sample": oos_stats,
        "degradation": degradation,
        "verdict": verdict,
    }
