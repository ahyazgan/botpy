"""
Arka plan tarama döngüsü.

Periyodik olarak Gamma API + Binance'den veri çeker,
sonuçları thread-safe bir cache'de tutar.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

import config
from fetcher import fetch_all_markets_sync, fetch_btc_price
from screener import build_market_rows

log = logging.getLogger(__name__)


class Cache:
    """Thread-safe veri deposu."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "markets": [],
            "btc_price": None,
            "total_active": 0,
            "filtered_count": 0,
            "error": None,
            "updated_at": None,
        }

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)
            self._data["updated_at"] = datetime.now(timezone.utc).isoformat()

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def market_current_price(self, market_id: str, side: str) -> float | None:
        with self._lock:
            for m in self._data["markets"]:
                if m["id"] == market_id:
                    if side == "YES":
                        return m.get("bid")
                    ask = m.get("ask")
                    return (1.0 - ask) if ask is not None else None
        return None


cache = Cache()


def _refresh(session: requests.Session) -> None:
    try:
        btc = fetch_btc_price(session)
        raw = fetch_all_markets_sync(session)
        rows = build_market_rows(raw)

        cache.update(
            btc_price=btc,
            markets=rows,
            total_active=len(raw),
            filtered_count=len(rows),
            error=None,
        )
        log.info(
            "Snapshot | BTC=%s | aktif=%d | filtre=%d | PAPER_MODE=%s",
            f"{btc:,.2f}" if btc is not None else "n/a",
            len(raw),
            len(rows),
            config.PAPER_MODE,
        )
    except requests.RequestException as e:
        log.exception("HTTP hatası: %s", e)
        cache.update(error=str(e))
    except Exception as e:
        log.exception("Snapshot hatası: %s", e)
        cache.update(error=str(e))


def _loop(stop: threading.Event) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-scanner/2.0"
    while not stop.is_set():
        _refresh(session)
        stop.wait(config.SCAN_INTERVAL_SEC)


class BackgroundScanner:
    """Başlat/durdur arayüzü — FastAPI lifespan'ı ile kullanılır."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=_loop, args=(self._stop,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
