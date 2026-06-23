"""Monte Carlo / bootstrap risk simülasyonu — sonuç DAĞILIMI + iflas riski.

Drawdown kill-switch tek (gerçekleşen) noktaya bakar; gerçekleşen P&L eğrisi yalnız
BİR örnek yoldur — farklı bir işlem SIRASIYLA drawdown çok daha kötü olabilirdi. Bu
modül kapanan işlem sonuçlarını yeniden örnekleyerek (bootstrap) binlerce alternatif
yol üretir → final P&L dağılımı (p5/p50/p95), max-drawdown dağılımı (medyan/p95/en kötü),
kâr olasılığı ve **iflas riski** (sermayenin %X altına düşme olasılığı).

Saf ve deterministik (seed ile). Ağ/global durum yok.
"""

from __future__ import annotations

import random
from typing import Any


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Doğrusal-interpolasyonlu yüzdelik (sıralı liste, q∈[0,1]). Saf."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _run_path(pnls: list[float], base: float, ruin_level: float) -> tuple[float, float, bool]:
    """Tek örneklenmiş yolu simüle et → (final_pnl, max_drawdown_pct, ruined). Saf."""
    eq = base
    peak = base
    max_dd_pct = 0.0
    ruined = False
    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        if peak > 0:
            dd_pct = (peak - eq) / peak * 100.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
        if eq <= ruin_level:
            ruined = True
    return eq - base, round(max_dd_pct, 2), ruined


def monte_carlo(pnls: list[float], *, runs: int = 2000, account_equity: float = 10000.0,
                ruin_pct: float = 50.0, seed: int | None = None,
                min_trades: int = 20) -> dict[str, Any]:
    """Kapanan işlem P&L'lerinden bootstrap risk dağılımı (saf, deterministik seed ile).

    Her run: `pnls` listesinden YERİNE-KOYMALI `len(pnls)` işlem örnekle → equity yolu
    kur, final P&L + max-drawdown% + iflas (sermaye `ruin_pct`% altına düştü mü) izle.
    Dağılımı özetle. `reliable`=örnek `min_trades`'ten az değilse (azsa sonuç gürültülü).

    Döner: final_pnl {p5/p50/p95/mean}, max_drawdown_pct {p50/p95/worst},
    prob_profit (%), risk_of_ruin (%), n_trades, runs, reliable.
    """
    clean = [float(p) for p in pnls if p is not None]
    n = len(clean)
    if n == 0:
        return {"ok": False, "reason": "kapanan işlem yok", "n_trades": 0}
    runs = max(1, min(runs, 100_000))
    base = max(1e-9, account_equity)
    ruin_level = base * (1 - ruin_pct / 100.0)
    rng = random.Random(seed)

    finals: list[float] = []
    dds: list[float] = []
    ruined = 0
    for _ in range(runs):
        sample = [clean[rng.randrange(n)] for _ in range(n)]
        f, dd, ru = _run_path(sample, base, ruin_level)
        finals.append(f)
        dds.append(dd)
        if ru:
            ruined += 1

    finals.sort()
    dds.sort()
    return {
        "ok": True, "n_trades": n, "runs": runs,
        "account_equity": round(base, 2), "ruin_pct": ruin_pct,
        "reliable": n >= min_trades,
        "final_pnl": {
            "p5": round(_percentile(finals, 0.05), 2),
            "p50": round(_percentile(finals, 0.50), 2),
            "p95": round(_percentile(finals, 0.95), 2),
            "mean": round(sum(finals) / len(finals), 2),
        },
        "max_drawdown_pct": {
            "p50": round(_percentile(dds, 0.50), 2),
            "p95": round(_percentile(dds, 0.95), 2),
            "worst": round(dds[-1], 2),
        },
        "prob_profit": round(sum(1 for f in finals if f > 0) / len(finals) * 100, 1),
        "risk_of_ruin": round(ruined / runs * 100, 2),
        "note": "Bootstrap (yerine-koymalı yeniden örnekleme): edge gerçekse sonuç yelpazesi. "
                "risk_of_ruin = sermayenin %{:.0f} altına düşen yol oranı.".format(ruin_pct),
    }
