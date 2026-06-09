"""
Polymarket async arbitraj botu.

İki mod:
  • Sadece bildirim (varsayılan): fırsat bulununca masaüstü bildirimi atar,
    trade'i sen elinle yaparsın. CLOB API anahtarı GEREKMEZ.
  • Otomatik emir (--execute): fırsatta YES+NO emirlerini otomatik gönderir.
    .env'deki CLOB kimlik bilgileri GEREKİR.

Strateji:
  YES_ask + NO_ask < (1 - MIN_PROFIT)  → ikisini AL  (buy arb)
  YES_bid + NO_bid > (1 + MIN_PROFIT)  → ikisini SAT (sell arb)

Hız teknikleri:
  - asyncio + aiohttp ile paralel market tarama
  - İki emri aynı anda gönder (asyncio.gather)
  - FOK (Fill-or-Kill): dolmayan emir anında iptal
  - Kalıcı HTTP bağlantı havuzu (TCPConnector)
  - CLOB orderbook ile fiyat doğrulaması
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import webbrowser
from dataclasses import dataclass
from typing import Any

import aiohttp
from dotenv import load_dotenv
from winotify import Notification, audio

load_dotenv()

# ── Ayarlar ─────────────────────────────────────────────────────────────
CLOB_HOST      = "https://clob.polymarket.com"
GAMMA_URL      = "https://gamma-api.polymarket.com/markets"

SCAN_INTERVAL  = 5        # saniye — her kaç saniyede tarasın
MIN_VOLUME_24H = 50_000   # USDC — düşük hacimli marketleri atla
MIN_PROFIT     = 0.02     # %2 minimum net kâr (gas + slippage payı)
MAX_TRADE_USDC = 50.0     # her leg için maksimum USDC
PAGE_LIMIT     = 500

# Aynı fırsat için tekrar bildirim atmadan önce beklenecek süre (saniye).
# Fırsat 5 sn'de bir taramada tekrar tekrar görüneceği için spam'i önler.
NOTIFY_COOLDOWN = 300     # saniye — aynı market+yön için bildirim aralığı

APP_ID = "Polymarket Arb"

# ── Loglama ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── Veri yapıları ────────────────────────────────────────────────────────
@dataclass
class Market:
    id: str
    question: str
    slug: str               # polymarket.com URL için
    yes_token_id: str
    no_token_id: str
    yes_bid: float | None   # YES için en iyi alış (sat fiyatı)
    yes_ask: float | None   # YES için en iyi satış (al fiyatı)
    no_bid: float | None    # NO için en iyi alış
    no_ask: float | None    # NO için en iyi satış
    volume24h: float

    @property
    def url(self) -> str:
        return f"https://polymarket.com/market/{self.slug}" if self.slug else "https://polymarket.com"


@dataclass
class ArbOpportunity:
    market: Market
    direction: str    # "buy" veya "sell"
    profit_pct: float
    yes_price: float
    no_price: float


# ── CLOB Client (yalnızca --execute modunda) ─────────────────────────────
def build_clob_client() -> Any:
    """CLOB client'ı kur. Sadece otomatik emir modunda çağrılır;
    import ve .env anahtarları yalnızca burada gerekir."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from py_clob_client.constants import POLYGON

    try:
        private_key     = os.environ["PRIVATE_KEY"]
        funder_address  = os.environ["FUNDER_ADDRESS"]
        poly_api_key    = os.environ["POLY_API_KEY"]
        poly_secret     = os.environ["POLY_SECRET"]
        poly_passphrase = os.environ["POLY_PASSPHRASE"]
    except KeyError as e:
        log.error(
            "Otomatik emir modu için %s .env dosyasında tanımlı olmalı. "
            "Sadece bildirim için --execute'suz çalıştır.", e,
        )
        sys.exit(1)

    creds = ApiCreds(
        api_key=poly_api_key,
        api_secret=poly_secret,
        api_passphrase=poly_passphrase,
    )
    return ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=POLYGON,
        creds=creds,
        funder=funder_address,
    )


