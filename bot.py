"""
Polymarket market tarayici: Gamma API + Binance BTC spot.
FastAPI: /markets, /btc, /settings (PAPER_MODE), /trade, /trades.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

PAPER_MODE: bool = True
SCAN_INTERVAL_SEC: int = 30
MIN_VOLUME_24HR: float = 10_000.0

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
BINANCE_BTC_SPOT_URL = "https://api.binance.com/api/v3/ticker/price"
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 500

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {
    "markets": [],
    "btc_price": None,
    "total_active": 0,
    "filtered_count": 0,
    "error": None,
    "updated_at": None,
}

# In-memory paper trade store
_trades_lock = threading.Lock()
_paper_trades: list[dict[str, Any]] = []


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_btc_spot_usdt(session: requests.Session) -> float | None:
    r = session.get(
        BINANCE_BTC_SPOT_URL,
        params={"symbol": "BTCUSDT"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return float(r.json()["price"])


def fetch_active_markets(session: requests.Session) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = session.get(
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


def build_rows(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = [
        m
        for m in raw
        if (to_float(m.get("volume24hr")) or 0.0) > MIN_VOLUME_24HR
    ]
    filtered.sort(
        key=lambda m: to_float(m.get("volume24hr")) or 0.0,
        reverse=True,
    )
    rows: list[dict[str, Any]] = []
    for m in filtered:
        bid = to_float(m.get("bestBid"))
        ask = to_float(m.get("bestAsk"))
        if bid is not None and ask is not None:
            spread = ask - bid
        else:
            spread = to_float(m.get("spread"))
        rows.append(
            {
                "id": str(m.get("id", "")),
                "question": (m.get("question") or m.get("slug") or "?").strip(),
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "volume24h": to_float(m.get("volume24hr")),
            }
        )
    return rows


def refresh_snapshot(session: requests.Session) -> None:
    err: str | None = None
    try:
        btc = fetch_btc_spot_usdt(session)
        raw = fetch_active_markets(session)
        rows = build_rows(raw)
        with _cache_lock:
            _cache["btc_price"] = btc
            _cache["markets"] = rows
            _cache["total_active"] = len(raw)
            _cache["filtered_count"] = len(rows)
            _cache["error"] = None
            _cache["updated_at"] = datetime.now(timezone.utc).isoformat()
        logging.info(
            "Snapshot | BTC=%s | aktif=%d | filtre=%d | PAPER_MODE=%s",
            f"{btc:,.2f}" if btc is not None else "n/a",
            len(raw),
            len(rows),
            PAPER_MODE,
        )
    except requests.RequestException as e:
        err = str(e)
        logging.exception("HTTP hatasi: %s", e)
        with _cache_lock:
            _cache["error"] = err
            _cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        err = str(e)
        logging.exception("Snapshot hatasi: %s", e)
        with _cache_lock:
            _cache["error"] = err
            _cache["updated_at"] = datetime.now(timezone.utc).isoformat()


def _background_loop(stop: threading.Event) -> None:
    session = requests.Session()
    session.headers.setdefault(
        "User-Agent",
        "polymarket-scanner/1.0 (+https://polymarket.com)",
    )
    while not stop.is_set():
        refresh_snapshot(session)
        if stop.wait(SCAN_INTERVAL_SEC):
            break


_stop_event = threading.Event()
_bg_thread: threading.Thread | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_thread
    setup_logging()
    _stop_event.clear()
    _bg_thread = threading.Thread(
        target=_background_loop,
        args=(_stop_event,),
        daemon=True,
    )
    _bg_thread.start()
    yield
    _stop_event.set()
    if _bg_thread:
        _bg_thread.join(timeout=5)


app = FastAPI(title="Polymarket Scanner", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BtcResponse(BaseModel):
    price: float | None
    symbol: str = "BTCUSDT"
    updated_at: str | None = None
    error: str | None = None


class MarketRow(BaseModel):
    id: str
    question: str
    bid: float | None
    ask: float | None
    spread: float | None
    volume24h: float | None


class MarketsResponse(BaseModel):
    markets: list[MarketRow]
    paper_mode: bool
    total_active: int
    filtered_count: int
    min_volume_24hr: float = Field(default=MIN_VOLUME_24HR)
    updated_at: str | None = None
    error: str | None = None


class SettingsBody(BaseModel):
    paper_mode: bool


class SettingsResponse(BaseModel):
    paper_mode: bool


class TradeRequest(BaseModel):
    market_id: str
    side: str  # "YES" | "NO"
    amount_usdc: float = Field(gt=0)


class TradeRow(BaseModel):
    id: str
    market_id: str
    question: str
    side: str
    amount_usdc: float
    entry_price: float
    shares: float
    current_price: float | None
    pnl: float | None
    pnl_pct: float | None
    opened_at: str


class TradesResponse(BaseModel):
    trades: list[TradeRow]
    total_pnl: float


@app.get("/btc", response_model=BtcResponse)
def get_btc() -> BtcResponse:
    with _cache_lock:
        return BtcResponse(
            price=_cache["btc_price"],
            updated_at=_cache["updated_at"],
            error=_cache["error"],
        )


@app.get("/markets", response_model=MarketsResponse)
def get_markets() -> MarketsResponse:
    global PAPER_MODE
    with _cache_lock:
        rows = [MarketRow(**r) for r in _cache["markets"]]
        return MarketsResponse(
            markets=rows,
            paper_mode=PAPER_MODE,
            total_active=_cache["total_active"],
            filtered_count=_cache["filtered_count"],
            min_volume_24hr=MIN_VOLUME_24HR,
            updated_at=_cache["updated_at"],
            error=_cache["error"],
        )


@app.patch("/settings", response_model=SettingsResponse)
def patch_settings(body: SettingsBody) -> SettingsResponse:
    global PAPER_MODE
    PAPER_MODE = body.paper_mode
    logging.info("PAPER_MODE -> %s", PAPER_MODE)
    return SettingsResponse(paper_mode=PAPER_MODE)


def _market_current_price(market_id: str, side: str) -> float | None:
    """Returns current mark price for the given side from cache."""
    with _cache_lock:
        for m in _cache["markets"]:
            if m["id"] == market_id:
                if side == "YES":
                    return m.get("bid")  # exit price for YES = bid
                else:
                    ask = m.get("ask")
                    return (1.0 - ask) if ask is not None else None
    return None


@app.post("/trade", response_model=TradeRow)
def post_trade(body: TradeRequest) -> TradeRow:
    side = body.side.upper()
    if side not in ("YES", "NO"):
        raise HTTPException(status_code=400, detail="side must be YES or NO")

    with _cache_lock:
        market = next((m for m in _cache["markets"] if m["id"] == body.market_id), None)

    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    if side == "YES":
        entry = market.get("ask")
    else:
        bid = market.get("bid")
        entry = (1.0 - bid) if bid is not None else None

    if entry is None or entry <= 0:
        raise HTTPException(status_code=422, detail="entry price unavailable")

    shares = body.amount_usdc / entry
    trade: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "market_id": body.market_id,
        "question": market["question"],
        "side": side,
        "amount_usdc": body.amount_usdc,
        "entry_price": entry,
        "shares": shares,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    with _trades_lock:
        _paper_trades.append(trade)

    logging.info(
        "PAPER TRADE | %s | %s | %.2f USDC @ %.4f | shares=%.4f",
        trade["question"][:50],
        side,
        body.amount_usdc,
        entry,
        shares,
    )

    current_price = _market_current_price(body.market_id, side)
    pnl = (shares * current_price - body.amount_usdc) if current_price is not None else None
    pnl_pct = ((current_price / entry) - 1) * 100 if current_price is not None else None
    return TradeRow(**trade, current_price=current_price, pnl=pnl, pnl_pct=pnl_pct)


@app.get("/trades", response_model=TradesResponse)
def get_trades() -> TradesResponse:
    with _trades_lock:
        snapshot = list(_paper_trades)

    rows: list[TradeRow] = []
    total_pnl = 0.0
    for t in snapshot:
        current_price = _market_current_price(t["market_id"], t["side"])
        pnl = (t["shares"] * current_price - t["amount_usdc"]) if current_price is not None else None
        pnl_pct = ((current_price / t["entry_price"]) - 1) * 100 if current_price is not None else None
        rows.append(TradeRow(**t, current_price=current_price, pnl=pnl, pnl_pct=pnl_pct))
        if pnl is not None:
            total_pnl += pnl

    return TradesResponse(trades=rows, total_pnl=total_pnl)


@app.delete("/trades/{trade_id}")
def delete_trade(trade_id: str) -> dict[str, str]:
    with _trades_lock:
        idx = next((i for i, t in enumerate(_paper_trades) if t["id"] == trade_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="trade not found")
        _paper_trades.pop(idx)
    return {"deleted": trade_id}


def run_scan(session: requests.Session) -> None:
    """CLI dongusu icin (eski davranis)."""
    refresh_snapshot(session)
    if PAPER_MODE:
        logging.info("PAPER_MODE: islem acilmadi.")


def main_cli() -> None:
    setup_logging()
    logging.info(
        "Basladi (CLI) | PAPER_MODE=%s | dongu=%ds | min_vol24h=%.0f",
        PAPER_MODE,
        SCAN_INTERVAL_SEC,
        MIN_VOLUME_24HR,
    )
    session = requests.Session()
    session.headers.setdefault(
        "User-Agent",
        "polymarket-scanner/1.0 (+https://polymarket.com)",
    )
    while True:
        try:
            run_scan(session)
        except requests.RequestException:
            logging.exception("HTTP istegi basarisiz")
        except Exception:
            logging.exception("Beklenmeyen hata")
        time.sleep(SCAN_INTERVAL_SEC)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket scanner")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Sadece konsol log dongusu (FastAPI yok)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="API host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="API port",
    )
    args = parser.parse_args()
    if args.cli:
        main_cli()
        return
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
