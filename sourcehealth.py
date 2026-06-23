"""Kaynak sağlık kaydı — besleme redundansı için durum-makinesi.

Asıl gerçek-zamanlı edge tek beslemeye (TreeNews WS) bağlı; `_scan_interval`
failover'ı onu yedekle telafi eder. Bu modül ise **yedek kaynakların kendisinin**
(RSS feed'leri + Binance duyuruları) sağlığını izler: üst üste hata veren bir
kaynağı geçici DEVRE DIŞI bırakır (sürekli timeout'la taramayı yavaşlatmasın),
cooldown sonrası otomatik dener, toparlanınca geri alır.

Saf ve thread-safe (ağ/global durum yok). `news_bot.fetch_all` her kaynak
sonucunu besler; geçiş olayları (disabled/recovered) uzak kanaldan bildirilir.

Durum-makinesi (kaynak başına):
- `record_failure` ardışık hata sayar; eşiği (`fail_threshold`) aşınca DEVRE DIŞI
  → tek "disabled" geçişi döndürür (cooldown re-arm sessiz).
- cooldown dolunca `is_disabled` False olur → kaynak yeniden denenir (probe).
- `record_success` ardışık sayacı sıfırlar; devre dışıyken gelirse "recovered".
"""

from __future__ import annotations

import threading
import time
from typing import Any


class SourceHealth:
    def __init__(self, fail_threshold: int = 3, cooldown_sec: float = 300.0) -> None:
        self._lock = threading.Lock()
        self._fail_threshold = max(1, fail_threshold)
        self._cooldown = cooldown_sec
        self._state: dict[str, dict[str, Any]] = {}

    def _entry(self, name: str) -> dict[str, Any]:
        e = self._state.get(name)
        if e is None:
            e = self._state[name] = {
                "consecutive_fails": 0, "total_ok": 0, "total_fail": 0,
                "last_ok": None, "last_fail": None, "last_error": "",
                "disabled": False, "disabled_until": None,
            }
        return e

    def record_success(self, name: str, now: float | None = None) -> str | None:
        """Başarılı çekim. Devre dışıyken gelirse 'recovered' geçişi, aksi halde None."""
        t = now if now is not None else time.time()
        with self._lock:
            e = self._entry(name)
            e["consecutive_fails"] = 0
            e["total_ok"] += 1
            e["last_ok"] = t
            if e["disabled"]:
                e["disabled"] = False
                e["disabled_until"] = None
                return "recovered"
            return None

    def record_failure(self, name: str, error: str = "", now: float | None = None) -> str | None:
        """Başarısız çekim. Eşiği YENİ aşıyorsa 'disabled' geçişi, aksi halde None."""
        t = now if now is not None else time.time()
        with self._lock:
            e = self._entry(name)
            e["consecutive_fails"] += 1
            e["total_fail"] += 1
            e["last_fail"] = t
            e["last_error"] = error[:200]
            if e["consecutive_fails"] >= self._fail_threshold:
                e["disabled_until"] = t + self._cooldown   # cooldown'ı re-arm et
                if not e["disabled"]:
                    e["disabled"] = True
                    return "disabled"
            return None

    def is_disabled(self, name: str, now: float | None = None) -> bool:
        """Kaynak şu an devre dışı mı (cooldown henüz dolmadı). Cooldown bitince yeniden dener."""
        t = now if now is not None else time.time()
        with self._lock:
            e = self._state.get(name)
            if not e or not e["disabled"]:
                return False
            du = e["disabled_until"]
            return du is not None and t < du

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Tüm kaynakların durumu (gözlemlenebilirlik). Saf okuma."""
        t = now if now is not None else time.time()
        with self._lock:
            out = {}
            for name, e in self._state.items():
                du = e["disabled_until"]
                out[name] = {
                    "healthy": not (e["disabled"] and du is not None and t < du),
                    "disabled": bool(e["disabled"]),
                    "consecutive_fails": e["consecutive_fails"],
                    "total_ok": e["total_ok"], "total_fail": e["total_fail"],
                    "last_ok": e["last_ok"], "last_fail": e["last_fail"],
                    "last_error": e["last_error"],
                    "retry_in_sec": round(du - t, 1) if (e["disabled"] and du and t < du) else 0,
                }
            return out

    def reset(self) -> None:
        with self._lock:
            self._state.clear()
