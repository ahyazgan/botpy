"""
Yerel CORS proxy: Gamma market listesi + Binance BTC (dashboard.html icin).
"""

from __future__ import annotations

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from storage import Store

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
BINANCE_BTC_URL = "https://api.binance.com/api/v3/ticker/price"
REQUEST_TIMEOUT = 60
PAGE_LIMIT = 500

app = FastAPI(title="Polymarket proxy")

# arb_bot ile paylaşılan SQLite (radar fırsat geçmişi okunur)
_store = Store()

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


@app.get("/arb")
def get_arb(limit: int = 100) -> dict:
    """Arb radarı: arb_bot'un kaydettiği son fırsatlar (read-only)."""
    limit = max(1, min(limit, 500))
    rows = _store.list_opportunities(limit)
    return {"opportunities": rows, "count": len(rows)}


@app.get("/trades")
def get_open_trades() -> dict:
    """Açık paper pozisyonlar (read-only). Canlı PnL istemci tarafında hesaplanır."""
    rows = _store.list_trades()
    return {"trades": rows, "count": len(rows)}


@app.get("/trades/closed")
def get_closed_trades(limit: int = 200) -> dict:
    """Kapanan (realize) paper işlemler ve toplam gerçekleşen PnL."""
    limit = max(1, min(limit, 1000))
    rows = _store.list_closed_trades(limit)
    return {"trades": rows, "realized_pnl": _store.realized_pnl_total()}


class CloseBody(BaseModel):
    close_price: float = Field(gt=0, lt=1)


@app.post("/trades/{trade_id}/close")
def close_trade(trade_id: str, body: CloseBody) -> dict:
    """Açık pozisyonu istemci tarafında hesaplanan güncel fiyattan kapat."""
    closed = _store.close_trade(trade_id, body.close_price, "manual")
    if closed is None:
        raise HTTPException(status_code=404, detail="trade not found")
    return closed
