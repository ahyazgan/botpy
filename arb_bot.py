"""
Polymarket async arbitraj botu — hız + güvenlik.

Strateji:
  YES_ask + NO_ask < (1 - MIN_PROFIT)  → ikisini AL  (buy arb)
  YES_bid + NO_bid > (1 + MIN_PROFIT)  → ikisini SAT (sell arb)

Hız teknikleri:
  - asyncio + aiohttp ile paralel market tarama
  - İki emri aynı anda gönder (asyncio.gather)
  - FOK (Fill-or-Kill): dolmayan emir anında iptal
  - Kalıcı HTTP bağlantı havuzu (TCPConnector)
  - CLOB orderbook ile fiyat doğrulaması

Güvenlik teknikleri (system-strengthening):
  - Bacak riski koruması: bir emir dolup diğeri iptal olursa, dolan
    bacak otomatik kapatılmaya çalışılır (tek taraflı açık pozisyon yok).
  - Cooldown/dedup: aynı market kısa sürede tekrar tetiklenemez.
  - Oturum bütçesi: toplam riske atılan USDC bir tavanla sınırlı.
  - Zarif yapılandırma: eksik secrets program açılışında net hata verir,
    ARB_DRY_RUN=1 ile emir göndermeden simülasyon yapılır.
  - HTTP retry + exponential backoff ve CLOB istekleri için eşzamanlılık
    sınırı (rate-limit koruması).

Çekirdek mantık (parse / arb tespiti / guard'lar) ağ veya gizli anahtar
gerektirmez; bu sayede birim testleriyle doğrulanabilir.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

# ── Ayarlar ─────────────────────────────────────────────────────────────
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"

SCAN_INTERVAL = 5         # saniye — her kaç saniyede tarasın
MIN_VOLUME_24H = 50_000   # USDC — düşük hacimli marketleri atla
MIN_PROFIT = 0.02         # %2 minimum brüt kâr (ön eleme)
MAX_TRADE_USDC = 50.0     # her leg için maksimum USDC
PAGE_LIMIT = 500

# Yürütme gerçekçiliği — slippage + maliyet
FEE_PCT = 0.0               # Polymarket işlem ücreti (şu an 0; modellenebilir)
GAS_COST_USDC = 0.05        # leg başına tahmini Polygon gas (USDC eşdeğeri)
MIN_NET_PROFIT_USDC = 0.5   # fee+gas+slippage sonrası min net kâr (USDC)

# Güvenlik
COOLDOWN_SEC = 60.0       # aynı market için iki işlem arası min süre
RECORD_COOLDOWN = 60.0    # aynı fırsatı DB'ye tekrar yazma aralığı (radar)
MAX_SESSION_USDC = 1_000.0  # bir oturumda riske atılabilecek toplam USDC
HTTP_RETRIES = 3          # geçici HTTP hataları için deneme sayısı
HTTP_BACKOFF_BASE = 0.5   # exponential backoff taban süresi (saniye)
CLOB_CONCURRENCY = 10     # CLOB orderbook'a aynı anda en fazla istek
REQUEST_TIMEOUT = 15      # saniye

# CLOB emir kısıtları
TICK_SIZE = 0.01          # fiyat adımı (çoğu market 0.01; bazıları 0.001)
MIN_SHARES = 5.0          # minimum emir büyüklüğü (shares)
PRICE_MIN = 0.01          # geçerli fiyat alt sınırı
PRICE_MAX = 0.99          # geçerli fiyat üst sınırı

# FOK emir cevabında "dolu" sayılan durumlar (heuristik)
_FILLED_STATUSES = {"matched", "filled"}

# ── Loglama ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── Yapılandırma ─────────────────────────────────────────────────────────
@dataclass
class Config:
    private_key: str
    funder_address: str
    api_key: str
    api_secret: str
    api_passphrase: str
    dry_run: bool = False


_REQUIRED_ENV = (
    "PRIVATE_KEY",
    "FUNDER_ADDRESS",
    "POLY_API_KEY",
    "POLY_SECRET",
    "POLY_PASSPHRASE",
)


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_config(env: dict[str, str] | None = None) -> Config:
    """Ortam değişkenlerini oku ve doğrula.

    Eksik secrets varsa ve DRY_RUN kapalıysa net bir hata ile çık.
    DRY_RUN açıkken anahtarlar boş olabilir (emir gönderilmez).
    """
    if env is None:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        env = dict(os.environ)

    dry_run = _is_truthy(env.get("ARB_DRY_RUN"))
    missing = [k for k in _REQUIRED_ENV if not env.get(k)]
    if missing and not dry_run:
        raise SystemExit(
            "Eksik ortam değişkenleri: "
            + ", ".join(missing)
            + "\n.env dosyasını doldurun ya da ARB_DRY_RUN=1 ile simülasyon yapın."
        )

    return Config(
        private_key=env.get("PRIVATE_KEY", ""),
        funder_address=env.get("FUNDER_ADDRESS", ""),
        api_key=env.get("POLY_API_KEY", ""),
        api_secret=env.get("POLY_SECRET", ""),
        api_passphrase=env.get("POLY_PASSPHRASE", ""),
        dry_run=dry_run,
    )


# ── Veri yapıları ────────────────────────────────────────────────────────
@dataclass
class Market:
    id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_bid: float | None   # YES için en iyi alış (sat fiyatı)
    yes_ask: float | None   # YES için en iyi satış (al fiyatı)
    no_bid: float | None    # NO için en iyi alış
    no_ask: float | None    # NO için en iyi satış
    volume24h: float


@dataclass
class ArbOpportunity:
    market: Market
    direction: str    # "buy" veya "sell"
    profit_pct: float
    yes_price: float
    no_price: float


# ── Güvenlik guard'ları (saf, test edilebilir) ──────────────────────────
class ExecutionGuard:
    """Aynı marketin tekrar tekrar / eşzamanlı tetiklenmesini engeller."""

    def __init__(self, cooldown: float = COOLDOWN_SEC) -> None:
        self.cooldown = cooldown
        self._last: dict[str, float] = {}
        self._inflight: set[str] = set()

    def _now(self) -> float:
        return time.monotonic()

    def can_execute(self, market_id: str) -> bool:
        if market_id in self._inflight:
            return False
        last = self._last.get(market_id)
        return last is None or (self._now() - last) >= self.cooldown

    def mark_start(self, market_id: str) -> None:
        self._inflight.add(market_id)

    def mark_done(self, market_id: str) -> None:
        self._inflight.discard(market_id)
        self._last[market_id] = self._now()


@dataclass
class Budget:
    """Oturum boyunca riske atılan toplam USDC için tavan."""

    max_total: float = MAX_SESSION_USDC
    spent: float = 0.0

    def can_afford(self, amount: float) -> bool:
        return (self.spent + amount) <= self.max_total

    def charge(self, amount: float) -> None:
        self.spent += amount


# ── Yardımcılar ──────────────────────────────────────────────────────────
def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_json_list(value: Any) -> list[Any]:
    """Gamma alanları JSON-string olabilir ('["a","b"]') ya da liste."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def extract_token_ids(raw: dict[str, Any]) -> tuple[str, str] | None:
    """YES/NO CLOB token id'lerini çıkar — birden çok formatı destekler.

    Gamma /markets ucu `clobTokenIds` (+ `outcomes`) döndürür; bunlar
    JSON-string olarak gelir. CLOB ucu ise `tokens[].token_id` verir.
    """
    token_ids = [str(t) for t in _parse_json_list(raw.get("clobTokenIds"))]
    outcomes = [str(o).strip().upper() for o in _parse_json_list(raw.get("outcomes"))]

    # Format 1: clobTokenIds + outcomes eşleştirmesi (en güvenilir)
    if token_ids and outcomes and len(token_ids) == len(outcomes):
        mapping = dict(zip(outcomes, token_ids))
        yes, no = mapping.get("YES"), mapping.get("NO")
        if yes and no:
            return yes, no

    # Format 2: outcomes yoksa, [YES, NO] sırası varsayılır
    if len(token_ids) == 2 and not outcomes:
        return token_ids[0], token_ids[1]

    # Format 3: CLOB tokens[] yapısı
    tokens = raw.get("tokens") or []
    yes_tok = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), None)
    no_tok = next((t for t in tokens if str(t.get("outcome", "")).upper() == "NO"), None)
    if yes_tok and no_tok:
        y, n = str(yes_tok.get("token_id", "")), str(no_tok.get("token_id", ""))
        if y and n:
            return y, n

    return None


