"""
Yerel CORS proxy: Gamma market listesi + Binance BTC (dashboard.html icin).
"""

from __future__ import annotations

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from metrics import compute_stats
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


@app.get("/pnl/curve")
def get_pnl_curve(limit: int = 1000) -> dict:
    """Kapanan işlemlerden kümülatif (equity) PnL eğrisi."""
    limit = max(1, min(limit, 5000))
    return {
        "points": _store.equity_curve(limit),
        "realized_pnl": _store.realized_pnl_total(),
    }


@app.get("/pnl/stats")
def get_pnl_stats() -> dict:
    """Kapanan işlemlerden performans metrikleri."""
    pnls = [t["pnl"] for t in _store.list_closed_trades(limit=5000)]
    return compute_stats(pnls)


@app.get("/audit")
def get_audit(limit: int = 200) -> dict:
    """Audit log + bekleyen emir niyetleri (crash recovery görünürlüğü)."""
    limit = max(1, min(limit, 1000))
    return {
        "events": _store.list_audit(limit),
        "open_intents": _store.list_open_intents(),
    }


@app.get("/history")
def get_history_info() -> dict:
    """Geçmiş snapshot kaydı durumu + kapsam."""
    return _store.snapshot_span()


@app.get("/backtest")
def run_backtest_endpoint(limit_per_market: int = 1000, amount: float = 10.0) -> dict:
    """Kaydedilmiş gerçek geçmiş veri üzerinde stratejiyi backtest et."""
    from backtest import run_backtest

    series = _store.history_series(max(1, min(limit_per_market, 5000)))
    if not series:
        return {"error": "geçmiş veri yok", "markets": 0,
                "trade_count": 0, "stats": compute_stats([])}
    res = run_backtest(series, amount=amount)
    return {
        "markets": len(series),
        "trade_count": len(res["trades"]),
        "stats": res["stats"],
    }


@app.get("/optimize")
def run_optimize_endpoint(
    objective: str = "total_pnl", min_trades: int = 3, top: int = 10,
) -> dict:
    """Geçmiş veride TP/SL ızgarasını tarayıp en iyi parametreleri bul."""
    from optimize import DEFAULT_GRID, grid_search

    series = _store.history_series(5000)
    if not series:
        return {"error": "geçmiş veri yok", "results": []}
    results = grid_search(
        series, DEFAULT_GRID,
        objective=objective, min_trades=max(1, min_trades), top=max(1, min(top, 50)),
    )
    return {"objective": objective, "markets": len(series), "results": results}


@app.get("/walkforward")
def run_walkforward_endpoint(
    train_frac: float = 0.7, objective: str = "total_pnl", min_trades: int = 3,
) -> dict:
    """Walk-forward doğrulama: in-sample optimize, out-of-sample test."""
    from optimize import DEFAULT_GRID
    from walkforward import walk_forward

    series = _store.history_series(5000)
    if not series:
        return {"ok": False, "reason": "geçmiş veri yok"}
    frac = min(0.9, max(0.1, train_frac))
    return walk_forward(
        series, DEFAULT_GRID,
        train_frac=frac, objective=objective, min_trades=max(1, min_trades),
    )


class CloseBody(BaseModel):
    close_price: float = Field(gt=0, lt=1)


@app.post("/trades/{trade_id}/close")
def close_trade(trade_id: str, body: CloseBody) -> dict:
    """Açık pozisyonu istemci tarafında hesaplanan güncel fiyattan kapat."""
    closed = _store.close_trade(trade_id, body.close_price, "manual")
    if closed is None:
        raise HTTPException(status_code=404, detail="trade not found")
    return closed
