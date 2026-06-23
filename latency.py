"""Pipeline gecikme ölçümü — haber-trade'in gerçek edge'i.

Haber-trade botunda asıl kazanç "haber geldi → emir gitti" arasındaki
saniyelerde saklıdır. Beynin ne kadar akıllı olduğu kadar, hatta ondan çok,
**ne kadar hızlı** girdiği önemlidir. Bu modül boru hattının her aşamasını
ölçer ve `/metrics` + `/latency` ile dışa verir.

Saf ve thread-safe: ağ/global durum yok, sadece kayan örnek penceresi tutar.
`news_bot` aşama sürelerini besler; burada yalnızca toplanır ve özetlenir.

Aşamalar (ms):
- ``ingest``    kaynak yayını → bot alımı (TreeNews + ağ gecikmesi)
- ``score``     Claude rafine puanlama süresi (batch)
- ``confirm``   Binance fiyat teyidi süresi
- ``order``     uyarı kararı → emir gönderildi
- ``pipeline``  alım → emir (uçtan uca aksiyon gecikmesi — manşet sayı)
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

_MAXLEN = 500   # aşama başına tutulan son örnek sayısı (kayan pencere)


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


def _stats(vals: list[float]) -> dict[str, float]:
    """Örnek listesinden özet (count/avg/p50/p95/max/last). Saf."""
    s = sorted(vals)
    return {
        "count": len(s),
        "avg_ms": round(sum(s) / len(s), 1),
        "p50_ms": round(_percentile(s, 0.50), 1),
        "p95_ms": round(_percentile(s, 0.95), 1),
        "max_ms": round(s[-1], 1),
        "last_ms": round(vals[-1], 1),
    }


class LatencyTracker:
    """Aşama başına kayan örnek penceresi + özet istatistik. Thread-safe."""

    def __init__(self, maxlen: int = _MAXLEN) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, deque[float]] = {}
        self._maxlen = maxlen

    def record(self, stage: str, ms: float | None) -> None:
        """Bir aşama örneği ekle. None/negatif (saat kayması/sıra hatası) yok sayılır."""
        if ms is None or ms < 0:
            return
        with self._lock:
            dq = self._stages.get(stage)
            if dq is None:
                dq = self._stages[stage] = deque(maxlen=self._maxlen)
            dq.append(float(ms))

    def summary(self) -> dict[str, dict[str, float]]:
        """Tüm aşamaların özetini döndür (boş aşamalar atlanır)."""
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            snapshot = {k: list(v) for k, v in self._stages.items()}
        for stage, vals in snapshot.items():
            if vals:
                out[stage] = _stats(vals)
        return out

    def reset(self) -> None:
        """Tüm örnekleri temizle (test/operasyon)."""
        with self._lock:
            self._stages.clear()


def flatten_metrics(summary: dict[str, dict[str, float]], prefix: str = "botpy_latency") -> dict[str, float]:
    """Özet sözlüğünü Prometheus-uyumlu düz gauge'lere çevir. Saf.

    Örn. {"pipeline": {"p50_ms": 120, ...}} → {"botpy_latency_pipeline_p50_ms": 120, ...}
    Yalnız p50/p95/max + count dışa verilir (kardinaliteyi düşük tut).
    """
    out: dict[str, float] = {}
    for stage, st in summary.items():
        for key in ("p50_ms", "p95_ms", "max_ms", "count"):
            if key in st:
                out[f"{prefix}_{stage}_{key}"] = st[key]
    return out


_DEFAULT = LatencyTracker()
_BY_SOURCE = LatencyTracker()   # kaynak-bazlı ingest kırılımı (hangi besleme yavaş)


def record(stage: str, ms: float | None) -> None:
    """Modül-düzeyi varsayılan tracker'a örnek ekle."""
    _DEFAULT.record(stage, ms)


def record_source(bucket: str, ms: float | None) -> None:
    """Kaynak-bazlı ingest örneği (bucket: treenews/binance/rss...)."""
    _BY_SOURCE.record(bucket, ms)


def summary() -> dict[str, dict[str, float]]:
    """Modül-düzeyi varsayılan tracker özeti."""
    return _DEFAULT.summary()


def source_summary() -> dict[str, dict[str, float]]:
    """Kaynak-bazlı ingest özeti."""
    return _BY_SOURCE.summary()


def reset() -> None:
    """Modül-düzeyi varsayılan tracker'ları temizle."""
    _DEFAULT.reset()
    _BY_SOURCE.reset()


def get_metrics() -> dict[str, Any]:
    """Düz Prometheus gauge sözlüğü (varsayılan tracker'dan)."""
    return flatten_metrics(_DEFAULT.summary())


def evaluate_sla(summary: dict[str, dict[str, float]],
                 sla_ms: dict[str, float], min_samples: int = 5) -> dict[str, dict[str, Any]]:
    """Her aşamanın p95'ini SLA eşiğiyle kıyasla. Saf.

    Yalnız SLA tanımlı + yeterli örneği (`min_samples`) olan aşamalar değerlendirilir.
    Dönen: {stage: {p95_ms, sla_ms, ok, samples}}. `ok=False` = p95 eşiği aştı (yavaş).
    """
    out: dict[str, dict[str, Any]] = {}
    for stage, limit in sla_ms.items():
        st = summary.get(stage)
        if not st or st.get("count", 0) < min_samples:
            continue
        p95 = st["p95_ms"]
        out[stage] = {"p95_ms": p95, "sla_ms": limit, "ok": p95 <= limit,
                      "samples": int(st["count"])}
    return out
