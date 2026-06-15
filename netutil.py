"""Dış API çağrıları için retry + üstel backoff sarmalayıcısı.

Ağ hataları ve 5xx (sunucu) yanıtlarında sınırlı sayıda yeniden dener; 4xx
(istemci) yanıtlarında denemez. Başarısızlıkta None döner (çağıranlar zaten
None bekliyor). `sleep` enjekte edilebilir → testte ağsız/anında.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

# Rate-limit / sunucu durumları yeniden denenir. Binance aşımda 429 (Too Many
# Requests) ve 418 (IP yasağı) döner — bunlar 4xx olsa da retryable'dır.
RETRYABLE_STATUS = {418, 429}
RETRY_AFTER_MAX = 120.0   # Retry-After başlığına uyulurken üst sınır (saniye)

SleepFn = Callable[[float], None]


def _retry_after_seconds(resp: Any) -> float | None:
    """`Retry-After` başlığını saniyeye çevir (yalnızca delta-seconds). Saf.

    Geçersiz/eksik/negatifse None (çağıran üstel backoff'a düşer).
    """
    val = getattr(resp, "headers", {}).get("Retry-After")
    if not val:
        return None
    try:
        secs = float(int(str(val).strip()))
    except (ValueError, TypeError):
        return None
    return secs if secs >= 0 else None


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 10,
    retries: int = 3,
    backoff: float = 0.4,
    session: requests.Session | None = None,
    sleep: SleepFn = time.sleep,
) -> Any | None:
    """GET → JSON, retry'lı. Başarısızlıkta None.

    - Bağlantı hatası / 5xx / 429-418 → `retries` kez dene; bekleme üstel backoff,
      ancak yanıtta `Retry-After` varsa ona uyulur (RETRY_AFTER_MAX ile sınırlı).
    - Diğer 4xx → denemeden None. 2xx ama JSON değil → None.
    """
    getter = (session or requests).get
    for attempt in range(max(1, retries)):
        wait = backoff * (2 ** attempt)
        try:
            r = getter(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            log.debug("get_json bağlantı hatası (%s, deneme %d): %s", url, attempt + 1, e)
        else:
            if r.status_code < 400:
                try:
                    return r.json()
                except ValueError:
                    return None
            if r.status_code < 500 and r.status_code not in RETRYABLE_STATUS:
                return None  # istemci hatası (404 vb.) → retry yok
            # 5xx veya 429/418 (rate-limit) → yeniden dene; sunucu Retry-After verdiyse ona uy
            ra = _retry_after_seconds(r)
            if ra is not None:
                wait = min(ra, RETRY_AFTER_MAX)
            log.debug("get_json yeniden denenebilir durum (%s): HTTP %d (bekleme %.1fs)", url, r.status_code, wait)
        if attempt < retries - 1:
            sleep(wait)
    return None
