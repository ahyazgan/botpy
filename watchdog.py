"""Bot iç döngü watchdog'u — kendi thread'lerinin canlılığını izler.

Ölü-adam anahtarı (`_ws_feed_stale`) DIŞ haber akışını izler; bu modül botun
KENDİ arka plan döngülerini izler. Özellikle pozisyon-izleme döngüsü (`_monitor_loop`)
takılır/ölürse SL/TP/trailing hiç tetiklenmez → pozisyon stop'unu deler (felaket).
Her döngü her turun sonunda `beat(name)` çağırır; watchdog thread'i `health()` ile
heartbeat yaşını eşikle kıyaslar; bayat döngü = takılı/ölü.

Saf ve thread-safe (ağ/global durum yok). `monotonic` saat kullanır (duvar saati
sıçramalarından etkilenmez); `now` enjekte edilebilir (test).
"""

from __future__ import annotations

import threading
import time
from typing import Any


class Watchdog:
    """Döngü-başına heartbeat kaydı + bayatlık tespiti. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._beats: dict[str, float] = {}        # name -> son monotonic heartbeat
        self._thresholds: dict[str, float] = {}   # name -> bayatlık eşiği (sn)

    def register(self, name: str, threshold_sec: float, now: float | None = None) -> None:
        """Bir döngüyü izlemeye al (başlangıç heartbeat'i = şimdi)."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            self._thresholds[name] = float(threshold_sec)
            self._beats[name] = t

    def beat(self, name: str, now: float | None = None) -> None:
        """Döngü canlı: heartbeat'i tazele (her tur sonunda çağrılır)."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            self._beats[name] = t

    def health(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        """Kayıtlı her döngünün durumu: {name: {age_sec, threshold_sec, stale}}."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            items = {n: (self._beats.get(n, t), thr) for n, thr in self._thresholds.items()}
        out: dict[str, dict[str, Any]] = {}
        for name, (last, thr) in items.items():
            age = max(0.0, t - last)
            out[name] = {"age_sec": round(age, 1), "threshold_sec": thr, "stale": age > thr}
        return out

    def stale(self, now: float | None = None) -> list[str]:
        """Şu an bayat (takılı/ölü) döngü adları."""
        return [n for n, v in self.health(now).items() if v["stale"]]

    def reset(self) -> None:
        with self._lock:
            self._beats.clear()
            self._thresholds.clear()