def parse_market(raw: dict[str, Any]) -> Market | None:
    vol = _f(raw.get("volume24hr")) or 0.0
    if vol < MIN_VOLUME_24H:
        return None

    ids = extract_token_ids(raw)
    if ids is None:
        return None
    yes_token_id, no_token_id = ids

    # Gamma API: bestBid/bestAsk YES token içindir.
    # NO token fiyatları: NO_ask = 1 - YES_bid, NO_bid = 1 - YES_ask
    yes_bid = _f(raw.get("bestBid"))
    yes_ask = _f(raw.get("bestAsk"))
    no_bid = (1.0 - yes_ask) if yes_ask is not None else None
    no_ask = (1.0 - yes_bid) if yes_bid is not None else None

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
    )


def detect_arb(
    yes_bid: float | None,
    yes_ask: float | None,
    no_bid: float | None,
    no_ask: float | None,
    min_profit: float = MIN_PROFIT,
) -> tuple[str, float, float, float] | None:
    """Saf arb tespiti. (direction, profit_pct, yes_price, no_price) ya da None."""
    # BUY arb: YES al + NO al → toplam < 1.00
    if yes_ask is not None and no_ask is not None:
        total_cost = yes_ask + no_ask
        if total_cost < (1.0 - min_profit):
            return "buy", (1.0 - total_cost) * 100, yes_ask, no_ask

    # SELL arb: YES sat + NO sat → toplam > 1.00
    if yes_bid is not None and no_bid is not None:
        total_recv = yes_bid + no_bid
        if total_recv > (1.0 + min_profit):
            return "sell", (total_recv - 1.0) * 100, yes_bid, no_bid

    return None