# ── Market tarayıcı (Gamma API) ──────────────────────────────────────────
async def fetch_markets(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while True:
        async with session.get(
            GAMMA_URL,
            params={
                "active": "true",
                "closed": "false",
                "limit": PAGE_LIMIT,
                "offset": offset,
            },
        ) as r:
            r.raise_for_status()
            batch: list[dict[str, Any]] = await r.json()
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return markets


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_list(v: Any) -> list[Any]:
    """Gamma alanları liste ya da JSON-string olabilir; ikisini de listeye çevir."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def parse_market(raw: dict[str, Any]) -> Market | None:
    vol = _f(raw.get("volume24hr")) or 0.0
    if vol < MIN_VOLUME_24H:
        return None

    # Gamma API binary marketlerde `outcomes` (["Yes","No"]) ve aynı sıradaki
    # `clobTokenIds` döndürür — eski `tokens` alanı artık null geliyor.
    outcomes = [str(o).upper() for o in _as_list(raw.get("outcomes"))]
    token_ids = _as_list(raw.get("clobTokenIds"))
    if len(outcomes) != len(token_ids) or "YES" not in outcomes or "NO" not in outcomes:
        return None

    yes_token_id = str(token_ids[outcomes.index("YES")])
    no_token_id  = str(token_ids[outcomes.index("NO")])
    if not yes_token_id or not no_token_id:
        return None

    # Gamma API: bestBid/bestAsk YES token içindir.
    # NO token fiyatları: NO_ask = 1 - YES_bid, NO_bid = 1 - YES_ask
    yes_bid = _f(raw.get("bestBid"))
    yes_ask = _f(raw.get("bestAsk"))
    no_bid  = (1.0 - yes_ask) if yes_ask is not None else None
    no_ask  = (1.0 - yes_bid) if yes_bid is not None else None

    return Market(
        id=str(raw.get("id", "")),
        question=(raw.get("question") or raw.get("slug") or "?").strip(),
        slug=str(raw.get("slug", "")),
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        volume24h=vol,
    )


# ── CLOB orderbook doğrulaması ───────────────────────────────────────────
async def fetch_best_prices(
    session: aiohttp.ClientSession,
    token_id: str,
) -> tuple[float | None, float | None]:
    """CLOB'dan token için gerçek bid/ask çek."""
    async with session.get(
        f"{CLOB_HOST}/book",
        params={"token_id": token_id},
    ) as r:
        if r.status != 200:
            return None, None
        data = await r.json()

    bids: list[dict] = data.get("bids") or []
    asks: list[dict] = data.get("asks") or []

    best_bid = max((_f(b.get("price")) for b in bids if b.get("price")), default=None)
    best_ask = min((_f(a.get("price")) for a in asks if a.get("price")), default=None)
    return best_bid, best_ask


async def verify_opportunity(
    session: aiohttp.ClientSession,
    market: Market,
) -> ArbOpportunity | None:
    """CLOB orderbook'undan gerçek fiyatları alıp arb hesapla."""
    (yes_bid, yes_ask), (no_bid, no_ask) = await asyncio.gather(
        fetch_best_prices(session, market.yes_token_id),
        fetch_best_prices(session, market.no_token_id),
    )

    # BUY arb: YES al + NO al → toplam < 1.00
    if yes_ask is not None and no_ask is not None:
        total_cost = yes_ask + no_ask
        if total_cost < (1.0 - MIN_PROFIT):
            profit_pct = (1.0 - total_cost) * 100
            return ArbOpportunity(market, "buy", profit_pct, yes_ask, no_ask)

    # SELL arb: YES sat + NO sat → toplam > 1.00
    if yes_bid is not None and no_bid is not None:
        total_recv = yes_bid + no_bid
        if total_recv > (1.0 + MIN_PROFIT):
            profit_pct = (total_recv - 1.0) * 100
            return ArbOpportunity(market, "sell", profit_pct, yes_bid, no_bid)

    return None


# ── Hızlı ön eleme (CLOB çağırmadan) ────────────────────────────────────
def quick_screen(market: Market) -> bool:
    """Gamma fiyatlarıyla hızlı kontrol — yanlış pozitif olabilir, OK."""
    ya, na = market.yes_ask, market.no_ask
    yb, nb = market.yes_bid, market.no_bid

    if ya is not None and na is not None:
        if (ya + na) < (1.0 - MIN_PROFIT / 2):  # daha gevşek eşik
            return True

    if yb is not None and nb is not None:
        if (yb + nb) > (1.0 + MIN_PROFIT / 2):
            return True

    return False


# ── Emir gönderici ───────────────────────────────────────────────────────
def _place_order_sync(
    client: Any,
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> dict[str, Any]:
    """Senkron emir — executor thread'de çalışır."""
    from py_clob_client.clob_types import OrderArgs, OrderType

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
    )
    signed = client.create_order(order_args)
    return client.post_order(signed, OrderType.FOK)


async def execute_arb(
    client: Any,
    opp: ArbOpportunity,
    loop: asyncio.AbstractEventLoop,
) -> None:
    m = opp.market
    log.info(
        "ARB EXECUTE | %s | dir=%s | kâr=%.2f%% | yes=%.4f no=%.4f",
        m.question[:55], opp.direction, opp.profit_pct, opp.yes_price, opp.no_price,
    )

    if opp.direction == "buy":
        yes_side, no_side = "BUY", "BUY"
    else:
        yes_side, no_side = "SELL", "SELL"

    yes_size = round(MAX_TRADE_USDC / opp.yes_price, 2)
    no_size  = round(MAX_TRADE_USDC / opp.no_price, 2)

    # YES ve NO emirlerini AYNI ANDA gönder (maksimum hız)
    yes_res, no_res = await asyncio.gather(
        loop.run_in_executor(
            None, _place_order_sync,
            client, m.yes_token_id, yes_side, opp.yes_price, yes_size,
        ),
        loop.run_in_executor(
            None, _place_order_sync,
            client, m.no_token_id, no_side, opp.no_price, no_size,
        ),
        return_exceptions=True,
    )

    log.info("YES sonuç: %s", yes_res)
    log.info("NO  sonuç: %s", no_res)


# ── Masaüstü bildirimi ────────────────────────────────────────────────────
def notify_opportunity(opp: ArbOpportunity) -> None:
    """Windows masaüstü bildirimi gönder. 'Markete git' butonu tarayıcıda açar."""
    m = opp.market
    yon = "AL (YES+NO al)" if opp.direction == "buy" else "SAT (YES+NO sat)"
    toast = Notification(
        app_id=APP_ID,
        title=f"💰 Arbitraj %{opp.profit_pct:.2f} kâr",
        msg=(
            f"{m.question[:90]}\n"
            f"{yon} | YES {opp.yes_price:.3f}  NO {opp.no_price:.3f}\n"
            f"24s hacim: ${m.volume24h:,.0f}"
        ),
        duration="long",
    )
    toast.set_audio(audio.LoopingAlarm, loop=False)
    toast.add_actions(label="Markete git", launch=m.url)
    toast.show()


# ── Ana döngü ────────────────────────────────────────────────────────────
async def main_loop(client: Any | None, execute: bool) -> None:
    loop = asyncio.get_event_loop()

    # Aynı fırsatı tekrar tekrar bildirmemek için son bildirim zamanları
    last_notified: dict[str, float] = {}

    # Kalıcı bağlantı havuzu — her istekte yeni TCP açma
    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, keepalive_timeout=30)
    headers = {"User-Agent": "polymarket-arb/1.0", "Accept": "application/json"}

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        mod = "OTOMATİK EMİR" if execute else "SADECE BİLDİRİM"
        log.info(
            "Bot başladı | mod=%s | MIN_PROFIT=%.0f%% | SCAN=%ds",
            mod, MIN_PROFIT * 100, SCAN_INTERVAL,
        )

        while True:
            t0 = time.monotonic()
            try:
                # 1. Tüm marketleri çek
                raw_markets = await fetch_markets(session)
                markets = [m for raw in raw_markets if (m := parse_market(raw))]

                # 2. Gamma fiyatlarıyla hızlı ön eleme
                candidates = [m for m in markets if quick_screen(m)]

                scan_ms = (time.monotonic() - t0) * 1000
                log.info(
                    "Tarama | %d market | %d aday | %.0fms",
                    len(markets), len(candidates), scan_ms,
                )

                # 3. Adaylar için CLOB orderbook'unu paralel çek ve doğrula
                if candidates:
                    verify_tasks = [verify_opportunity(session, m) for m in candidates]
                    results = await asyncio.gather(*verify_tasks, return_exceptions=True)

                    opps = [
                        r for r in results
                        if isinstance(r, ArbOpportunity)
                    ]
                    opps.sort(key=lambda o: o.profit_pct, reverse=True)

                    if opps:
                        log.info("%d gerçek ARB fırsatı bulundu!", len(opps))
                        now = time.monotonic()
                        for opp in opps:
                            if execute:
                                await execute_arb(client, opp, loop)
                                continue
                            # Sadece bildirim: cooldown ile spam'i önle
                            key = f"{opp.market.id}:{opp.direction}"
                            if now - last_notified.get(key, 0.0) >= NOTIFY_COOLDOWN:
                                log.info(
                                    "BİLDİRİM | %s | %s | kâr=%.2f%%",
                                    opp.market.question[:55], opp.direction, opp.profit_pct,
                                )
                                notify_opportunity(opp)
                                last_notified[key] = now
                    else:
                        log.info("Doğrulanmış ARB yok.")

            except aiohttp.ClientError as e:
                log.error("HTTP hatası: %s", e)
            except Exception:
                log.exception("Beklenmeyen hata")

            # Bir sonraki taramaya kadar bekle
            elapsed = time.monotonic() - t0
            wait = max(0.0, SCAN_INTERVAL - elapsed)
            if wait > 0:
                await asyncio.sleep(wait)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket arbitraj botu")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Otomatik emir gönder (.env gerekir). Varsayılan: sadece bildirim.",
    )
    args = parser.parse_args()

    client = build_clob_client() if args.execute else None
    try:
        asyncio.run(main_loop(client, execute=args.execute))
    except KeyboardInterrupt:
        log.info("Bot durduruldu.")


if __name__ == "__main__":
    main()
