"""
Dış API çağrıları: Gamma, Binance, CLOB.

Her fonksiyon bağımsız ve test edilebilir — side-effect yok.
"""
from __future__ import annotations

from typing import Any

import aiohttp
import requests

from config import (
    BINANCE_BTC_URL,
    CLOB_HOST,
    GAMMA_URL,
    PAGE_LIMIT,
    REQUEST_TIMEOUT,
)


# ── Senkron (scanner arka plan thread'i için) ────────────────────────────

def fetch_btc_price(session: requests.Session) -> float:
    r = session.get(BINANCE_BTC_URL, params={"symbol": "BTCUSDT"}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])


def fetch_all_markets_sync(session: requests.Session) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = session.get(
            GAMMA_URL,
            params={"active": "true", "closed": "false", "limit": PAGE_LIMIT, "offset": offset},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        batch: list[dict[str, Any]] = r.json()
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return markets


# ── Async (arbitraj botu için) ───────────────────────────────────────────

async def fetch_all_markets_async(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while True:
        async with session.get(
            GAMMA_URL,
            params={"active": "true", "closed": "false", "limit": PAGE_LIMIT, "offset": offset},
        ) as r:
            r.raise_for_status()
            batch: list[dict[str, Any]] = await r.json()
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return markets


async def fetch_clob_best_prices(
    session: aiohttp.ClientSession,
    token_id: str,
) -> tuple[float | None, float | None]:
    """CLOB orderbook'undan token için en iyi bid/ask döner."""
    async with session.get(f"{CLOB_HOST}/book", params={"token_id": token_id}) as r:
        if r.status != 200:
            return None, None
        data: dict[str, Any] = await r.json()

    def _best(entries: list[dict], fn):  # type: ignore[type-arg]
        prices = [float(e["price"]) for e in entries if e.get("price")]
        return fn(prices) if prices else None

    best_bid = _best(data.get("bids") or [], max)
    best_ask = _best(data.get("asks") or [], min)
    return best_bid, best_ask