# ── Order book derinliği / slippage (saf) ────────────────────────────────
def book_levels(side_levels: Any) -> list[tuple[float, float]]:
    """CLOB book tarafını [(price, size), ...] listesine çevir (geçersizleri at)."""
    out: list[tuple[float, float]] = []
    for lvl in side_levels or []:
        p = _f(lvl.get("price"))
        s = _f(lvl.get("size"))
        if p is not None and s is not None and s > 0:
            out.append((p, s))
    return out


def vwap_for_size(
    levels: list[tuple[float, float]], size: float,
) -> tuple[float | None, float]:
    """Verilen büyüklük için hacim-ağırlıklı ortalama fiyat (slippage dahil).

    `levels` tüketim sırasında olmalı (en iyi fiyat önce).
    (vwap, filled) döner; filled < size → yeterli derinlik yok.
    """
    remaining = size
    cost = 0.0
    filled = 0.0
    for price, avail in levels:
        take = min(remaining, avail)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled <= 0:
        return None, 0.0
    return cost / filled, filled


def net_arb_profit(
    direction: str,
    yes_price: float,
    no_price: float,
    size: float,
    *,
    fee_pct: float = FEE_PCT,
    gas_usdc: float = GAS_COST_USDC,
) -> float:
    """size adet çift için fee + gas sonrası net kâr (USDC).

    buy:  her çift `1 - (yes+no)` kazandırır (biri 1'e çözülür).
    sell: her çift `(yes+no) - 1` kazandırır.
    """
    pair = yes_price + no_price
    gross = size * ((1.0 - pair) if direction == "buy" else (pair - 1.0))
    fees = fee_pct * size * pair
    gas = gas_usdc * 2  # iki bacak
    return gross - fees - gas


