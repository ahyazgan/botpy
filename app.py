"""
FastAPI uygulaması.

Rotalar bağımsız — scanner ve trader bağımlılıkları enjekte edilir.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import config
from models import (
    BtcResponse,
    MarketRow,
    MarketsResponse,
    SettingsBody,
    SettingsResponse,
    TradeRequest,
    TradeRow,
    TradesResponse,
)
from scanner import BackgroundScanner, cache
from trader import PaperTrader

log = logging.getLogger(__name__)

_scanner = BackgroundScanner()
_paper   = PaperTrader()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _scanner.start()
    yield
    _scanner.stop()


app = FastAPI(title="Polymarket Scanner v2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Okuma rotaları ───────────────────────────────────────────────────────

@app.get("/btc", response_model=BtcResponse)
def get_btc() -> BtcResponse:
    data = cache.get()
    return BtcResponse(price=data["btc_price"], updated_at=data["updated_at"], error=data["error"])


@app.get("/markets", response_model=MarketsResponse)
def get_markets() -> MarketsResponse:
    data = cache.get()
    rows = [MarketRow(**r) for r in data["markets"]]
    return MarketsResponse(
        markets=rows,
        paper_mode=config.PAPER_MODE,
        total_active=data["total_active"],
        filtered_count=data["filtered_count"],
        min_volume_24hr=config.MIN_VOLUME_24H,
        updated_at=data["updated_at"],
        error=data["error"],
    )


# ── Ayarlar ──────────────────────────────────────────────────────────────

@app.patch("/settings", response_model=SettingsResponse)
def patch_settings(body: SettingsBody) -> SettingsResponse:
    config.PAPER_MODE = body.paper_mode
    log.info("PAPER_MODE -> %s", config.PAPER_MODE)
    return SettingsResponse(paper_mode=config.PAPER_MODE)


# ── Trade rotaları ───────────────────────────────────────────────────────

@app.post("/trade", response_model=TradeRow)
def post_trade(body: TradeRequest) -> TradeRow:
    side = body.side.upper()
    if side not in ("YES", "NO"):
        raise HTTPException(status_code=400, detail="side must be YES or NO")

    data = cache.get()
    market = next((m for m in data["markets"] if m["id"] == body.market_id), None)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    entry = market.get("ask") if side == "YES" else (
        (1.0 - market["bid"]) if market.get("bid") is not None else None
    )
    if entry is None or entry <= 0:
        raise HTTPException(status_code=422, detail="entry price unavailable")

    trade = _paper.open(body.market_id, market["question"], side, body.amount_usdc, entry)
    cp = cache.market_current_price(body.market_id, side)
    pnl = (trade["shares"] * cp - body.amount_usdc) if cp is not None else None
    pnl_pct = ((cp / entry) - 1) * 100 if cp is not None else None
    return TradeRow(**trade, current_price=cp, pnl=pnl, pnl_pct=pnl_pct)


@app.get("/trades", response_model=TradesResponse)
def get_trades() -> TradesResponse:
    return _paper.build_response(cache.market_current_price)


@app.delete("/trades/{trade_id}")
def delete_trade(trade_id: str) -> dict[str, str]:
    if not _paper.delete(trade_id):
        raise HTTPException(status_code=404, detail="trade not found")
    return {"deleted": trade_id}
