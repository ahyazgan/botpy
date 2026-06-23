"""Claude API maliyet/kullanım takibi — gözlemlenebilirlik.

Sistem sinyal başına Claude çağrısı yapar: puanlama (tarama başına batch) + giriş
beyni (gate'leri geçen aday başına × `brain_vote_count` + eskalasyon). Bu maliyet
görünmezdi; `complexity_audit` çarpanı tahmin ediyordu ama gerçek token/çağrı
sayısı tutulmuyordu. Bu modül onu ölçer (kategori × model bazında çağrı + token)
ve yapılandırılabilir fiyatla USD tahmini verir.

Saf ve thread-safe (ağ/global durum yok). `news_bot` Claude yanıtının `usage`'ını
besler; burada yalnızca toplanır ve özetlenir.
"""

from __future__ import annotations

import threading
from typing import Any


class CostTracker:
    """Kategori × model bazında Claude çağrı + token toplaması. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (category, model) -> {calls, input_tokens, output_tokens}
        self._agg: dict[tuple[str, str], dict[str, int]] = {}

    def record(self, category: str, model: str,
               input_tokens: int, output_tokens: int) -> None:
        """Bir Claude çağrısının kullanımını ekle. Negatif/None token 0 sayılır."""
        it = max(0, int(input_tokens or 0))
        ot = max(0, int(output_tokens or 0))
        with self._lock:
            a = self._agg.get((category, model))
            if a is None:
                a = self._agg[(category, model)] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
            a["calls"] += 1
            a["input_tokens"] += it
            a["output_tokens"] += ot

    def summary(self, pricing: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
        """Kategori/model kırılımı + USD tahmini (pricing: model→(in_$/MTok, out_$/MTok)).

        Bilinmeyen model fiyatı 0 sayılır (çağrı/token yine sayılır). Döner: `by_key`
        (kategori+model satırları), `by_category` (kategori toplamları), `totals`.
        """
        price = pricing or {}

        def _cost(model: str, it: int, ot: int) -> float:
            rin, rout = price.get(model, (0.0, 0.0))
            return round(it / 1e6 * rin + ot / 1e6 * rout, 4)

        with self._lock:
            snapshot = {k: dict(v) for k, v in self._agg.items()}

        by_key = []
        by_cat: dict[str, dict[str, Any]] = {}
        tot_calls = tot_in = tot_out = 0
        tot_cost = 0.0
        for (cat, model), a in sorted(snapshot.items()):
            cost = _cost(model, a["input_tokens"], a["output_tokens"])
            by_key.append({"category": cat, "model": model, "calls": a["calls"],
                           "input_tokens": a["input_tokens"], "output_tokens": a["output_tokens"],
                           "est_cost_usd": cost})
            c = by_cat.setdefault(cat, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0})
            c["calls"] += a["calls"]
            c["input_tokens"] += a["input_tokens"]
            c["output_tokens"] += a["output_tokens"]
            c["est_cost_usd"] = round(c["est_cost_usd"] + cost, 4)
            tot_calls += a["calls"]
            tot_in += a["input_tokens"]
            tot_out += a["output_tokens"]
            tot_cost += cost
        return {
            "by_key": by_key, "by_category": by_cat,
            "totals": {"calls": tot_calls, "input_tokens": tot_in,
                       "output_tokens": tot_out, "est_cost_usd": round(tot_cost, 4)},
        }

    def reset(self) -> None:
        with self._lock:
            self._agg.clear()


_DEFAULT = CostTracker()


def record(category: str, model: str, input_tokens: int, output_tokens: int) -> None:
    """Modül-düzeyi varsayılan tracker'a kullanım ekle."""
    _DEFAULT.record(category, model, input_tokens, output_tokens)


def summary(pricing: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
    """Modül-düzeyi varsayılan tracker özeti."""
    return _DEFAULT.summary(pricing)


def reset() -> None:
    """Modül-düzeyi varsayılan tracker'ı temizle."""
    _DEFAULT.reset()
