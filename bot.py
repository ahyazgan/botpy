"""
Polymarket market tarayici: Gamma API + Binance BTC spot.
FastAPI: /markets, /btc, /settings (paper_mode), /trade, /trades, /health.

Durum yonetimi: global mutable degiskenler yerine tek bir thread-safe
AppState ornegi kullanilir.
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

DEFAULT_PAPER_MODE: bool = True
SCAN_INTERVAL_SEC: int = 30
MIN_VOLUME_24HR: float = 10_000.0

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
BINANCE_BTC_SPOT_URL = "https://api.binance.com/api/v3/ticker/price"
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 500
HTTP_RETRIES = 3
HTTP_BACKOFF_BASE = 0.5  # saniye — exponential backoff tabanı


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.setdefault(
        "User-Agent",
        "polymarket-scanner/1.0 (+https://polymarket.com)",
    )
    return session


# ── Saf yardımcılar (durumsuz) ───────────────────────────────────────────
def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_with_retry(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    *,
    retries: int = HTTP_RETRIES,
) -> requests.Response:
    """Geçici hatalarda (429/5xx/ağ) exponential backoff ile yeniden dene."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                r.raise_for_status()
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = e
            if attempt == retries - 1:
                break
            wait = HTTP_BACKOFF_BASE * (2 ** attempt)
            logging.warning(
                "HTTP retry %d/%d (%s) — %.1fs bekle", attempt + 1, retries, e, wait,
            )
            time.sleep(wait)
    assert last_err is not None
    raise last_err


def fetch_btc_spot_usdt(session: requests.Session) -> float | None:
    r = _get_with_retry(session, BINANCE_BTC_SPOT_URL, {"symbol": "BTCUSDT"})
    return float(r.json()["price"])


def fetch_active_markets(session: requests.Session) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = _get_with_retry(
            session,
            GAMMA_MARKETS_URL,
            {
                "active": "true",
                "closed": "false",
                "limit": PAGE_LIMIT,
                "offset": offset,
            },
        )
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


# ── Uygulama durumu (thread-safe, kapsüllenmiş) ──────────────────────────
class AppState:
    """Tarayıcı önbelleği + paper trade defteri + tarayıcı thread'i.

    Eski dağınık global'lerin (PAPER_MODE, _cache, _paper_trades, thread)
    yerini alan tek, kilitli durum nesnesi.
    """

    def __init__(self, paper_mode: bool = DEFAULT_PAPER_MODE) -> None:
        self.paper_mode: bool = paper_mode
        self._cache_lock = threading.Lock()
        self._cache: dict[str, Any] = {
            "markets": [],
            "btc_price": None,
            "total_active": 0,
            "filtered_count": 0,
            "error": None,
            "updated_at": None,
        }
        self._trades_lock = threading.Lock()
        self._paper_trades: list[dict[str, Any]] = []
        self._stop_event = threading.Event()
        self._bg_thread: threading.Thread | None = None

    # ── Snapshot / önbellek ──
    def update_snapshot(
        self, btc: float | None, rows: list[dict[str, Any]], total_active: int,
    ) -> None:
        with self._cache_lock:
            self._cache.update(
                btc_price=btc,
                markets=rows,
                total_active=total_active,
                filtered_count=len(rows),
                error=None,
                updated_at=_utcnow(),
            )

    def set_error(self, err: str) -> None:
        with self._cache_lock:
            self._cache["error"] = err
            self._cache["updated_at"] = _utcnow()

    def snapshot(self) -> dict[str, Any]:
        with self._cache_lock:
            snap = dict(self._cache)
        snap["markets"] = list(snap["markets"])
        return snap

    def find_market(self, market_id: str) -> dict[str, Any] | None:
        with self._cache_lock:
            return next(
                (m for m in self._cache["markets"] if m["id"] == market_id), None,
            )

    def current_price(self, market_id: str, side: str) -> float | None:
        """Verilen taraf için cache'teki güncel mark fiyatı."""
        with self._cache_lock:
            for m in self._cache["markets"]:
                if m["id"] == market_id:
                    if side == "YES":
                        return m.get("bid")  # YES çıkış fiyatı = bid
                    ask = m.get("ask")
                    return (1.0 - ask) if ask is not None else None
        return None

    # ── Paper trade defteri ──
    def add_trade(self, trade: dict[str, Any]) -> None:
        with self._trades_lock:
            self._paper_trades.append(trade)

    def list_trades(self) -> list[dict[str, Any]]:
        with self._trades_lock:
            return list(self._paper_trades)

    def remove_trade(self, trade_id: str) -> bool:
        with self._trades_lock:
            idx = next(
                (i for i, t in enumerate(self._paper_trades) if t["id"] == trade_id),
                None,
            )
            if idx is None:
                return False
            self._paper_trades.pop(idx)
            return True

    # ── Tarayıcı yaşam döngüsü ──
    def start_scanner(self) -> None:
        self._stop_event.clear()
        self._bg_thread = threading.Thread(target=self._background_loop, daemon=True)
        self._bg_thread.start()

    def stop_scanner(self) -> None:
        self._stop_event.set()
        if self._bg_thread:
            self._bg_thread.join(timeout=5)

    def scanner_alive(self) -> bool:
        return self._bg_thread is not None and self._bg_thread.is_alive()

    def _background_loop(self) -> None:
        session = _make_session()
        while not self._stop_event.is_set():
            refresh_snapshot(session, self)
            if self._stop_event.wait(SCAN_INTERVAL_SEC):
                break


