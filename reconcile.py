"""
Pozisyon mutabakatı — botun bildiği pozisyonlar ile zincirdeki gerçeğin
karşılaştırılması. Saf fonksiyon (ağ yok); zincir verisi dışarıdan gelir.

Bot çökerse ya da emir beklenmedik dolarsa, yerel görüş ile gerçek
ayrışır. Bu ayrışmayı erken yakalamak gerçek parada kritiktir.
"""

from __future__ import annotations

from typing import Any


def diff_positions(
    local: dict[str, float],
    chain: dict[str, float],
    *,
    tol: float = 1e-6,
) -> dict[str, Any]:
    """Yerel ve zincir pozisyonlarını karşılaştır (token_id -> miktar).

    Döner:
      ok: tüm pozisyonlar eşleşiyor mu
      missing_on_chain: yerelde var ama zincirde yok/eksik (sahip sandığımız)
      unexpected_on_chain: zincirde var ama yerelde yok (takip edilmeyen)
      mismatched: ikisinde de var ama miktar farklı
    """
    missing: dict[str, dict[str, float]] = {}
    unexpected: dict[str, dict[str, float]] = {}
    mismatched: dict[str, dict[str, float]] = {}

    for token in set(local) | set(chain):
        lv = float(local.get(token, 0.0))
        cv = float(chain.get(token, 0.0))
        if abs(lv - cv) <= tol:
            continue
        entry = {"local": lv, "chain": cv}
        if cv <= tol:           # zincirde efektif olarak yok
            missing[token] = entry
        elif lv <= tol:         # yerelde efektif olarak yok
            unexpected[token] = entry
        else:                   # ikisinde de var, miktar farklı
            mismatched[token] = entry

    ok = not (missing or unexpected or mismatched)
    return {
        "ok": ok,
        "missing_on_chain": missing,
        "unexpected_on_chain": unexpected,
        "mismatched": mismatched,
    }
