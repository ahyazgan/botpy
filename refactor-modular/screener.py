"""
Market filtreleme ve arbitraj tespiti.

Pure functions — dış bağımlılık yok, test edilmesi kolay.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from config import MIN_PROFIT, MIN_VOLUME_24H
from fetcher import fetch_clob_best_prices
from models import ArbOpportunity, Market, to_float


def parse_market(raw: dict[str, Any]) -> Market | None:
    """Ham Gamma API verisini Market dataclass'ına çevirir. Geçersizse None."""
    vol = to_float(raw.get("volume24hr")) or 0.0
    if vol < MIN_VOLUME_24H:
        return None

    tokens: list[dict[str, Any]] = raw.get("tokens") or []
    yes_tok = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), None)
    no_tok  = next((t for t in tokens if str(t.get("outcome", "")).upper() == "NO"), None)

    yes_token_id = (yes_tok or {}).get("token_id", "")
    no_token_id  = (no_tok or {}).get("token_id", "")

    yes_bid = to_float(raw.get("bestBid"))
    yes_ask = to_float(raw.get("bestAsk"))

    # NO fiyatları Gamma'da yoktur; YES fiyatlarından türetilir
    no_bid = (1.0 - yes_ask) if yes_ask is not None else None
    no_ask = (1.0 - yes_bid) if yes_bid is not None else None

    bid = yes_bid
    ask = yes_ask
    spread = (ask - bid) if (bid is not None and ask is not None) else to_float(raw.get("spread"))

    return Market(
        id=str(raw.get("id", "")),
        question=(raw.get("question") or raw.get("slug") or "?").strip(),
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        volume24h=vol,
        spread=spread,
    )


def quick_screen(market: Market) -> bool:
    """
    Gamma fiyatlarıyla hızlı ön eleme.
    CLOB çağırmadan olası fırsatları filtreler; yanlış pozitif kabul edilebilir.
    """
    ya, na = market.yes_ask, market.no_ask
    yb, nb = market.yes_bid, market.no_bid

    if ya is not None and na is not None and (ya + na) < (1.0 - MIN_PROFIT / 2):
        return True
    if yb is not None and nb is not None and (yb + nb) > (1.0 + MIN_PROFIT / 2):
        return True
    return False


async def verify_opportunity(
    session: aiohttp.ClientSession,
    market: Market,
) -> ArbOpportunity | None:
    """CLOB orderbook'unu sorgulayarak gerçek arbitraj fırsatını doğrular."""
    (yes_bid, yes_ask), (no_bid, no_ask) = await asyncio.gather(
        fetch_clob_best_prices(session, market.yes_token_id),
        fetch_clob_best_prices(session, market.no_token_id),
    )

    if yes_ask is not None and no_ask is not None:
        total_cost = yes_ask + no_ask
        if total_cost < (1.0 - MIN_PROFIT):
            return ArbOpportunity(market, "buy", (1.0 - total_cost) * 100, yes_ask, no_ask)

    if yes_bid is not None and no_bid is not None:
        total_recv = yes_bid + no_bid
        if total_recv > (1.0 + MIN_PROFIT):
            return ArbOpportunity(market, "sell", (total_recv - 1.0) * 100, yes_bid, no_bid)

    return None


def build_market_rows(raw_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Scanner cache'i için ham market listesini satır listesine çevirir.
    Hacime göre azalan sırada döner.
    """
    rows = []
    for raw in raw_list:
        vol = to_float(raw.get("volume24hr")) or 0.0
        if vol <= MIN_VOLUME_24H:
            continue
        bid = to_float(raw.get("bestBid"))
        ask = to_float(raw.get("bestAsk"))
        spread = (ask - bid) if (bid is not None and ask is not None) else to_float(raw.get("spread"))
        rows.append({
            "id": str(raw.get("id", "")),
            "question": (raw.get("question") or raw.get("slug") or "?").strip(),
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "volume24h": vol,
        })
    rows.sort(key=lambda r: r["volume24h"], reverse=True)
    return rows