def refresh_snapshot(session: requests.Session, state: AppState) -> None:
    try:
        btc = fetch_btc_spot_usdt(session)
        raw = fetch_active_markets(session)
        rows = build_rows(raw)
        state.update_snapshot(btc, rows, len(raw))
        logging.info(
            "Snapshot | BTC=%s | aktif=%d | filtre=%d | paper_mode=%s",
            f"{btc:,.2f}" if btc is not None else "n/a",
            len(raw),
            len(rows),
            state.paper_mode,
        )
    except requests.RequestException as e:
        logging.exception("HTTP hatasi: %s", e)
        state.set_error(str(e))
    except Exception as e:  # noqa: BLE001 — döngü canlı kalmalı
        logging.exception("Snapshot hatasi: %s", e)
        state.set_error(str(e))


# Tek uygulama durumu örneği
state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    state.start_scanner()
    yield
    state.stop_scanner()


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


@app.get("/health")
def health() -> dict[str, Any]:
    """Servis sağlığı: arka plan tarayıcı çalışıyor mu, son güncelleme ne zaman."""
    snap = state.snapshot()
    alive = state.scanner_alive()
    healthy = alive and snap["error"] is None and snap["updated_at"] is not None
    return {
        "status": "ok" if healthy else "degraded",
        "scanner_alive": alive,
        "updated_at": snap["updated_at"],
        "error": snap["error"],
        "paper_mode": state.paper_mode,
    }


@app.get("/btc", response_model=BtcResponse)
def get_btc() -> BtcResponse:
    snap = state.snapshot()
    return BtcResponse(
        price=snap["btc_price"],
        updated_at=snap["updated_at"],
        error=snap["error"],
    )


@app.get("/markets", response_model=MarketsResponse)
def get_markets() -> MarketsResponse:
    snap = state.snapshot()
    rows = [MarketRow(**r) for r in snap["markets"]]
    return MarketsResponse(
        markets=rows,
        paper_mode=state.paper_mode,
        total_active=snap["total_active"],
        filtered_count=snap["filtered_count"],
        min_volume_24hr=MIN_VOLUME_24HR,
        updated_at=snap["updated_at"],
        error=snap["error"],
    )


@app.patch("/settings", response_model=SettingsResponse)
def patch_settings(body: SettingsBody) -> SettingsResponse:
    state.paper_mode = body.paper_mode
    logging.info("paper_mode -> %s", state.paper_mode)
    return SettingsResponse(paper_mode=state.paper_mode)


@app.post("/trade", response_model=TradeRow)
def post_trade(body: TradeRequest) -> TradeRow:
    side = body.side.upper()
    if side not in ("YES", "NO"):
        raise HTTPException(status_code=400, detail="side must be YES or NO")

    market = state.find_market(body.market_id)
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
        "opened_at": _utcnow(),
    }
    state.add_trade(trade)

    logging.info(
        "PAPER TRADE | %s | %s | %.2f USDC @ %.4f | shares=%.4f",
        trade["question"][:50],
        side,
        body.amount_usdc,
        entry,
        shares,
    )

    current_price = state.current_price(body.market_id, side)
    pnl = (shares * current_price - body.amount_usdc) if current_price is not None else None
    pnl_pct = ((current_price / entry) - 1) * 100 if current_price is not None else None
    return TradeRow(**trade, current_price=current_price, pnl=pnl, pnl_pct=pnl_pct)


@app.get("/trades", response_model=TradesResponse)
def get_trades() -> TradesResponse:
    snapshot = state.list_trades()

    rows: list[TradeRow] = []
    total_pnl = 0.0
    for t in snapshot:
        current_price = state.current_price(t["market_id"], t["side"])
        pnl = (t["shares"] * current_price - t["amount_usdc"]) if current_price is not None else None
        pnl_pct = ((current_price / t["entry_price"]) - 1) * 100 if current_price is not None else None
        rows.append(TradeRow(**t, current_price=current_price, pnl=pnl, pnl_pct=pnl_pct))
        if pnl is not None:
            total_pnl += pnl

    return TradesResponse(trades=rows, total_pnl=total_pnl)


@app.delete("/trades/{trade_id}")
def delete_trade(trade_id: str) -> dict[str, str]:
    if not state.remove_trade(trade_id):
        raise HTTPException(status_code=404, detail="trade not found")
    return {"deleted": trade_id}


def run_scan(session: requests.Session, state: AppState) -> None:
    """CLI dongusu icin (eski davranis)."""
    refresh_snapshot(session, state)
    if state.paper_mode:
        logging.info("paper_mode: islem acilmadi.")


def main_cli() -> None:
    setup_logging()
    logging.info(
        "Basladi (CLI) | paper_mode=%s | dongu=%ds | min_vol24h=%.0f",
        state.paper_mode,
        SCAN_INTERVAL_SEC,
        MIN_VOLUME_24HR,
    )
    session = _make_session()
    while True:
        try:
            run_scan(session, state)
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