def quick_screen(market: Market, min_profit: float = MIN_PROFIT) -> bool:
    """Gamma fiyatlarıyla hızlı ön eleme — yanlış pozitif olabilir, OK.

    CLOB doğrulamasından önce gevşek eşik kullanır.
    """
    return detect_arb(
        market.yes_bid,
        market.yes_ask,
        market.no_bid,
        market.no_ask,
        min_profit=min_profit / 2,
    ) is not None


def order_filled(res: Any) -> bool:
    """FOK emir cevabını yorumla: dolu mu?

    py_clob_client.post_order bir dict döndürür; istisna ise dolmamıştır.
    """
    if isinstance(res, Exception) or not isinstance(res, dict):
        return False
    if res.get("success") is False:
        return False
    status = str(res.get("status", "")).strip().lower()
    if status:
        return status in _FILLED_STATUSES
    # status alanı yoksa success=True'yu dolu kabul et
    return bool(res.get("success"))


# ── Emir hazırlama / CLOB kısıt doğrulaması (saf) ────────────────────────
@dataclass
class PreparedOrder:
    yes_price: float
    yes_size: float
    no_price: float
    no_size: float


def quantize_price(price: float, tick: float = TICK_SIZE) -> float:
    """Fiyatı en yakın tick'e yuvarla (kayan nokta artıklarını da temizler)."""
    return round(round(price / tick) * tick, 10)


def prepare_arb_orders(
    opp: ArbOpportunity,
    *,
    tick: float = TICK_SIZE,
    min_shares: float = MIN_SHARES,
    max_trade: float = MAX_TRADE_USDC,
) -> PreparedOrder | None:
    """Arb fırsatını CLOB kısıtlarına göre emirlere çevir.

    Fiyatları tick'e yuvarlar, aralık ve minimum büyüklük kontrolü yapar,
    yuvarlamadan sonra arbın hâlâ kârlı (net pozitif) olduğunu doğrular.
    Herhangi bir bacak geçersizse None döner → tek bacak gönderilmez
    (bacak riski önlenir).
    """
    yp = quantize_price(opp.yes_price, tick)
    npr = quantize_price(opp.no_price, tick)

    for p in (yp, npr):
        if not (PRICE_MIN <= p <= PRICE_MAX):
            return None

    # Yuvarlama arbı bozmuş olabilir → net pozitiflik hâlâ geçerli mi?
    if opp.direction == "buy":
        if (yp + npr) >= 1.0:
            return None
    elif opp.direction == "sell":
        if (yp + npr) <= 1.0:
            return None
    else:
        return None

    yes_size = round(max_trade / yp, 2)
    no_size = round(max_trade / npr, 2)
    if yes_size < min_shares or no_size < min_shares:
        return None

    return PreparedOrder(yes_price=yp, yes_size=yes_size, no_price=npr, no_size=no_size)


