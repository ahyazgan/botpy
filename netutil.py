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

SleepFn = Callable[[float], None]


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

    - Bağlantı hatası / 5xx → `retries` kez dene, her denemede backoff*2**i bekle.
    - 4xx → denemeden None (istemci hatası, retry anlamsız).
    - 2xx ama JSON değil → None.
    """
    getter = (session or requests).get
    for attempt in range(max(1, retries)):
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
            if r.status_code < 500:
                return None  # istemci hatası → retry yok
            log.debug("get_json sunucu hatası (%s): HTTP %d", url, r.status_code)
        if attempt < retries - 1:
            sleep(backoff * (2 ** attempt))
    return None
