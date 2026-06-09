"""
Performans metrikleri — kapanan işlemlerin realize PnL listesinden.

Saf fonksiyonlar (ağ/durum yok). Profesyonel değerlendirme için:
win-rate, profit factor, expectancy, max drawdown, (işlem-başı) Sharpe.
"""

from __future__ import annotations

from typing import Any


def compute_stats(pnls: list[float]) -> dict[str, Any]:
    """PnL listesinden özet istatistikler.

    profit_factor: brüt kâr / brüt zarar (zarar yoksa None).
    sharpe: işlem-başı ortalama / standart sapma (yıllıklandırılmamış).
    max_drawdown: kümülatif equity'de tepe-dip en büyük düşüş (USDC).
    """
    n = len(pnls)
    if n == 0:
        return {
            "count": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "profit_factor": None, "expectancy": 0.0,
            "max_drawdown": 0.0, "sharpe": 0.0,
        }

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    gross_win = sum(wins)
    gross_loss = -sum(losses)

    # max drawdown (kümülatif equity)
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)

    # işlem-başı Sharpe
    mean = total / n
    var = sum((p - mean) ** 2 for p in pnls) / n
    std = var ** 0.5
    sharpe = (mean / std) if std > 0 else 0.0

    return {
        "count": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100.0,
        "total_pnl": total,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy": mean,
        "max_drawdown": mdd,
        "sharpe": sharpe,
    }