# ── Fırsat kaydı (radar) ─────────────────────────────────────────────────
def opp_to_row(opp: ArbOpportunity) -> dict[str, Any]:
    """ArbOpportunity'yi storage satırına çevir."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "market_id": opp.market.id,
        "question": opp.market.question,
        "direction": opp.direction,
        "profit_pct": opp.profit_pct,
        "yes_price": opp.yes_price,
        "no_price": opp.no_price,
    }


def maybe_record(store: Any, opp: ArbOpportunity, guard: ExecutionGuard) -> bool:
    """Fırsatı DB'ye yaz — aynı market için cooldown içinde tekrar yazma.

    store None ise (radar kapalı) sessizce atlar. Kayıt yapıldıysa True.
    """
    if store is None or not guard.can_execute(opp.market.id):
        return False
    store.record_opportunity(opp_to_row(opp))
    guard.mark_done(opp.market.id)
    return True


def format_opp(opp: ArbOpportunity) -> str:
    """Bildirim metni."""
    return (
        f"🎯 ARB {opp.direction.upper()} | {opp.market.question[:60]}\n"
        f"kâr={opp.profit_pct:.2f}% | YES={opp.yes_price:.3f} NO={opp.no_price:.3f}"
    )


# ── CLOB Client (lazy import) ────────────────────────────────────────────
def build_clob_client(config: Config):
    """py_clob_client yalnızca gerçek işlem yapılırken import edilir."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from py_clob_client.constants import POLYGON

    creds = ApiCreds(
        api_key=config.api_key,
        api_secret=config.api_secret,
        api_passphrase=config.api_passphrase,
    )
    return ClobClient(
        host=CLOB_HOST,
        key=config.private_key,
        chain_id=POLYGON,
        creds=creds,
        funder=config.funder_address,
    )


# ── Gerçek bakiye kontrolü (CLOB) ────────────────────────────────────────
def _usdc_balance_sync(client: Any) -> float | None:
    """CLOB'dan kullanılabilir USDC (collateral) bakiyesini sorgula.

    py_clob_client base-unit (6 ondalık) string döndürür → USDC'ye çevrilir.
    Hata durumunda None döner (çağıran tarafta güvenli ele alınır).
    """
    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
    except Exception:
        log.exception("Bakiye sorgusu başarısız")
        return None

    raw = resp.get("balance") if isinstance(resp, dict) else None
    val = _f(raw)
    if val is None:
        return None
    return val / 1_000_000  # 6 ondalık USDC


async def fetch_usdc_balance(
    client: Any, loop: asyncio.AbstractEventLoop,
) -> float | None:
    """Senkron bakiye sorgusunu executor'da çalıştır (event loop'u bloklamaz)."""
    return await loop.run_in_executor(None, _usdc_balance_sync, client)


