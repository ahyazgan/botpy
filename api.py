"""
Yerel CORS proxy: Gamma market listesi + Binance BTC (dashboard.html icin).
"""

from __future__ import annotations

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
BINANCE_BTC_URL = "https://api.binance.com/api/v3/ticker/price"
REQUEST_TIMEOUT = 60
PAGE_LIMIT = 500

app = FastAPI(title="Polymarket proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_session = requests.Session()
_session.headers.setdefault(
    "User-Agent",
    "polymarket-api-proxy/1.0 (+https://polymarket.com)",
)


def _fetch_all_active_markets() -> list[dict]:
    markets: list[dict] = []
    offset = 0
    while True:
        r = _session.get(
            GAMMA_MARKETS_URL,
            params={
                "active": "true",
                "closed": "false",
                "limit": PAGE_LIMIT,
                "offset": offset,
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return markets


@app.get("/markets")
def get_markets() -> list[dict]:
    return _fetch_all_active_markets()


@app.get("/btc")
def get_btc() -> dict:
    r = _session.get(
        BINANCE_BTC_URL,
        params={"symbol": "BTCUSDT"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return {
        "symbol": data.get("symbol", "BTCUSDT"),
        "price": data.get("price"),
    }
