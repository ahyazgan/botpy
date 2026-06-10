"""
Veri modelleri: API response'ları için Pydantic, iç mantık için dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


# ── İç mantık veri yapıları ──────────────────────────────────────────────

@dataclass
class Market:
    id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    volume24h: float
    # Gamma'dan gelen ham spread (CLOB yoksa yedek)
    spread: float | None = None


@dataclass
class ArbOpportunity:
    market: Market
    direction: str       # "buy" | "sell"
    profit_pct: float
    yes_price: float
    no_price: float


# ── API response modelleri ───────────────────────────────────────────────

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
    min_volume_24hr: float
    updated_at: str | None = None
    error: str | None = None


class BtcResponse(BaseModel):
    price: float | None
    symbol: str = "BTCUSDT"
    updated_at: str | None = None
    error: str | None = None


class SettingsBody(BaseModel):
    paper_mode: bool


class SettingsResponse(BaseModel):
    paper_mode: bool


class TradeRequest(BaseModel):
    market_id: str
    side: str            # "YES" | "NO"
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


# ── Yardımcı fonksiyon ───────────────────────────────────────────────────

def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
