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

from metrics import compute_stats
from risk import RiskLimits, RiskManager
from storage import Store
from strategy import (
    entry_price,
    evaluate_signal,
    should_close,
    to_float,
)

DEFAULT_PAPER_MODE: bool = True
SCAN_INTERVAL_SEC: int = 30
MIN_VOLUME_24HR: float = 10_000.0

# Otomatik strateji pozisyon büyüklüğü (eşikler strategy.py'de)
AUTO_TRADE_AMOUNT: float = 10.0   # her otomatik işlem için USDC

# Risk yönetimi (paper)
PAPER_BANKROLL: float = 1_000.0   # nominal başlangıç sermayesi

# Geçmiş kaydı (gerçek-veri backtest)
HISTORY_MAX_MARKETS: int = 150    # her taramada kaydedilecek en fazla market
HISTORY_MAX_ROWS: int = 500_000   # snapshot tablosu üst sınırı (prune)

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


def _utctoday() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.setdefault(
        "User-Agent",
        "polymarket-scanner/1.0 (+https://polymarket.com)",
    )
    return session


# ── Saf yardımcılar (durumsuz) ───────────────────────────────────────────
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


# ── Trade yardımcıları (saf) ─────────────────────────────────────────────
def new_trade(market: dict[str, Any], side: str, amount: float, entry: float) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "market_id": str(market.get("id", "")),
        "question": market.get("question", "?"),
        "side": side,
        "amount_usdc": amount,
        "entry_price": entry,
        "shares": amount / entry,
        "opened_at": _utcnow(),
    }


# ── Otomatik strateji adımı (saf karar strategy.py'de) ───────────────────
def auto_trade_step(
    state: AppState, rows: list[dict[str, Any]], *, amount: float = AUTO_TRADE_AMOUNT,
) -> int:
    """Sinyal veren ve henüz açık pozisyonu olmayan marketlerde paper işlem aç.

    Market başına en fazla bir açık otomatik pozisyon. Açılan işlem sayısını döner.
    """
    open_trades = state.list_trades()
    open_market_ids = {t["market_id"] for t in open_trades}
    exposure = sum(to_float(t["amount_usdc"]) or 0.0 for t in open_trades)
    open_count = len(open_trades)
    today = _utctoday()
    opened = 0
    for row in rows:
        mid = str(row.get("id", ""))
        if not mid or mid in open_market_ids:
            continue
        side = evaluate_signal(row)
        if side is None:
            continue
        decision = state.risk.check_open(amount, open_count, exposure, today=today)
        if not decision.allowed:
            logging.info("Risk: yeni işlem engellendi (%s)", decision.reason)
            break  # limit/halt → bu turda daha fazla açma
        entry = entry_price(row, side)
        if entry is None or entry <= 0:
            continue
        state.add_trade(new_trade(row, side, amount, entry))
        open_market_ids.add(mid)
        exposure += amount
        open_count += 1
        opened += 1
    return opened


def auto_close_step(state: AppState) -> int:
    """Açık pozisyonları take-profit/stop-loss'a göre kapat. Kapanan sayısı döner."""
    closed = 0
    for trade in state.list_trades():
        current = state.current_price(trade["market_id"], trade["side"])
        reason = should_close(trade["entry_price"], current)
        if reason is None:
            continue
        # current None değil (should_close None döndürürdü)
        row = state.close_trade(trade["id"], float(current), reason)
        if row is None:
            continue
        state.risk.on_close(row["pnl"], _utctoday())
        logging.info(
            "AUTO CLOSE | %s | %s | pnl=%.2f USDC%s",
            trade["question"][:40], reason, row["pnl"],
            " | RISK HALT" if state.risk.halted else "",
        )
        closed += 1
    return closed


