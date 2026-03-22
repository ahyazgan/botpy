"""
Tüm sabit değerler ve ortam değişkenleri tek yerde.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# ── API URL'leri ─────────────────────────────────────────────────────────
GAMMA_URL       = "https://gamma-api.polymarket.com/markets"
CLOB_HOST       = "https://clob.polymarket.com"
BINANCE_BTC_URL = "https://api.binance.com/api/v3/ticker/price"

# ── Tarama ayarları ──────────────────────────────────────────────────────
SCAN_INTERVAL_SEC: int   = 30
MIN_VOLUME_24H:    float = 10_000.0
MIN_PROFIT:        float = 0.02      # %2 net kâr eşiği
MAX_TRADE_USDC:    float = 50.0
PAGE_LIMIT:        int   = 500
REQUEST_TIMEOUT:   int   = 30

# ── Paper mode ───────────────────────────────────────────────────────────
PAPER_MODE: bool = True

# ── Polymarket CLOB kimlik bilgileri (gerçek mod için) ───────────────────
PRIVATE_KEY     = os.environ.get("PRIVATE_KEY", "")
FUNDER_ADDRESS  = os.environ.get("FUNDER_ADDRESS", "")
POLY_API_KEY    = os.environ.get("POLY_API_KEY", "")
POLY_SECRET     = os.environ.get("POLY_SECRET", "")
POLY_PASSPHRASE = os.environ.get("POLY_PASSPHRASE", "")

# ── CORS izin verilen originler ──────────────────────────────────────────
CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