# ── HTTP (retry + backoff) ───────────────────────────────────────────────
async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    retries: int = HTTP_RETRIES,
) -> Any:
    """Geçici hatalarda (429/5xx/ağ) exponential backoff ile yeniden dene."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            async with session.get(url, params=params) as r:
                if r.status == 429 or r.status >= 500:
                    raise aiohttp.ClientResponseError(
                        r.request_info, r.history, status=r.status,
                        message=f"retryable status {r.status}",
                    )
                r.raise_for_status()
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = e
            if attempt == retries - 1:
                break
            wait = HTTP_BACKOFF_BASE * (2 ** attempt)
            log.warning(
                "HTTP retry %d/%d (%s) — %.1fs bekle", attempt + 1, retries, e, wait,
            )
            await asyncio.sleep(wait)
    assert last_err is not None
    raise last_err


# ── Market tarayıcı (Gamma API) ──────────────────────────────────────────
async def fetch_markets(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = await _get_json(
            session,
            GAMMA_URL,
            params={
                "active": "true",
                "closed": "false",
                "limit": PAGE_LIMIT,
                "offset": offset,
            },
        )
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return markets


# ── CLOB orderbook doğrulaması (derinlik + maliyet) ──────────────────────
async def fetch_book(
    session: aiohttp.ClientSession,
    token_id: str,
    sem: asyncio.Semaphore | None = None,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """CLOB'dan tam orderbook çek; (bids, asks) tüketim sırasında döner.

    bids: en yüksek alış önce; asks: en düşük satış önce.
    """
    async def _do() -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        try:
            data = await _get_json(session, f"{CLOB_HOST}/book", params={"token_id": token_id})
        except aiohttp.ClientError:
            return [], []
        bids = book_levels(data.get("bids"))
        asks = book_levels(data.get("asks"))
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        return bids, asks

    if sem is None:
        return await _do()
    async with sem:
        return await _do()


def evaluate_book_arb(
    yes_bids: list[tuple[float, float]],
    yes_asks: list[tuple[float, float]],
    no_bids: list[tuple[float, float]],
    no_asks: list[tuple[float, float]],
    *,
    max_trade: float = MAX_TRADE_USDC,
    min_net_usdc: float = MIN_NET_PROFIT_USDC,
) -> tuple[str, float, float, float] | None:
    """Derinlik + maliyet farkındalıklı arb kararı (saf).

    VWAP ile gerçek dolum fiyatını ve fee+gas sonrası net kârı hesaplar.
    (direction, profit_pct, yes_vwap, no_vwap) ya da None.
    """
    # BUY arb: YES ask + NO ask < 1
    if yes_asks and no_asks and (yes_asks[0][0] + no_asks[0][0]) < 1.0:
        size = round(max_trade / max(yes_asks[0][0], no_asks[0][0]), 2)
        ya, yf = vwap_for_size(yes_asks, size)
        na, nf = vwap_for_size(no_asks, size)
        if ya is not None and na is not None and yf >= size and nf >= size:
            if net_arb_profit("buy", ya, na, size) >= min_net_usdc:
                return "buy", (1.0 - (ya + na)) * 100, ya, na

    # SELL arb: YES bid + NO bid > 1
    if yes_bids and no_bids and (yes_bids[0][0] + no_bids[0][0]) > 1.0:
        size = round(max_trade / max(yes_bids[0][0], no_bids[0][0]), 2)
        yb, yf = vwap_for_size(yes_bids, size)
        nb, nf = vwap_for_size(no_bids, size)
        if yb is not None and nb is not None and yf >= size and nf >= size:
            if net_arb_profit("sell", yb, nb, size) >= min_net_usdc:
                return "sell", ((yb + nb) - 1.0) * 100, yb, nb

    return None


async def verify_opportunity(
    session: aiohttp.ClientSession,
    market: Market,
    sem: asyncio.Semaphore | None = None,
) -> ArbOpportunity | None:
    """CLOB orderbook derinliğinden VWAP + maliyet sonrası net arb hesapla."""
    (yes_bids, yes_asks), (no_bids, no_asks) = await asyncio.gather(
        fetch_book(session, market.yes_token_id, sem),
        fetch_book(session, market.no_token_id, sem),
    )
    found = evaluate_book_arb(yes_bids, yes_asks, no_bids, no_asks)
    if found is None:
        return None
    direction, profit_pct, yes_price, no_price = found
    return ArbOpportunity(market, direction, profit_pct, yes_price, no_price)


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

    order_args = OrderArgs(token_id=token_id, price=price, size=size, side=side)
    signed = client.create_order(order_args)
    return client.post_order(signed, OrderType.FOK)


async def _send_order(
    client: Any,
    loop: asyncio.AbstractEventLoop,
    token_id: str,
    side: str,
    price: float,
    size: float,
) -> Any:
    return await loop.run_in_executor(
        None, _place_order_sync, client, token_id, side, price, size,
    )


async def _unwind_leg(
    client: Any,
    loop: asyncio.AbstractEventLoop,
    token_id: str,
    original_side: str,
    price: float,
    size: float,
) -> None:
    """Dolan tek bacağı ters emirle kapatmaya çalış (bacak riski koruması)."""
    counter_side = "SELL" if original_side == "BUY" else "BUY"
    log.critical(
        "BACAK RİSKİ! Tek taraflı pozisyon — %s bacağı ters emirle (%s) kapatılıyor.",
        original_side, counter_side,
    )
    try:
        res = await _send_order(client, loop, token_id, counter_side, price, size)
        if order_filled(res):
            log.warning("Bacak başarıyla kapatıldı (flatten). Cevap: %s", res)
        else:
            log.critical(
                "Bacak KAPATILAMADI — MANUEL MÜDAHALE GEREKLİ! token=%s cevap=%s",
                token_id, res,
            )
    except Exception:
        log.critical(
            "Bacak kapatma emri istisna fırlattı — MANUEL MÜDAHALE GEREKLİ! token=%s",
            token_id, exc_info=True,
        )


async def execute_arb(
    client: Any,
    opp: ArbOpportunity,
    loop: asyncio.AbstractEventLoop,
    *,
    budget: Budget,
    dry_run: bool = False,
    available_usdc: float | None = None,
) -> None:
    m = opp.market

    # CLOB kısıt doğrulaması: tick'e yuvarla, aralık/min büyüklük kontrolü.
    prepared = prepare_arb_orders(opp)
    if prepared is None:
        log.warning(
            "Emir kısıtları sağlanmadı (tick/min-size/aralık) — %s atlandı.",
            m.question[:40],
        )
        return
    yes_price, no_price = prepared.yes_price, prepared.no_price
    yes_size, no_size = prepared.yes_size, prepared.no_size
    notional = MAX_TRADE_USDC * 2

    if not budget.can_afford(notional):
        log.warning(
            "Bütçe tavanı doldu (harcanan=%.0f / tavan=%.0f USDC) — %s atlandı.",
            budget.spent, budget.max_total, m.question[:40],
        )
        return

    # Gerçek on-chain bakiye guard'ı (sorgu başarısızsa available_usdc=None
    # gelir; o durumda engellemeyiz, oturum bütçesi yine de koruma sağlar).
    if available_usdc is not None and available_usdc < notional:
        log.warning(
            "Yetersiz USDC bakiyesi (%.2f < %.2f gerekli) — %s atlandı.",
            available_usdc, notional, m.question[:40],
        )
        return

    log.info(
        "ARB EXECUTE%s | %s | dir=%s | kâr=%.2f%% | yes=%.4f no=%.4f",
        " [DRY]" if dry_run else "", m.question[:50], opp.direction,
        opp.profit_pct, opp.yes_price, opp.no_price,
    )

    if dry_run:
        budget.charge(notional)
        return

    yes_side, no_side = ("BUY", "BUY") if opp.direction == "buy" else ("SELL", "SELL")
    budget.charge(notional)

    # YES ve NO emirlerini AYNI ANDA gönder (maksimum hız)
    yes_res, no_res = await asyncio.gather(
        _send_order(client, loop, m.yes_token_id, yes_side, yes_price, yes_size),
        _send_order(client, loop, m.no_token_id, no_side, no_price, no_size),
        return_exceptions=True,
    )
    log.info("YES sonuç: %s", yes_res)
    log.info("NO  sonuç: %s", no_res)

    yes_ok = order_filled(yes_res)
    no_ok = order_filled(no_res)

    if yes_ok and no_ok:
        log.info("✓ ARB tamamlandı (her iki bacak dolu).")
    elif not yes_ok and not no_ok:
        log.info("Hiçbir bacak dolmadı (FOK iptal) — pozisyon yok, güvenli.")
    elif yes_ok and not no_ok:
        # YES doldu, NO iptal → açık YES pozisyonunu kapat
        await _unwind_leg(client, loop, m.yes_token_id, yes_side, yes_price, yes_size)
    else:
        # NO doldu, YES iptal → açık NO pozisyonunu kapat
        await _unwind_leg(client, loop, m.no_token_id, no_side, no_price, no_size)


# ── Ana döngü ────────────────────────────────────────────────────────────
async def main_loop(
    client: Any, *, dry_run: bool = False, store: Any = None, notifier: Any = None,
) -> None:
    loop = asyncio.get_event_loop()
    guard = ExecutionGuard(COOLDOWN_SEC)
    record_guard = ExecutionGuard(RECORD_COOLDOWN)
    budget = Budget(MAX_SESSION_USDC)
    sem = asyncio.Semaphore(CLOB_CONCURRENCY)

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, keepalive_timeout=30)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    headers = {"User-Agent": "polymarket-arb/1.0", "Accept": "application/json"}

    async with aiohttp.ClientSession(
        connector=connector, headers=headers, timeout=timeout,
    ) as session:
        log.info(
            "Bot başladı%s | MIN_PROFIT=%.0f%% | MAX_TRADE=%.0f | SCAN=%ds | "
            "cooldown=%.0fs | bütçe=%.0f USDC",
            " [DRY_RUN]" if dry_run else "", MIN_PROFIT * 100, MAX_TRADE_USDC,
            SCAN_INTERVAL, COOLDOWN_SEC, MAX_SESSION_USDC,
        )

        while True:
            t0 = time.monotonic()
            try:
                raw_markets = await fetch_markets(session)
                markets = [m for raw in raw_markets if (m := parse_market(raw))]
                candidates = [
                    m for m in markets
                    if quick_screen(m) and guard.can_execute(m.id)
                ]

                scan_ms = (time.monotonic() - t0) * 1000
                log.info(
                    "Tarama | %d market | %d aday | %.0fms",
                    len(markets), len(candidates), scan_ms,
                )

                if candidates:
                    results = await asyncio.gather(
                        *(verify_opportunity(session, m, sem) for m in candidates),
                        return_exceptions=True,
                    )
                    opps = [r for r in results if isinstance(r, ArbOpportunity)]
                    opps.sort(key=lambda o: o.profit_pct, reverse=True)

                    if opps:
                        log.info("%d gerçek ARB fırsatı bulundu!", len(opps))
                        for opp in opps:
                            # Radar: fırsatı geçmişe yaz (işlem açılsa da açılmasa da)
                            recorded = maybe_record(store, opp, record_guard)
                            # Yeni kayıtta bildir (dedup record_guard ile aynı)
                            if recorded and notifier is not None and notifier.enabled:
                                await loop.run_in_executor(
                                    None, notifier.send, format_opp(opp),
                                )
                            if not guard.can_execute(opp.market.id):
                                continue
                            guard.mark_start(opp.market.id)
                            try:
                                available = (
                                    None if dry_run or client is None
                                    else await fetch_usdc_balance(client, loop)
                                )
                                await execute_arb(
                                    client, opp, loop,
                                    budget=budget, dry_run=dry_run,
                                    available_usdc=available,
                                )
                            finally:
                                guard.mark_done(opp.market.id)
                    else:
                        log.info("Doğrulanmış ARB yok.")

            except aiohttp.ClientError as e:
                log.error("HTTP hatası: %s", e)
            except Exception:
                log.exception("Beklenmeyen hata")

            elapsed = time.monotonic() - t0
            wait = max(0.0, SCAN_INTERVAL - elapsed)
            if wait > 0:
                await asyncio.sleep(wait)


def main() -> None:
    config = load_config()
    client = None if config.dry_run else build_clob_client(config)
    if config.dry_run:
        log.warning("ARB_DRY_RUN aktif — gerçek emir GÖNDERİLMEYECEK (simülasyon).")

    # Radar: bulunan fırsatları paylaşılan SQLite'a yaz (dashboard /arb okur).
    from storage import Store

    from notify import Notifier

    store = Store()
    notifier = Notifier.from_env()
    if notifier.enabled:
        log.info(
            "Bildirim aktif (telegram=%s, discord=%s)",
            notifier.telegram_enabled, notifier.discord_enabled,
        )
    try:
        asyncio.run(main_loop(
            client, dry_run=config.dry_run, store=store, notifier=notifier,
        ))
    except KeyboardInterrupt:
        log.info("Bot durduruldu.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
