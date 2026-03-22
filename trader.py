"""
Emir yönetimi.

PaperTrader  — in-memory simülasyon.
LiveTrader   — Polymarket CLOB ile gerçek emirler (async).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from models import ArbOpportunity, TradeRow, TradesResponse

log = logging.getLogger(__name__)


# ── Paper Trader ─────────────────────────────────────────────────────────

class PaperTrader:
    """Thread-safe in-memory paper trade deposu."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trades: list[dict[str, Any]] = []

    def open(
        self,
        market_id: str,
        question: str,
        side: str,
        amount_usdc: float,
        entry_price: float,
    ) -> dict[str, Any]:
        shares = amount_usdc / entry_price
        trade: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "market_id": market_id,
            "question": question,
            "side": side,
            "amount_usdc": amount_usdc,
            "entry_price": entry_price,
            "shares": shares,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._trades.append(trade)
        log.info(
            "PAPER TRADE | %s | %s | %.2f USDC @ %.4f | shares=%.4f",
            question[:50], side, amount_usdc, entry_price, shares,
        )
        return trade

    def delete(self, trade_id: str) -> bool:
        with self._lock:
            idx = next((i for i, t in enumerate(self._trades) if t["id"] == trade_id), None)
            if idx is None:
                return False
            self._trades.pop(idx)
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._trades)

    def build_response(self, current_price_fn) -> TradesResponse:  # type: ignore[type-arg]
        """current_price_fn(market_id, side) -> float | None"""
        rows: list[TradeRow] = []
        total_pnl = 0.0
        for t in self.snapshot():
            cp = current_price_fn(t["market_id"], t["side"])
            pnl = (t["shares"] * cp - t["amount_usdc"]) if cp is not None else None
            pnl_pct = ((cp / t["entry_price"]) - 1) * 100 if cp is not None else None
            rows.append(TradeRow(**t, current_price=cp, pnl=pnl, pnl_pct=pnl_pct))
            if pnl is not None:
                total_pnl += pnl
        return TradesResponse(trades=rows, total_pnl=total_pnl)


# ── Live Trader ──────────────────────────────────────────────────────────

class LiveTrader:
    """Polymarket CLOB ile gerçek emir gönderimi."""

    def __init__(self) -> None:
        self._client = None  # geç başlatma

    def _get_client(self):  # type: ignore[return]
        if self._client is None:
            from config import FUNDER_ADDRESS, POLY_API_KEY, POLY_PASSPHRASE, POLY_SECRET, PRIVATE_KEY
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            from py_clob_client.constants import POLYGON
            from config import CLOB_HOST

            creds = ApiCreds(
                api_key=POLY_API_KEY,
                api_secret=POLY_SECRET,
                api_passphrase=POLY_PASSPHRASE,
            )
            self._client = ClobClient(
                host=CLOB_HOST,
                key=PRIVATE_KEY,
                chain_id=POLYGON,
                creds=creds,
                funder=FUNDER_ADDRESS,
            )
        return self._client

    def _place_order_sync(self, token_id: str, side: str, price: float, size: float) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs, OrderType

        client = self._get_client()
        signed = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=side))
        return client.post_order(signed, OrderType.FOK)

    async def execute_arb(self, opp: ArbOpportunity, max_usdc: float) -> None:
        from config import MAX_TRADE_USDC
        m = opp.market
        trade_usdc = min(max_usdc, MAX_TRADE_USDC)

        yes_side, no_side = ("BUY", "BUY") if opp.direction == "buy" else ("SELL", "SELL")
        yes_size = round(trade_usdc / opp.yes_price, 2)
        no_size  = round(trade_usdc / opp.no_price, 2)

        log.info(
            "ARB EXECUTE | %s | dir=%s | kâr=%.2f%% | yes=%.4f no=%.4f",
            m.question[:55], opp.direction, opp.profit_pct, opp.yes_price, opp.no_price,
        )

        loop = asyncio.get_event_loop()
        yes_res, no_res = await asyncio.gather(
            loop.run_in_executor(None, self._place_order_sync, m.yes_token_id, yes_side, opp.yes_price, yes_size),
            loop.run_in_executor(None, self._place_order_sync, m.no_token_id,  no_side,  opp.no_price,  no_size),
            return_exceptions=True,
        )
        log.info("YES sonuç: %s", yes_res)
        log.info("NO  sonuç: %s", no_res)