# ── Uygulama durumu (thread-safe, kapsüllenmiş) ──────────────────────────
class AppState:
    """Tarayıcı önbelleği + paper trade defteri + tarayıcı thread'i.

    Eski dağınık global'lerin (PAPER_MODE, _cache, _paper_trades, thread)
    yerini alan tek, kilitli durum nesnesi.
    """

    def __init__(
        self, paper_mode: bool = DEFAULT_PAPER_MODE, store: Store | None = None,
    ) -> None:
        self.paper_mode: bool = paper_mode
        self.auto_trade: bool = False
        self.record_history: bool = False
        self.store: Store = store if store is not None else Store()
        # Risk yöneticisi — realize PnL DB'den seed edilir (restart'a dayanıklı)
        self.risk = RiskManager(RiskLimits(), starting_equity=PAPER_BANKROLL)
        self.risk.realized_pnl = self.store.realized_pnl_total()
        self.risk.peak_equity = max(self.risk.starting_equity, self.risk.equity)
        self._cache_lock = threading.Lock()
        self._cache: dict[str, Any] = {
            "markets": [],
            "btc_price": None,
            "total_active": 0,
            "filtered_count": 0,
            "error": None,
            "updated_at": None,
        }
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

    # ── Paper trade defteri (SQLite ile kalıcı) ──
    def add_trade(self, trade: dict[str, Any]) -> None:
        self.store.add_trade(trade)

    def list_trades(self) -> list[dict[str, Any]]:
        return self.store.list_trades()

    def remove_trade(self, trade_id: str) -> bool:
        return self.store.remove_trade(trade_id)

    def close_trade(
        self, trade_id: str, close_price: float, reason: str,
    ) -> dict[str, Any] | None:
        """Açık pozisyonu güncel fiyattan kapat (atomik). Kapanan satırı döner."""
        return self.store.close_trade(trade_id, close_price, reason)

    def list_closed_trades(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_closed_trades(limit)

    def realized_pnl_total(self) -> float:
        return self.store.realized_pnl_total()

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
        if state.record_history and rows:
            state.store.record_snapshots(_utcnow(), rows[:HISTORY_MAX_MARKETS])
            state.store.prune_snapshots(HISTORY_MAX_ROWS)
        if state.auto_trade:
            # Önce mevcut pozisyonları TP/SL ile kapat, sonra yeni sinyalleri aç
            closed = auto_close_step(state)
            if closed:
                logging.info("Auto-close: %d pozisyon kapatıldı (TP/SL)", closed)
            opened = auto_trade_step(state, rows)
            if opened:
                logging.info("Auto-trade: %d yeni paper işlem açıldı", opened)
        logging.info(
            "Snapshot | BTC=%s | aktif=%d | filtre=%d | paper_mode=%s | auto=%s",
            f"{btc:,.2f}" if btc is not None else "n/a",
            len(raw),
            len(rows),
            state.paper_mode,
            state.auto_trade,
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
    paper_mode: bool | None = None
    auto_trade: bool | None = None
    reset_halt: bool | None = None
    record_history: bool | None = None


class SettingsResponse(BaseModel):
    paper_mode: bool
    auto_trade: bool
    risk_halted: bool
    record_history: bool


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


class ClosedTradeRow(BaseModel):
    id: str
    market_id: str
    question: str
    side: str
    amount_usdc: float
    entry_price: float
    shares: float
    opened_at: str
    closed_at: str
    close_price: float
    pnl: float
    reason: str


class ClosedTradesResponse(BaseModel):
    trades: list[ClosedTradeRow]
    realized_pnl: float


class ArbOppRow(BaseModel):
    id: int
    ts: str
    market_id: str
    question: str
    direction: str
    profit_pct: float
    yes_price: float
    no_price: float


class ArbResponse(BaseModel):
    opportunities: list[ArbOppRow]
    count: int


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
        "auto_trade": state.auto_trade,
        "risk_halted": state.risk.halted,
    }


@app.get("/risk")
def get_risk() -> dict[str, Any]:
    """Risk durumu: equity, drawdown, halt, limitler."""
    return state.risk.snapshot()


@app.get("/arb", response_model=ArbResponse)
def get_arb(limit: int = 100) -> ArbResponse:
    """Arb radarı: arb_bot tarafından kaydedilen son fırsatlar (read-only)."""
    limit = max(1, min(limit, 500))
    rows = state.store.list_opportunities(limit)
    return ArbResponse(
        opportunities=[ArbOppRow(**o) for o in rows],
        count=len(rows),
    )


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
    if body.paper_mode is not None:
        state.paper_mode = body.paper_mode
        logging.info("paper_mode -> %s", state.paper_mode)
    if body.auto_trade is not None:
        state.auto_trade = body.auto_trade
        logging.info("auto_trade -> %s", state.auto_trade)
    if body.reset_halt:
        state.risk.reset_halt()
        logging.info("risk halt sıfırlandı")
    if body.record_history is not None:
        state.record_history = body.record_history
        logging.info("record_history -> %s", state.record_history)
    return SettingsResponse(
        paper_mode=state.paper_mode,
        auto_trade=state.auto_trade,
        risk_halted=state.risk.halted,
        record_history=state.record_history,
    )


@app.post("/trade", response_model=TradeRow)
def post_trade(body: TradeRequest) -> TradeRow:
    side = body.side.upper()
    if side not in ("YES", "NO"):
        raise HTTPException(status_code=400, detail="side must be YES or NO")

    market = state.find_market(body.market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    entry = entry_price(market, side)
    if entry is None or entry <= 0:
        raise HTTPException(status_code=422, detail="entry price unavailable")

    trade = new_trade(market, side, body.amount_usdc, entry)
    state.add_trade(trade)

    logging.info(
        "PAPER TRADE | %s | %s | %.2f USDC @ %.4f | shares=%.4f",
        trade["question"][:50],
        side,
        body.amount_usdc,
        entry,
        trade["shares"],
    )

    current_price = state.current_price(body.market_id, side)
    pnl = (
        trade["shares"] * current_price - body.amount_usdc
        if current_price is not None else None
    )
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


@app.get("/trades/closed", response_model=ClosedTradesResponse)
def get_closed_trades(limit: int = 200) -> ClosedTradesResponse:
    """Kapanan (realize) paper işlemler ve toplam gerçekleşen PnL."""
    limit = max(1, min(limit, 1000))
    rows = [ClosedTradeRow(**t) for t in state.list_closed_trades(limit)]
    return ClosedTradesResponse(trades=rows, realized_pnl=state.realized_pnl_total())


@app.get("/pnl/curve")
def get_pnl_curve(limit: int = 1000) -> dict[str, Any]:
    """Kapanan işlemlerden kümülatif (equity) PnL eğrisi."""
    limit = max(1, min(limit, 5000))
    points = state.store.equity_curve(limit)
    return {"points": points, "realized_pnl": state.realized_pnl_total()}


@app.get("/pnl/stats")
def get_pnl_stats() -> dict[str, Any]:
    """Kapanan işlemlerden performans metrikleri (win-rate, PF, Sharpe, max DD)."""
    pnls = [t["pnl"] for t in state.store.list_closed_trades(limit=5000)]
    return compute_stats(pnls)


@app.get("/audit")
def get_audit(limit: int = 200) -> dict[str, Any]:
    """Audit log (arb_bot emir/olay kayıtları) + bekleyen emir niyetleri."""
    limit = max(1, min(limit, 1000))
    return {
        "events": state.store.list_audit(limit),
        "open_intents": state.store.list_open_intents(),
    }


@app.get("/history")
def get_history_info() -> dict[str, Any]:
    """Geçmiş snapshot kaydı durumu (gerçek-veri backtest için)."""
    return {
        "record_history": state.record_history,
        "snapshots": state.store.count_snapshots(),
    }


@app.get("/backtest")
def run_backtest_endpoint(limit_per_market: int = 1000, amount: float = 10.0) -> dict[str, Any]:
    """Kaydedilmiş gerçek geçmiş veri üzerinde stratejiyi backtest et."""
    from backtest import run_backtest  # lazy: döngüsel import'tan kaçın

    series = state.store.history_series(max(1, min(limit_per_market, 5000)))
    if not series:
        return {
            "error": "geçmiş veri yok — /settings record_history=true ile kaydı açın",
            "markets": 0, "trade_count": 0, "stats": compute_stats([]),
        }
    res = run_backtest(series, amount=amount)
    return {
        "markets": len(series),
        "trade_count": len(res["trades"]),
        "stats": res["stats"],
    }


@app.get("/optimize")
def run_optimize_endpoint(
    objective: str = "total_pnl", min_trades: int = 3, top: int = 10,
) -> dict[str, Any]:
    """Geçmiş veride TP/SL ızgarasını tarayıp en iyi parametreleri bul."""
    from optimize import DEFAULT_GRID, grid_search  # lazy

    series = state.store.history_series(5000)
    if not series:
        return {"error": "geçmiş veri yok — record_history açın", "results": []}
    results = grid_search(
        series, DEFAULT_GRID,
        objective=objective, min_trades=max(1, min_trades), top=max(1, min(top, 50)),
    )
    return {"objective": objective, "markets": len(series), "results": results}


@app.get("/walkforward")
def run_walkforward_endpoint(
    train_frac: float = 0.7, objective: str = "total_pnl", min_trades: int = 3,
) -> dict[str, Any]:
    """Walk-forward doğrulama: in-sample optimize, out-of-sample test."""
    from optimize import DEFAULT_GRID  # lazy
    from walkforward import walk_forward

    series = state.store.history_series(5000)
    if not series:
        return {"ok": False, "reason": "geçmiş veri yok — record_history açın"}
    frac = min(0.9, max(0.1, train_frac))
    return walk_forward(
        series, DEFAULT_GRID,
        train_frac=frac, objective=objective, min_trades=max(1, min_trades),
    )


@app.post("/trades/{trade_id}/close", response_model=ClosedTradeRow)
def close_trade_endpoint(trade_id: str) -> ClosedTradeRow:
    """Açık pozisyonu güncel fiyattan kapat (realize PnL ile kaydet)."""
    trade = next((t for t in state.list_trades() if t["id"] == trade_id), None)
    if trade is None:
        raise HTTPException(status_code=404, detail="trade not found")
    current = state.current_price(trade["market_id"], trade["side"])
    if current is None:
        raise HTTPException(status_code=422, detail="current price unavailable")
    row = state.close_trade(trade_id, float(current), "manual")
    if row is None:
        raise HTTPException(status_code=404, detail="trade not found")
    state.risk.on_close(row["pnl"], _utctoday())
    logging.info(
        "MANUAL CLOSE | %s | %s | pnl=%.2f USDC",
        trade["question"][:40], trade["side"], row["pnl"],
    )
    return ClosedTradeRow(**row)


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
