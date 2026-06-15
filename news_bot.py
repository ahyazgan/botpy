"""
Kripto haber-trade uyarı motoru.

Akış (arka plan thread, her SCAN_INTERVAL_SEC saniyede):
  1. Haber kaynaklarını çek (RSS + Binance listeleme duyuruları)
  2. Yeni haberleri ayıkla (id ile tekrar engelle)
  3. Her haberi PUANLA: hangi coin, etki gücü (1-10), yön (bullish/bearish)
  4. Gücü ALERT_THRESHOLD üstü olanlar → masaüstü bildirimi + /alerts listesi

FastAPI endpoint'leri in-memory cache'i okur (thread-safe).
Puanlama: varsayılan kural-tabanlı (sıfır maliyet). ANTHROPIC_API_KEY tanımlıysa
Claude API ile daha akıllı puanlama (score_with_claude — sonraki adımda).
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

import storage
import trader
from netutil import get_json, get_stats
from notify import Notifier

load_dotenv()  # .env dosyasındaki ANTHROPIC_API_KEY'i okur

# Uzak bildirim (Telegram/Discord) — env tanımlıysa otomatik etkin, yoksa sessiz.
# winotify (masaüstü) yalnızca bilgisayar başındayken işe yarar; bu kanal güçlü
# haber + oto-işlem olaylarını telefona/uzağa ulaştırır.
_notifier = Notifier.from_env()

# Sinyal arşivi: güçlü haberleri SQLite'a kalıcı yazar (restart'a dayanıklı).
# Lazy — yalnızca ilk kullanımda açılır (import'ta dosya yaratma yan etkisi yok).
_store: storage.Store | None = None


def get_store() -> storage.Store:
    global _store
    if _store is None:
        _store = storage.Store()
    return _store

# İşlem/ayar uçları için opsiyonel token koruması. API_TOKEN env tanımlıysa
# mutasyon uçları (trade/settings/positions) X-API-Token başlığı ister; yoksa
# açık (yerel kullanım — geriye dönük uyumlu).
API_TOKEN = os.environ.get("API_TOKEN") or None


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    if API_TOKEN and x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Geçersiz veya eksik API token (X-API-Token)")

# ── Ayarlar ──────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 20      # saniye — kaynaklar ne sıklıkta taransın
ALERT_THRESHOLD   = 7       # bu güç (1-10) ve üstü = bildirim at
MAX_NEWS_KEEP     = 300     # bellekte tutulacak haber sayısı
MAX_ARCHIVE_SIGNALS = 5000  # SQLite arşivinde tutulacak max sinyal (sınırsız büyümeyi önler)
ARCHIVE_PRUNE_EVERY = 200   # her N yeni sinyalde bir eski kayıtları buda
MAX_NEWS_AGE_HOURS = 24     # bundan eski haberler feed'den düşer
REQUEST_TIMEOUT   = 15

# Binance rutin duyuru gürültüsü — fiyatı oynatmaz, feed'i doldurur (filtrelenir).
# Sadece gerçek spot listeleme/delisting gibi işe yarayan haberler kalır.
BINANCE_NOISE = re.compile(
    r"perpetual|futures will launch|tradfi|\bon earn\b|simple earn|vip loan|"
    r"\bmargin\b|airdrop|pre-?ipo|copy trading|leveraged token|dual investment|"
    r"wealth|buy crypto|convert|trading bot|auto-?invest",
    re.I,
)
APP_ID            = "Kripto Haber Trade"

# Puanlama: ANTHROPIC_API_KEY tanımlıysa Claude ile akıllı puanlama, yoksa kural-tabanlı.
# Haiku 4.5 — yüksek hacimli başlık sınıflandırması için hızlı/ucuz. (Opus istersen
# CLAUDE_MODEL'i "claude-opus-4-8" yap; maliyet ~5x artar.)
CLAUDE_MODEL = "claude-haiku-4-5"
USE_CLAUDE = bool(os.environ.get("ANTHROPIC_API_KEY"))

# ── Fiyat teyidi (Binance public) ────────────────────────────────────────
BINANCE_API = "https://api.binance.com/api/v3"
MIN_VOLUME_USD = 1_000_000     # bu hacmin altı = düşük likidite (slippage riski)
CONFIRM_MOVE_PCT = 0.5         # son penceede haber yönünde en az bu % hareket = teyit
ALREADY_PRICED_PCT = 25.0      # 24s'te bu % üzeri hareket = büyük kısmı fiyatlanmış
# Teyit penceresi: kaç dakikalık mum × kaç adet. Varsayılan 15m×4 (son 15dk + ~1s).
# Daha hızlı/erken teyit için CONFIRM_INTERVAL=1m CONFIRM_LIMIT=15 (son 1dk + 15dk);
# daha gürültülü ama haberin önünde olur. Backtest'le kalibre et.
CONFIRM_INTERVAL = os.environ.get("CONFIRM_INTERVAL", "15m")
CONFIRM_LIMIT = int(os.environ.get("CONFIRM_LIMIT", "4"))
# Binance USDT paritesi olmayan/olağan dışı coinler için stop listesi
_NOT_TRADEABLE = {"USDT", "USDC", "USD", "FDUSD", "TRY", "AED", "OPENAI", "ANTHROPIC"}

# RSS kaynakları — ücretsiz, anahtar gerekmez
RSS_FEEDS: dict[str, str] = {
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt":       "https://decrypt.co/feed",
    "TheBlock":      "https://www.theblock.co/rss.xml",
    "BMag":          "https://bitcoinmagazine.com/feed",
}

# Binance yeni listeleme duyuruları (catalogId=48) — en yüksek etkili sinyal
BINANCE_ANN_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query"
BINANCE_ANN_BASE = "https://www.binance.com/en/support/announcement/"

# TreeNews WebSocket — GERÇEK ZAMANLI haber (borsa duyuruları + Twitter + haber siteleri).
# Ücretsiz, auth gerekmez. RSS'in 20sn gecikmesi yerine saniyeler içinde haber.
USE_TREENEWS = True
TREE_WS = "wss://news.treeofalpha.com/ws"
TREE_BACKFILL_GUARD_SEC = 8   # bağlantının ilk saniyelerindeki mesajlar = geçmiş, bildirme

# Ölü-adam anahtarı: asıl gerçek-zamanlı kaynak (WS) bu kadar saniye kopuk/sessiz
# kalırsa uzak kanaldan uyar (canlıda sessiz sinyal-kaybını önler), düzelince haber ver.
WS_STALE_ALERT_SEC = float(os.environ.get("WS_STALE_ALERT_SEC", "600"))

# ── Loglama ──────────────────────────────────────────────────────────────
def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


log = logging.getLogger(__name__)


# ── Veri yapısı ──────────────────────────────────────────────────────────
@dataclass
class NewsItem:
    id: str
    source: str
    title: str
    url: str
    published: str | None
    fetched_at: str
    # puanlama sonuçları
    coins: list[str] = field(default_factory=list)
    impact: int = 0                 # 1-10
    direction: str = "neutral"      # bullish | bearish | neutral
    reason: str = ""
    scorer: str = "rule"            # rule | claude
    # fiyat teyidi (Binance)
    symbol: str | None = None       # işlem yapılacak parite (örn. BTCUSDT)
    price_24h_pct: float | None = None
    price_15m_pct: float | None = None
    price_60m_pct: float | None = None   # ~1 saatlik hareket (çoklu zaman dilimi teyidi)
    volume_usd: float | None = None
    confirmed: bool = False         # haber + fiyat hareketi uyumlu mu
    price_note: str = ""            # teyit açıklaması

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "published": self.published,
            "fetched_at": self.fetched_at,
            "coins": self.coins,
            "impact": self.impact,
            "direction": self.direction,
            "reason": self.reason,
            "scorer": self.scorer,
            "symbol": self.symbol,
            "price_24h_pct": self.price_24h_pct,
            "price_15m_pct": self.price_15m_pct,
            "price_60m_pct": self.price_60m_pct,
            "volume_usd": self.volume_usd,
            "confirmed": self.confirmed,
            "price_note": self.price_note,
        }


def _news_id(source: str, url: str, title: str) -> str:
    raw = f"{source}|{url or title}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Cache (thread-safe) ──────────────────────────────────────────────────
_cache_lock = threading.Lock()
_news: list[NewsItem] = []          # en yeni başta
_seen_ids: set[str] = set()
_primed = False                     # ilk tarama: mevcut haberleri bildirimsiz tohumla
_status: dict[str, Any] = {
    "updated_at": None,
    "error": None,
    "total_seen": 0,
    "alert_threshold": ALERT_THRESHOLD,
}
_started_at = time.time()   # uptime hesabı için

# Gözlemlenebilirlik sayaçları (monoton artan; /metrics ile dışa verilir)
_metrics: dict[str, int] = {
    "alerts_total": 0,          # eşik üstü güçlü haber sayısı
    "trades_opened_total": 0,   # otomatik açılan pozisyon sayısı
    "scan_errors_total": 0,     # arka plan tarama hatası sayısı
}

# TreeNews WS sağlığı (asıl gerçek-zamanlı kaynak) — gözlemlenebilirlik
_ws_state: dict[str, Any] = {"connected": False, "last_msg_at": None}


def _ws_last_msg_age(now: float | None = None) -> float | None:
    """Son WS mesajından bu yana geçen saniye (hiç mesaj yoksa None). Saf."""
    last = _ws_state.get("last_msg_at")
    if last is None:
        return None
    return round((now if now is not None else time.time()) - last, 1)

# ── Çalışma zamanı ayarları (panelden değişir, store'da kalıcı) ───────────
_news_settings: dict[str, Any] = {
    "alert_threshold": ALERT_THRESHOLD,   # bu güç (1-10) ve üstü = uyarı/işlem
    "remote_notify": True,                # Telegram/Discord push aç/kapat (env yoksa zaten sessiz)
}
_settings_loaded = False
_rss_feeds: dict[str, str] | None = None   # store'dan lazy yüklenen efektif RSS listesi


def _load_news_settings() -> None:
    """Kalıcı ayarları store'dan bir kez yükle (restart'a dayanıklı)."""
    global _settings_loaded
    if _settings_loaded:
        return
    _settings_loaded = True
    try:
        st = get_store()
        t = st.get_setting("news_alert_threshold")
        if t is not None:
            _news_settings["alert_threshold"] = int(t)
        rn = st.get_setting("news_remote_notify")
        if rn is not None:
            _news_settings["remote_notify"] = rn == "1"
    except Exception as e:
        log.warning("Haber ayarları yüklenemedi: %s", e)
    _status["alert_threshold"] = _news_settings["alert_threshold"]


def get_news_settings() -> dict[str, Any]:
    _load_news_settings()
    return {**_news_settings, "remote_channels_available": getattr(_notifier, "enabled", False)}


def update_news_settings(patch: dict[str, Any]) -> dict[str, Any]:
    _load_news_settings()
    st = get_store()
    if patch.get("alert_threshold") is not None:
        v = max(1, min(10, int(patch["alert_threshold"])))
        _news_settings["alert_threshold"] = v
        _status["alert_threshold"] = v
        st.set_setting("news_alert_threshold", str(v))
    if patch.get("remote_notify") is not None:
        v = bool(patch["remote_notify"])
        _news_settings["remote_notify"] = v
        st.set_setting("news_remote_notify", "1" if v else "0")
    return get_news_settings()


# ── Kaynak çekiciler ─────────────────────────────────────────────────────
def fetch_rss(name: str, url: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    d = feedparser.parse(url)
    for e in d.entries[:40]:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        if not title:
            continue
        published = None
        if getattr(e, "published", None):
            published = str(e.published)
        items.append(
            NewsItem(
                id=_news_id(name, link, title),
                source=name,
                title=title,
                url=link,
                published=published,
                fetched_at=_now_iso(),
            )
        )
    return items


def fetch_binance_announcements(session: requests.Session) -> list[NewsItem]:
    items: list[NewsItem] = []
    r = session.get(
        BINANCE_ANN_URL,
        params={"catalogId": 48, "pageNo": 1, "pageSize": 20},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "lang": "en"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    articles = (r.json().get("data") or {}).get("articles") or []
    for a in articles:
        title = (a.get("title") or "").strip()
        code = a.get("code") or ""
        if not title or not code:
            continue
        url = f"{BINANCE_ANN_BASE}{code}"
        items.append(
            NewsItem(
                id=_news_id("Binance", url, title),
                source="Binance",
                title=title,
                url=url,
                published=None,
                fetched_at=_now_iso(),
            )
        )
    return items


def get_rss_feeds() -> dict[str, str]:
    """Efektif RSS kaynakları (store'da kalıcı; yoksa RSS_FEEDS varsayılanı)."""
    global _rss_feeds
    if _rss_feeds is None:
        try:
            raw = get_store().get_setting("news_rss_feeds")
            _rss_feeds = json.loads(raw) if raw else dict(RSS_FEEDS)
        except Exception as e:
            log.warning("RSS ayarı yüklenemedi: %s", e)
            _rss_feeds = dict(RSS_FEEDS)
    return _rss_feeds


def set_rss_feeds(feeds: dict[str, str]) -> dict[str, str]:
    """RSS kaynaklarını ayarla (yalnızca http(s); store'a kalıcı yaz)."""
    global _rss_feeds
    clean = {str(k): str(v) for k, v in feeds.items()
             if str(v).startswith(("http://", "https://"))}
    _rss_feeds = clean
    try:
        get_store().set_setting("news_rss_feeds", json.dumps(clean))
    except Exception as e:
        log.warning("RSS ayarı yazılamadı: %s", e)
    return clean


def fetch_all(session: requests.Session) -> list[NewsItem]:
    """Tüm kaynakları çek; biri patlarsa diğerleri devam etsin."""
    out: list[NewsItem] = []
    for name, url in get_rss_feeds().items():
        try:
            out.extend(fetch_rss(name, url))
        except Exception as e:
            log.warning("RSS başarısız (%s): %s", name, e)
    try:
        out.extend(fetch_binance_announcements(session))
    except Exception as e:
        log.warning("Binance duyuru başarısız: %s", e)
    return out


# ── Puanlama (kural-tabanlı) ─────────────────────────────────────────────
# Coin tespiti: ticker → düzenli ifade (sembol veya tam ad)
COIN_PATTERNS: dict[str, re.Pattern[str]] = {
    "BTC":  re.compile(r"\b(btc|bitcoin)\b", re.I),
    "ETH":  re.compile(r"\b(eth|ethereum|ether)\b", re.I),
    "SOL":  re.compile(r"\b(sol|solana)\b", re.I),
    "XRP":  re.compile(r"\b(xrp|ripple)\b", re.I),
    "BNB":  re.compile(r"\b(bnb|binance coin)\b", re.I),
    "ADA":  re.compile(r"\b(ada|cardano)\b", re.I),
    "DOGE": re.compile(r"\b(doge|dogecoin)\b", re.I),
    "AVAX": re.compile(r"\b(avax|avalanche)\b", re.I),
    "DOT":  re.compile(r"\b(dot|polkadot)\b", re.I),
    "MATIC": re.compile(r"\b(matic|polygon)\b", re.I),
    "LINK": re.compile(r"\b(link|chainlink)\b", re.I),
    "LTC":  re.compile(r"\b(ltc|litecoin)\b", re.I),
    "TRX":  re.compile(r"\b(trx|tron)\b", re.I),
    "SHIB": re.compile(r"\b(shib|shiba)\b", re.I),
    "TON":  re.compile(r"\b(toncoin|\bton\b)\b", re.I),
    "SUI":  re.compile(r"\bsui\b", re.I),
}

# Etki anahtar kelimeleri: (kelime kalıbı, güç 1-10, yön)
IMPACT_KEYWORDS: list[tuple[re.Pattern[str], int, str]] = [
    # Çok güçlü olumsuz
    (re.compile(r"\b(hack|hacked|exploit|breach|stolen|drained)\b", re.I), 9, "bearish"),
    (re.compile(r"\b(bankrupt|bankruptcy|insolvent|collapse|collapses)\b", re.I), 9, "bearish"),
    (re.compile(r"\b(ban|banned|bans|outlaw|illegal)\b", re.I), 8, "bearish"),
    (re.compile(r"\b(lawsuit|sues|sued|charges|fraud|investigat)\w*", re.I), 8, "bearish"),
    (re.compile(r"\b(delist|delisting|halt|halts|suspend)\w*", re.I), 8, "bearish"),
    (re.compile(r"\b(crash|crashes|plunge|plunges|dump|dumps|liquidat)\w*", re.I), 7, "bearish"),
    (re.compile(r"\b(sell-?off|tumbl|slump|nosedive)\w*", re.I), 6, "bearish"),
    # Çok güçlü olumlu
    (re.compile(r"\betf\b.*\b(approv|launch|live)\w*|\b(approv\w*)\b.*\betf\b", re.I), 9, "bullish"),
    (re.compile(r"\b(will list|lists|listing|adds)\b", re.I), 8, "bullish"),
    (re.compile(r"\b(partnership|partners with|integrat\w*|adopt\w*)\b", re.I), 7, "bullish"),
    (re.compile(r"\b(blackrock|fidelity|institutional|spot etf)\b", re.I), 7, "bullish"),
    (re.compile(r"\b(mainnet|upgrade|launches|launch)\b", re.I), 6, "bullish"),
    (re.compile(r"\b(surge|surges|rally|rallies|all-?time high|ath|soars?)\b", re.I), 6, "bullish"),
    # Makro / düzenleme (nötr ama önemli)
    (re.compile(r"\b(sec|cftc|fed|federal reserve|regulat\w*|cpi|interest rate)\b", re.I), 6, "neutral"),
]


def detect_coins(text: str) -> list[str]:
    return [c for c, pat in COIN_PATTERNS.items() if pat.search(text)]


# Binance başlıklarından ticker: parantez içi (GENIUS) veya XXXUSDT çiftleri
_BINANCE_PAREN = re.compile(r"\(([A-Z0-9]{2,10})\)")
_BINANCE_PAIR = re.compile(r"\b([A-Z0-9]{2,10})(?:USDT|USDC|USD|FDUSD|BTC|TRY|AED)\b")
_BINANCE_STOP = {"USD", "USDT", "USDC", "FDUSD", "TRY", "AED", "BTC", "ETH", "SPOT"}


def extract_binance_tickers(title: str) -> list[str]:
    found: list[str] = []
    for m in _BINANCE_PAREN.findall(title):
        if m not in _BINANCE_STOP:
            found.append(m)
    for m in _BINANCE_PAIR.findall(title):
        if m not in _BINANCE_STOP:
            found.append(m)
    return list(dict.fromkeys(found))


def score_item(item: NewsItem) -> None:
    """item'i yerinde puanlar (impact, coins, direction, reason)."""
    text = item.title
    item.coins = detect_coins(text)

    best_impact = 0
    best_dir = "neutral"
    reasons: list[str] = []
    for pat, weight, direction in IMPACT_KEYWORDS:
        m = pat.search(text)
        if m:
            reasons.append(m.group(0))
            if weight > best_impact:
                best_impact = weight
                best_dir = direction

    # Binance yeni listeleme duyurusu = otomatik güçlü olumlu sinyal
    if item.source == "Binance":
        tickers = extract_binance_tickers(text)
        if tickers:
            item.coins = list(dict.fromkeys(item.coins + tickers))
        if re.search(r"\b(list|launch|add)\w*", text, re.I):
            best_impact = max(best_impact, 8)
            best_dir = "bullish"
            reasons.append("Binance listeleme")

    # Belirli bir coin yoksa ve haber jenerikse etkiyi biraz düşür
    if not item.coins and best_impact > 0 and item.source != "Binance":
        best_impact = max(1, best_impact - 2)

    item.impact = best_impact
    item.direction = best_dir
    item.reason = ", ".join(dict.fromkeys(reasons))[:120]
    item.scorer = "rule"


# ── Bağlam beyni (kaynak güvenilirliği + haber yorgunluğu) ───────────────
# Claude'a başlık dışında bağlam ipucu vererek isabeti yükseltir; ek istek/gecikme
# YOK (sadece prompt'a kısa etiket eklenir). Kaynak tier'i + coin'in son saatlerdeki
# haber sıklığı puanlamayı kalibre eder.
_EXCHANGE_SRC = ("binance", "coinbase", "upbit", "okx", "bybit", "kraken",
                 "kucoin", "bitget", "huobi", "gate", "bitfinex")
_SOCIAL_SRC = ("twitter", "tweet", "x.com", "telegram", "reddit", "discord")
_MEDIA_SRC = ("coindesk", "cointelegraph", "theblock", "decrypt", "bmag", "blog",
              "direct", "reuters", "bloomberg", "wsj", "magazine")
FATIGUE_WINDOW_HOURS = 6   # bu pencerede coin kaç kez haber oldu = "yorgunluk"


def _source_tier(source: str) -> str:
    """Kaynağı güvenilirlik sınıfına ayır: resmi-borsa > medya > sosyal > diğer."""
    s = source.lower().lstrip("⚡").strip()
    if any(x in s for x in _EXCHANGE_SRC):
        return "resmi-borsa"
    if any(x in s for x in _SOCIAL_SRC):
        return "sosyal"
    if any(x in s for x in _MEDIA_SRC):
        return "medya"
    return "diğer"


def _coin_fatigue(coin: str, now: datetime, recent: list[NewsItem]) -> int:
    """Son FATIGUE_WINDOW_HOURS içinde bu coin'i konu alan haber sayısı (yorgunluk)."""
    cutoff = now - timedelta(hours=FATIGUE_WINDOW_HOURS)
    n = 0
    for it in recent:
        ts = _parse_time(it.published) or _parse_time(it.fetched_at)
        if ts and ts >= cutoff and coin in it.coins:
            n += 1
    return n


def _item_context(it: NewsItem, now: datetime, recent: list[NewsItem]) -> str:
    """Bir haber için Claude'a verilecek bağlam etiketi (kaynak tier + yorgunluk)."""
    parts = [f"kaynak:{_source_tier(it.source)}"]
    if it.coins:
        fat = max(_coin_fatigue(c, now, recent) for c in it.coins)
        if fat > 1:
            parts.append(f"son{FATIGUE_WINDOW_HOURS}s:{fat}x (yorgun)")
    return " · ".join(parts)


# ── Puanlama (Claude — opsiyonel, akıllı) ────────────────────────────────
_anthropic_client: Any = None


def _get_anthropic() -> Any:
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


class _ItemScore(BaseModel):
    index: int
    coins: list[str]
    impact: int           # 1-10
    direction: str        # bullish | bearish | neutral
    reason: str


class _ScoreBatch(BaseModel):
    results: list[_ItemScore]


_SCORE_SYSTEM = (
    "Sen bir kripto haber-trade analistisin. Sana numaralı kripto haber başlıkları "
    "verilecek. Her başlığın köşeli parantezinde kaynak ve bağlam ipuçları var:\n"
    "- kaynak:resmi-borsa = borsanın kendi duyurusu, en güvenilir (etkiyi tam ver)\n"
    "- kaynak:medya = haber sitesi, güvenilir\n"
    "- kaynak:sosyal = doğrulanmamış tweet/söylenti — etkiyi TEMKİNLİ puanla (1-2 düşür)\n"
    "- kaynak:diğer = belirsiz kaynak — temkinli\n"
    "- 'sonNs:Mx (yorgun)' = bu coin son N saatte M kez haber oldu; haber zaten "
    "fiyatlanmış olabilir, ek haberin marjinal etkisi azdır — etkiyi bir miktar düşür\n"
    "Her başlık için tek bir JSON kaydı üret:\n"
    "- index: başlığın numarası\n"
    "- coins: etkilenen coin ticker'ları (örn. ['BTC','SOL']); net coin yoksa boş liste\n"
    "- impact: 1-10 piyasa etkisi (10 = piyasayı anında sert hareket ettirir: hack, "
    "iflas, ETF onayı, büyük borsa listelemesi, yasak, dava; 1 = önemsiz/genel yorum). "
    "Kaynak güvenilirliği ve yorgunluğu bu skoru ayarlar.\n"
    "- direction: 'bullish' (fiyatı yukarı), 'bearish' (aşağı) veya 'neutral'\n"
    "- reason: en fazla 12 kelimelik Türkçe gerekçe\n"
    "Sadece istenen yapıyı döndür."
)


# Tek Claude isteğinde puanlanacak haber sayısı. Küçük tut ki çıktı token
# sınırına (max_tokens) sığsın — büyük gruplarda JSON kesilir.
CLAUDE_BATCH = 25


def _score_chunk(client: Any, chunk: list[NewsItem], recent: list[NewsItem] | None = None) -> None:
    now = datetime.now(timezone.utc)
    ctx = recent if recent is not None else chunk
    listing = "\n".join(
        f"{i}. [{it.source} · {_item_context(it, now, ctx)}] {it.title}"
        for i, it in enumerate(chunk)
    )
    resp = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=_SCORE_SYSTEM,
        messages=[{"role": "user", "content": listing}],
        output_format=_ScoreBatch,
    )
    by_index = {r.index: r for r in resp.parsed_output.results}
    for i, it in enumerate(chunk):
        r = by_index.get(i)
        if r is None:
            score_item(it)
            continue
        it.coins = [str(c).upper() for c in r.coins][:6]
        it.impact = max(0, min(10, int(r.impact)))
        it.direction = r.direction if r.direction in ("bullish", "bearish", "neutral") else "neutral"
        it.reason = (r.reason or "")[:160]
        it.scorer = "claude"


def score_with_claude(items: list[NewsItem]) -> None:
    """Yeni haberleri CLAUDE_BATCH'lik gruplar halinde puanla. Bir grup
    başarısız olursa sadece o gruba kural-tabanlı puanlama uygulanır."""
    if not items:
        return
    client = _get_anthropic()
    # Yorgunluk hesabı için son haberlerin anlık görüntüsü (bağlam beyni)
    with _cache_lock:
        recent = list(_news)
    for start in range(0, len(items), CLAUDE_BATCH):
        chunk = items[start:start + CLAUDE_BATCH]
        try:
            _score_chunk(client, chunk, recent)
        except Exception as e:
            log.warning("Claude grup puanlama başarısız (kural-tabanlı): %s", e)
            for it in chunk:
                if it.scorer != "claude":
                    score_item(it)


# ── Fiyat teyidi (Binance public) ────────────────────────────────────────
def _fetch_symbol_stats(session: requests.Session, symbol: str) -> dict[str, float] | None:
    """Bir parite için 24s değişim, hacim, son ~15dk ve ~1s hareketini döndür."""
    t = get_json(f"{BINANCE_API}/ticker/24hr", params={"symbol": symbol},
                 timeout=REQUEST_TIMEOUT, session=session)
    if not t:
        return None
    # Yapılandırılabilir pencere → hem son mum (kısa pencere) hem tüm pencere hareketi
    candles = get_json(
        f"{BINANCE_API}/klines",
        params={"symbol": symbol, "interval": CONFIRM_INTERVAL, "limit": str(CONFIRM_LIMIT)},
        timeout=REQUEST_TIMEOUT, session=session,
    )
    move15 = move60 = 0.0
    if isinstance(candles, list) and candles:
        last = candles[-1]
        o15, c15 = float(last[1]), float(last[4])
        if o15:
            move15 = (c15 - o15) / o15 * 100
        o60, c60 = float(candles[0][1]), float(candles[-1][4])
        if o60:
            move60 = (c60 - o60) / o60 * 100
    return {
        "pct24": float(t.get("priceChangePercent", 0) or 0),
        "vol": float(t.get("quoteVolume", 0) or 0),
        "move15": move15,
        "move60": move60,
    }


def confirm_with_price(session: requests.Session, item: NewsItem) -> None:
    """Haberi Binance fiyat hareketiyle teyit et. item alanlarını yerinde doldurur."""
    coins = [c for c in item.coins if c not in _NOT_TRADEABLE]
    if not coins:
        item.price_note = "İşlem yapılabilir coin yok"
        return
    if item.direction == "neutral":
        item.price_note = "Yön nötr — yönlü işlem yok"

    stats = None
    for coin in coins:
        sym = f"{coin}USDT"
        try:
            stats = _fetch_symbol_stats(session, sym)
        except Exception:
            stats = None
        if stats is not None:
            item.symbol = sym
            break

    if stats is None:
        item.price_note = "Binance'de USDT paritesi bulunamadı"
        return

    item.price_24h_pct = round(stats["pct24"], 2)
    item.price_15m_pct = round(stats["move15"], 2)
    item.price_60m_pct = round(stats.get("move60", 0.0), 2)
    item.volume_usd = stats["vol"]

    liq_ok = stats["vol"] >= MIN_VOLUME_USD
    move = stats["move15"]
    move60 = stats.get("move60", 0.0)
    if item.direction == "bullish":
        dir_match = move >= CONFIRM_MOVE_PCT
        tf_ok = move60 >= -CONFIRM_MOVE_PCT   # 1s belirgin düşüşte değil
    elif item.direction == "bearish":
        dir_match = move <= -CONFIRM_MOVE_PCT
        tf_ok = move60 <= CONFIRM_MOVE_PCT    # 1s belirgin yükselişte değil
    else:
        dir_match = tf_ok = False

    already_priced = abs(stats["pct24"]) >= ALREADY_PRICED_PCT
    # Çoklu zaman dilimi: 15dk + 1s yön uyumu (15dk spike ama 1s ters = fade riski)
    item.confirmed = bool(dir_match and liq_ok and tf_ok)

    if not liq_ok:
        item.price_note = f"Düşük likidite (24s hacim ${stats['vol']:,.0f}) — slippage riski"
    elif item.direction == "neutral":
        pass  # not yukarıda set edildi
    elif dir_match and not tf_ok:
        item.price_note = f"15dk uyumlu ama 1s ters yönde (%{move60:+.1f}) — fade riski, teyit yok"
    elif item.confirmed and already_priced:
        item.price_note = f"Teyitli ama 24s'te %{stats['pct24']:.0f} oynamış — kısmen fiyatlanmış olabilir"
    elif item.confirmed:
        item.price_note = f"Haber + fiyat uyumlu (15dk %{move:+.1f}, 1s %{move60:+.1f})"
    else:
        item.price_note = f"Fiyat henüz haber yönünde oynamadı (15dk %{move:+.1f})"


# ── Bildirim ─────────────────────────────────────────────────────────────
_ARROW = {"bullish": "🟢 YÜKSELİŞ", "bearish": "🔴 DÜŞÜŞ", "neutral": "⚪ NÖTR"}


def _fmt_news_msg(item: NewsItem) -> str:
    """Güçlü haberi uzak kanal (Telegram/Discord) için düz metne çevir."""
    coins = ", ".join(item.coins) if item.coins else "Genel"
    tick = "✅ TEYİTLİ" if item.confirmed else "⏳ teyit yok"
    lines = [
        f"⚡ Güç {item.impact}/10 · {_ARROW[item.direction]} · {coins}",
        f"[{item.source}] {item.title[:200]}",
        tick + (f" · {item.price_note}" if item.price_note else ""),
    ]
    if item.reason:
        lines.append(f"💡 {item.reason[:200]}")
    if item.url:
        lines.append(f"🔗 {item.url}")
    return "\n".join(lines)


def _fmt_trade_msg(pos: dict[str, Any], opened: bool) -> str:
    """Oto-işlem açılış/kapanış olayını uzak kanal için düz metne çevir."""
    mode = pos.get("mode", "paper").upper()
    side = pos.get("side", "?").upper()
    sym = pos.get("symbol", "?")
    if opened:
        head = f"🤖 OTO İŞLEM AÇILDI [{mode}]"
        body = f"{side} {sym} · {pos.get('usdt', 0):.0f} USDT @ {pos.get('entry_price')}"
        sl, tp = pos.get("sl_price"), pos.get("tp_price")
        tail = f"SL {sl} · TP {tp}" if (sl or tp) else ""
    else:
        pnl, pct = pos.get("pnl"), pos.get("pnl_pct")
        emoji = "🟩" if (pnl or 0) >= 0 else "🟥"
        head = f"{emoji} POZİSYON KAPANDI [{mode}] · {pos.get('close_reason', '?')}"
        body = f"{side} {sym} @ {pos.get('close_price')}"
        if pnl is not None:
            tail = f"P&L {pnl:+.2f} USDT" + (f" ({pct:+.1f}%)" if pct is not None else "")
        else:
            tail = ""
    return "\n".join(x for x in (head, body, tail) if x)


def notify_remote(text: str) -> None:
    """Telegram/Discord'a gönder (kapalıysa veya env tanımsızsa sessizce atlanır)."""
    if not _news_settings.get("remote_notify", True):
        return
    try:
        _notifier.send(text)
    except Exception as e:
        log.warning("Uzak bildirim hatası: %s", e)


def _fmt_summary_msg(s: dict[str, Any]) -> str:
    """Günlük işlem özetini uzak kanal için düz metne çevir."""
    sign = "+" if s["realized"] >= 0 else ""
    return "\n".join([
        f"📊 Günlük özet · {s['date']}",
        f"İşlem: {s['trades']} ({s['wins']}K/{s['losses']}Z) · Realized: {sign}{s['realized']} USDT",
        f"En iyi/kötü: +{s['best']} / {s['worst']}",
        f"Açık: {s['open_positions']} pozisyon · {s['open_exposure_usdt']} USDT maruziyet",
    ])


# Gün dönümünde dünün özetini uzak kanaldan gönder (profesyonel end-of-day rapor).
_last_summary_date: str | None = None


def _maybe_daily_digest() -> None:
    """Tarih değiştiyse biten günün özetini gönder. Arka plan döngüsünden çağrılır."""
    global _last_summary_date
    today = trader._today()
    if _last_summary_date is None:
        _last_summary_date = today      # ilk tur: tetikleme yok
        return
    if today != _last_summary_date:
        prev, _last_summary_date = _last_summary_date, today
        try:
            summary = trader.daily_summary(prev)
            if summary["trades"] > 0:   # işlemsiz gün için özet gönderme
                notify_remote(_fmt_summary_msg(summary))
        except Exception as e:
            log.warning("Günlük özet hatası: %s", e)


# Ölü-adam anahtarı durumu (spam önleme: bir kez uyar, düzelince bir kez haber ver)
_ws_alert_active = False


def _ws_feed_stale(now: float | None = None) -> bool:
    """WS akışı (asıl kaynak) durmuş mu: kopuk VEYA son mesaj eşikten eski. Saf."""
    if not USE_TREENEWS:
        return False
    t = now if now is not None else time.time()
    if t - _started_at < WS_STALE_ALERT_SEC:
        return False   # başlangıç grace: ilk bağlantıya süre tanı
    if not _ws_state.get("connected"):
        return True
    age = _ws_last_msg_age(t)
    return age is not None and age > WS_STALE_ALERT_SEC


def _maybe_deadman_alert(now: float | None = None) -> None:
    """WS uzun süre kopuk/sessizse uzak kanaldan uyar; düzelince haber ver.
    Arka plan döngüsünden çağrılır (durum-makinesi → tek uyarı, tek toparlama)."""
    global _ws_alert_active
    stale = _ws_feed_stale(now)
    if stale and not _ws_alert_active:
        _ws_alert_active = True
        if not _ws_state.get("connected"):
            detail = "WS bağlantısı kopuk"
        else:
            mins = int((_ws_last_msg_age(now) or 0) / 60)
            detail = f"son mesaj {mins} dk önce"
        notify_remote(f"⚠️ HABER AKIŞI DURDU: {detail}. Gerçek-zamanlı sinyal "
                      "alınamıyor olabilir — motoru/bağlantıyı kontrol et.")
    elif not stale and _ws_alert_active:
        _ws_alert_active = False
        notify_remote("✅ Haber akışı geri geldi (WS bağlı, mesaj akıyor).")


def notify(item: NewsItem) -> None:
    """Güçlü haber için masaüstü (winotify) + uzak (Telegram/Discord) bildirim."""
    notify_remote(_fmt_news_msg(item))  # winotify yoksa bile uzak kanal çalışır

    try:
        from winotify import Notification, audio
    except ImportError:
        log.warning("winotify yok — masaüstü bildirimi atlanıyor (pip install winotify)")
        return

    arrow = _ARROW[item.direction]
    coins = ", ".join(item.coins) if item.coins else "Genel"
    tick = "✅ TEYİTLİ" if item.confirmed else "⏳ teyit yok"
    note = f"\n{tick}" + (f" · {item.price_note}" if item.price_note else "")
    toast = Notification(
        app_id=APP_ID,
        title=f"⚡ Güç {item.impact}/10 · {arrow} · {coins}",
        msg=f"[{item.source}] {item.title[:120]}{note}",
        duration="long",
    )
    toast.set_audio(audio.LoopingAlarm, loop=False)
    if item.url:
        toast.add_actions(label="Habere git", launch=item.url)
    toast.show()


# ── Filtreler: gürültü + yaş ─────────────────────────────────────────────
def _is_noise(item: NewsItem) -> bool:
    """Binance rutin duyurusu mu (perpetual/earn/margin/airdrop...) — işe yaramaz."""
    if "binance" in item.source.lower() and BINANCE_NOISE.search(item.title):
        return True
    return False


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _too_old(item: NewsItem) -> bool:
    t = _parse_time(item.published) or _parse_time(item.fetched_at)
    if t is None:
        return False
    return datetime.now(timezone.utc) - t > timedelta(hours=MAX_NEWS_AGE_HOURS)


def _prune_news() -> None:
    """Feed'de zaten duran gürültü/eski haberleri temizle."""
    with _cache_lock:
        kept = [n for n in _news if not _is_noise(n) and not _too_old(n)]
        if len(kept) != len(_news):
            _news[:] = kept


# ── Ortak işleme: puanla → teyit et → sakla → bildir/oto-işlem ───────────
MAX_CONFIRM_WORKERS = 8   # çoklu alert'te fiyat teyidi paralelliği (üst sınır)


def _confirm_one(session: requests.Session, it: NewsItem) -> None:
    try:
        confirm_with_price(session, it)
    except Exception as e:
        log.warning("Fiyat teyidi başarısız (%s): %s", it.symbol or it.coins, e)


def _confirm_alerts(session: requests.Session, alerts: list[NewsItem]) -> None:
    """Güçlü haberlerin fiyat teyidini paralel çalıştır (çoklu alert'te hız).

    Tek/sıfır alert'te inline; aksi halde sınırlı thread havuzu. Her teyit kendi
    item'ını yerinde günceller (paylaşımlı durum yok); hata diğerlerini etkilemez.
    """
    if not alerts:
        return
    if len(alerts) == 1:
        _confirm_one(session, alerts[0])
        return
    workers = min(len(alerts), MAX_CONFIRM_WORKERS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda it: _confirm_one(session, it), alerts))


def process_items(
    session: requests.Session,
    candidates: list[NewsItem],
    allow_notify: bool,
) -> tuple[int, int]:
    """Yeni adayları işle. (yeni_sayısı, güçlü_sayısı) döndürür."""
    new_items: list[NewsItem] = []
    with _cache_lock:
        for it in candidates:
            if it.id in _seen_ids:
                continue
            _seen_ids.add(it.id)
            if _is_noise(it) or _too_old(it):
                continue   # gürültü/eski: tekrar görmemek için işaretle ama saklama
            new_items.append(it)
    if not new_items:
        return 0, 0

    _load_news_settings()
    threshold = _news_settings["alert_threshold"]

    # Faz 1 — ANINDA kural puanı (Claude'un ağ gecikmesini beklemeden)
    for it in new_items:
        score_item(it)

    # Hemen sakla → panel/SSE gecikmesiz göstersin
    with _cache_lock:
        for it in new_items:
            _news.insert(0, it)
        del _news[MAX_NEWS_KEEP:]
        _status["updated_at"] = _now_iso()
        _status["error"] = None
        _status["total_seen"] = len(_seen_ids)

    # Erken heads-up: Claude gecikme ekleyeceği için kural-güçlülerini şimdi bildir
    notified: set[str] = set()
    if allow_notify and USE_CLAUDE:
        for it in new_items:
            if it.impact >= threshold:
                notify(it)
                notified.add(it.id)

    # Faz 2 — Claude ile rafine et (nihai skor); hata olursa kural skoru geçerli kalır
    if USE_CLAUDE:
        try:
            score_with_claude(new_items)
        except Exception as e:
            log.warning("Claude puanlama başarısız, kural skoru geçerli: %s", e)

    # Nihai güçlü haberler: teyit + arşiv + oto-işlem (para yolu nihai skorda)
    alerts = [it for it in new_items if it.impact >= threshold]
    _metrics["alerts_total"] += len(alerts)
    _confirm_alerts(session, alerts)   # paralel fiyat teyidi (çoklu alert'te hız)

    if allow_notify:
        for it in alerts:
            _archive_signal(it)
            if it.id not in notified:   # erken bildirilenleri tekrar bildirme
                notify(it)
            pos = trader.maybe_auto_trade(it)
            if pos:
                _metrics["trades_opened_total"] += 1
                log.info("OTO İŞLEM AÇILDI | %s %s | %s", pos["side"], pos["symbol"], pos["mode"])
                notify_remote(_fmt_trade_msg(pos, opened=True))

    return len(new_items), len(alerts)


_archive_count = 0


def _archive_signal(item: NewsItem) -> None:
    """Güçlü sinyali kalıcı arşive yaz (backtest için). Hata akışı bozmaz.

    Arşivin sınırsız büyümesini önlemek için periyodik olarak eski kayıtları budar.
    """
    global _archive_count
    try:
        store = get_store()
        if store.add_signal(item.to_dict()):
            _archive_count += 1
            if _archive_count % ARCHIVE_PRUNE_EVERY == 0:
                store.prune_signals(MAX_ARCHIVE_SIGNALS)
    except Exception as e:
        log.warning("Sinyal arşivleme hatası: %s", e)


# ── Arka plan döngüsü (RSS + Binance polling) ────────────────────────────
def refresh(session: requests.Session) -> None:
    global _primed
    try:
        fetched = fetch_all(session)
        first_run = not _primed
        _primed = True
        n_new, n_alert = process_items(session, fetched, allow_notify=not first_run)
        _prune_news()  # mevcut feed'den gürültü/eski haberleri temizle
        log.info(
            "Tarama | %d yeni haber | %d güçlü%s | toplam %d",
            n_new, n_alert,
            " (ilk tarama: bildirim yok)" if first_run else " uyarı bildirildi",
            len(_seen_ids),
        )
    except Exception as e:
        log.exception("Tarama hatası: %s", e)
        _metrics["scan_errors_total"] += 1
        with _cache_lock:
            _status["error"] = str(e)
            _status["updated_at"] = _now_iso()


def _background_loop(stop: threading.Event) -> None:
    session = requests.Session()
    session.headers.setdefault("User-Agent", "kripto-haber-bot/1.0")
    while not stop.is_set():
        refresh(session)
        _maybe_daily_digest()      # gün dönümünde dünün özetini gönder
        _maybe_deadman_alert()     # haber akışı durduysa uyar (ölü-adam anahtarı)
        if stop.wait(SCAN_INTERVAL_SEC):
            break


# Açık pozisyonları SL/TP/trailing için sık aralıkla izle
MONITOR_INTERVAL_SEC = 8


def _persist_closed(pos: dict[str, Any]) -> None:
    """Kapanan işlemi kalıcı deftere yaz (trade_state.json 500 sınırı dışı). Hata akışı bozmaz."""
    try:
        get_store().add_closed_news_trade(pos)
    except Exception as e:
        log.warning("Kapanan işlem arşivlenemedi: %s", e)


def _monitor_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            for pos in trader.monitor_positions():
                _persist_closed(pos)
                notify_remote(_fmt_trade_msg(pos, opened=False))
        except Exception as e:
            log.warning("Pozisyon izleme hatası: %s", e)
        if stop.wait(MONITOR_INTERVAL_SEC):
            break


# ── TreeNews WebSocket (gerçek zamanlı) ──────────────────────────────────
def parse_tree_message(raw: str) -> NewsItem | None:
    """TreeNews WS mesajını NewsItem'a çevir. Tanımadığı/haber olmayan mesajda None."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("title") or data.get("en") or data.get("body")
    if not title:
        return None
    title = str(title).strip()

    # TreeNews'te 'source' yok; 'type' (twitter/blogs/direct...) kaynak görevi görür
    src = data.get("source") or data.get("type") or "TreeNews"
    if isinstance(src, dict):
        src = src.get("name") or "TreeNews"
    url = str(data.get("url") or data.get("link") or "")
    tid = str(data.get("_id") or data.get("id") or "")

    published = None
    t = data.get("time")
    if isinstance(t, (int, float)):
        try:
            published = datetime.fromtimestamp(t / 1000, timezone.utc).isoformat()
        except (ValueError, OSError):
            published = None

    # Coin tespiti: 'coin' (tekil str), 'suggestions[].coin', 'symbols'/'coins' (liste)
    coins: list[str] = []
    c1 = data.get("coin")
    if isinstance(c1, str) and c1:
        coins.append(c1.upper())
    sugg = data.get("suggestions")
    if isinstance(sugg, list):
        for s in sugg:
            if isinstance(s, dict) and s.get("coin"):
                coins.append(str(s["coin"]).upper())
            elif isinstance(s, str) and s:
                coins.append(s.upper())
    for key in ("symbols", "coins"):
        v = data.get(key)
        if isinstance(v, list):
            coins.extend(str(c).upper() for c in v if c)
    coins = list(dict.fromkeys(coins))  # tekilleştir, sırayı koru

    item = NewsItem(
        id=_news_id(f"Tree:{src}", url or tid, title),
        source=f"⚡{src}",
        title=title,
        url=url,
        published=published,
        fetched_at=_now_iso(),
    )
    item.coins = coins  # ipucu; puanlayıcı gerekirse günceller
    return item


_WS_BACKOFF_BASE = 2.0
_WS_BACKOFF_MAX = 60.0
_WS_STABLE_SEC = 30.0   # bu kadar bağlı kaldıysa backoff sıfırlanır


def _next_backoff(prev: float) -> float:
    """Üstel backoff: 0/negatiften taban, aksi halde iki katı, tavanla sınırlı. Saf."""
    if prev <= 0:
        return _WS_BACKOFF_BASE
    return min(_WS_BACKOFF_MAX, prev * 2)


def _tree_ws_loop(stop: threading.Event) -> None:
    import websocket

    session = requests.Session()
    session.headers.setdefault("User-Agent", "kripto-haber-bot/1.0")
    connect_ts = [0.0]

    def on_open(ws: Any) -> None:
        connect_ts[0] = time.monotonic()
        _ws_state["connected"] = True
        log.info("TreeNews WebSocket bağlandı — gerçek zamanlı haber akışı açık")

    def on_message(ws: Any, message: str) -> None:
        _ws_state["last_msg_at"] = time.time()
        item = parse_tree_message(message)
        if item is None:
            return
        # Backfill koruması: bağlantının ilk saniyelerindeki mesajlar geçmiş olabilir
        allow = _primed and (time.monotonic() - connect_ts[0] > TREE_BACKFILL_GUARD_SEC)
        try:
            n_new, n_alert = process_items(session, [item], allow_notify=allow)
            if n_new and n_alert:
                log.info("⚡ TreeNews güçlü haber | %s | %s", item.source, item.title[:60])
        except Exception as e:
            log.warning("TreeNews işleme hatası: %s", e)

    def on_error(ws: Any, err: Any) -> None:
        log.warning("TreeNews WS hatası: %s", err)

    backoff = 0.0
    while not stop.is_set():
        started = time.monotonic()
        try:
            ws = websocket.WebSocketApp(
                TREE_WS, on_open=on_open, on_message=on_message, on_error=on_error,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log.warning("TreeNews WS hatası: %s", e)
        _ws_state["connected"] = False   # bağlantı düştü
        # Uzun süre bağlı kaldıysa backoff'u sıfırla; yoksa üstel büyüt (flood koruması)
        backoff = _WS_BACKOFF_BASE if time.monotonic() - started >= _WS_STABLE_SEC else _next_backoff(backoff)
        log.info("TreeNews WS yeniden bağlanılıyor (%.0fs)...", backoff)
        if stop.wait(backoff):
            break


_stop_event = threading.Event()
_bg_thread: threading.Thread | None = None
_ws_thread: threading.Thread | None = None
_mon_thread: threading.Thread | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_thread, _ws_thread, _mon_thread
    setup_logging()
    _load_news_settings()   # kalıcı eşik/bildirim ayarlarını yükle (restart'a dayanıklı)
    try:
        store = get_store()
        store.prune_signals(MAX_ARCHIVE_SIGNALS)   # arşivi sınırla (başlangıç budama)
        for t in trader.closed_trades(1000):       # trade_state.json geçmişini kalıcı deftere taşı
            store.add_closed_news_trade(t)
        rec = trader.reconcile_positions()         # canlı modda borsayla mutabakat (read-only)
        if rec.get("checked") and rec.get("orphans"):
            log.warning("Mutabakat: borsada bulunmayan %d yerel pozisyon (orphan): %s",
                        len(rec["orphans"]), [o["symbol"] for o in rec["orphans"]])
    except Exception as e:
        log.warning("Başlangıç arşiv işlemi hatası: %s", e)
    _stop_event.clear()
    _bg_thread = threading.Thread(target=_background_loop, args=(_stop_event,), daemon=True)
    _bg_thread.start()
    _mon_thread = threading.Thread(target=_monitor_loop, args=(_stop_event,), daemon=True)
    _mon_thread.start()
    if USE_TREENEWS:
        _ws_thread = threading.Thread(target=_tree_ws_loop, args=(_stop_event,), daemon=True)
        _ws_thread.start()
    log.info(
        "Haber motoru başladı | puanlama=%s | TreeNews=%s | SL/TP izleme=%ds | eşik=%d",
        f"Claude ({CLAUDE_MODEL})" if USE_CLAUDE else "kural-tabanlı",
        "açık" if USE_TREENEWS else "kapalı", MONITOR_INTERVAL_SEC, ALERT_THRESHOLD,
    )
    yield
    _stop_event.set()
    for th in (_bg_thread, _ws_thread, _mon_thread):
        if th:
            th.join(timeout=5)


# ── FastAPI ──────────────────────────────────────────────────────────────
app = FastAPI(title="Kripto Haber Trade", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:3000", "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Güvenlik başlıkları — saf JSON API için sıkı varsayılanlar. HSTS yalnızca
# HTTPS üzerinde etkilidir (HTTP'de zararsız).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


@app.middleware("http")
async def _security_headers(request: Any, call_next: Any) -> Any:
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


class NewsResponse(BaseModel):
    news: list[dict[str, Any]]
    updated_at: str | None
    error: str | None
    total_seen: int
    alert_threshold: int


@app.get("/news", response_model=NewsResponse)
def get_news(limit: int = 100, min_impact: int = 0) -> NewsResponse:
    with _cache_lock:
        rows = [n.to_dict() for n in _news if n.impact >= min_impact][:limit]
        st = dict(_status)
    return NewsResponse(
        news=rows,
        updated_at=st["updated_at"],
        error=st["error"],
        total_seen=st["total_seen"],
        alert_threshold=st["alert_threshold"],
    )


@app.get("/alerts", response_model=NewsResponse)
def get_alerts(limit: int = 50) -> NewsResponse:
    return get_news(limit=limit, min_impact=get_news_settings()["alert_threshold"])


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness probe — süreç ayakta mı. Her zaman 200; bağımlılık kontrolü yok."""
    return {"ok": True}


STREAM_INTERVAL_SEC = 2.0   # SSE sunucu-tarafı tarama aralığı (gerçek zamanlıya yakın)


def _stream_diff(snapshot: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    """Snapshot'ta daha önce gönderilmemiş (id'si seen'de olmayan) haberleri döndür.

    Saf fonksiyon — `seen` mutasyona uğratılmaz (çağıran günceller). En eskiden
    yeniye sırayla döner ki SSE istemcisi doğru sırada eklesin.
    """
    fresh = [n for n in snapshot if n.get("id") not in seen]
    return list(reversed(fresh))   # snapshot en-yeni-başta; istemciye en-eski-önce


@app.get("/stream")
async def stream() -> StreamingResponse:
    """Yeni güçlü haberleri SSE ile push'la (15s polling yerine gerçek zamanlıya yakın)."""
    async def gen() -> Any:
        seen: set[str] = set()
        primed = False
        while True:
            with _cache_lock:
                snapshot = [n.to_dict() for n in _news[:50]]
            if not primed:
                seen = {n["id"] for n in snapshot}
                primed = True
                yield ": bağlandı\n\n"
            else:
                fresh = _stream_diff(snapshot, seen)
                for n in fresh:
                    seen.add(n["id"])
                    yield f"data: {json.dumps(n, ensure_ascii=False)}\n\n"
                if not fresh:
                    yield ": ping\n\n"   # bağlantıyı canlı tut
            await asyncio.sleep(STREAM_INTERVAL_SEC)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_METRIC_META = {
    "botpy_uptime_seconds": ("gauge", "Süreç uptime (saniye)"),
    "botpy_news_seen_total": ("counter", "Görülen toplam haber (dedupe sonrası)"),
    "botpy_news_in_feed": ("gauge", "Bellekteki haber sayısı"),
    "botpy_alerts_total": ("counter", "Eşik üstü güçlü haber"),
    "botpy_trades_opened_total": ("counter", "Otomatik açılan pozisyon"),
    "botpy_scan_errors_total": ("counter", "Arka plan tarama hatası"),
    "botpy_open_positions": ("gauge", "Açık pozisyon sayısı"),
    "botpy_signals_archived": ("gauge", "Arşivlenmiş sinyal sayısı"),
    "botpy_ws_connected": ("gauge", "TreeNews WS bağlı mı (1/0)"),
    "botpy_ws_last_msg_age_seconds": ("gauge", "Son WS mesajından bu yana saniye"),
    "botpy_rate_limited_total": ("counter", "Binance 429/418 rate-limit yanıtı"),
    "botpy_http_retries_total": ("counter", "Dış API yeniden deneme sayısı"),
}


def _render_metrics(values: dict[str, int | float]) -> str:
    """Prometheus exposition formatı (saf)."""
    lines = []
    for name, (mtype, help_text) in _METRIC_META.items():
        if name not in values:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        lines.append(f"{name} {values[name]}")
    return "\n".join(lines) + "\n"


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    """Prometheus-uyumlu sayaç/gauge metrikleri (gözlemlenebilirlik)."""
    with _cache_lock:
        seen, in_feed = len(_seen_ids), len(_news)
    try:
        archived = get_store().signal_span()["count"]
    except Exception:
        archived = 0
    values: dict[str, int | float] = {
        "botpy_uptime_seconds": int(time.time() - _started_at),
        "botpy_news_seen_total": seen,
        "botpy_news_in_feed": in_feed,
        "botpy_alerts_total": _metrics["alerts_total"],
        "botpy_trades_opened_total": _metrics["trades_opened_total"],
        "botpy_scan_errors_total": _metrics["scan_errors_total"],
        "botpy_open_positions": len(trader._positions),
        "botpy_signals_archived": archived,
        "botpy_ws_connected": 1 if _ws_state["connected"] else 0,
    }
    age = _ws_last_msg_age()
    if age is not None:
        values["botpy_ws_last_msg_age_seconds"] = age
    net = get_stats()
    values["botpy_rate_limited_total"] = net["rate_limited"]
    values["botpy_http_retries_total"] = net["retries"]
    return PlainTextResponse(_render_metrics(values), media_type="text/plain; version=0.0.4")


@app.get("/health")
def health() -> dict[str, Any]:
    with _cache_lock:
        st = dict(_status)
    try:
        archived = get_store().signal_span()["count"]
    except Exception:
        archived = None
    return {
        "ok": st["error"] is None,
        **st,
        "uptime_sec": int(time.time() - _started_at),
        "scorer": "claude" if USE_CLAUDE else "rule",
        "treenews": USE_TREENEWS,
        "ws_connected": _ws_state["connected"],
        "ws_last_msg_age_sec": _ws_last_msg_age(),
        "feed_stale": _ws_feed_stale(),
        "rate_limited": get_stats()["rate_limited"],
        "signals_archived": archived,
    }


@app.get("/risk")
def risk() -> dict[str, Any]:
    """Anlık risk/maruziyet özeti (limitler, kullanım, günlük zarar, kill-switch)."""
    return trader.get_risk()


@app.get("/summary")
def summary(date: str | None = None) -> dict[str, Any]:
    """Günlük işlem özeti (varsayılan bugün): işlem sayısı, realized, en iyi/kötü, açık."""
    return trader.daily_summary(date)


@app.get("/reconcile")
def reconcile() -> dict[str, Any]:
    """Yerel açık pozisyonları borsayla karşılaştır (canlı; read-only, auto-close yok)."""
    return trader.reconcile_positions()


@app.get("/auto-preview")
def auto_preview(limit: int = 20) -> dict[str, Any]:
    """Mevcut güçlü haberler için oto-işlem kararı önizlemesi (çalıştırmadan).

    Her haber için hangi gerekçeyle işlem açılır/açılmaz ve hangi boyutta — config
    kalibrasyonu için. Global oto-işlem kapalı olsa da değerlendirir (yan etkisiz).
    """
    threshold = get_news_settings()["alert_threshold"]
    with _cache_lock:
        items = [n for n in _news if n.impact >= threshold][:limit]
    preview = []
    for it in items:
        d = trader.auto_decision(it)
        preview.append({
            "id": it.id, "title": it.title[:80], "symbol": it.symbol,
            "impact": it.impact, "direction": it.direction,
            "would_trade": d["would_trade"], "reason": d["reason"],
            "side": d["side"], "usdt": d["usdt"],
        })
    return {"preview": preview, "auto_trade_on": trader.S.auto_trade}


def _closed_trades(limit: int) -> list[dict[str, Any]]:
    """Kalıcı arşivden kapanan işlemler; arşiv boşsa in-memory deftere düş."""
    try:
        rows = get_store().list_closed_news_trades(limit)
        if rows:
            return rows
    except Exception as e:
        log.warning("Kapanan işlem arşivi okunamadı: %s", e)
    return trader.closed_trades(limit)


@app.get("/trades/closed")
def trades_closed(limit: int = 200) -> dict[str, Any]:
    """Kapanan işlemler (kalıcı defter, en yeniden eskiye)."""
    return {"trades": _closed_trades(limit)}


_CSV_FIELDS = (
    "closed_at", "opened_at", "symbol", "side", "mode", "usdt", "entry_price",
    "close_price", "pnl", "pnl_pct", "close_reason", "source",
)


@app.get("/trades/closed.csv")
def trades_closed_csv(limit: int = 1000) -> PlainTextResponse:
    """Kapanan işlemleri CSV olarak dışa aktar (işlem günlüğü / vergi-rapor)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for t in _closed_trades(limit):
        writer.writerow(t)
    headers = {"Content-Disposition": 'attachment; filename="closed_trades.csv"'}
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers=headers)


class NewsSettingsPatch(BaseModel):
    alert_threshold: int | None = None   # 1-10; bu güç ve üstü = uyarı/işlem
    remote_notify: bool | None = None    # Telegram/Discord push aç/kapat


@app.get("/news-settings")
def news_settings_get() -> dict[str, Any]:
    return get_news_settings()


@app.patch("/news-settings", dependencies=[Depends(require_token)])
def news_settings_patch(body: NewsSettingsPatch) -> dict[str, Any]:
    return update_news_settings(body.model_dump(exclude_none=True))


class RssFeedsPatch(BaseModel):
    feeds: dict[str, str]    # {ad: url} — yalnızca http(s) kabul edilir


@app.get("/news-sources")
def news_sources() -> dict[str, Any]:
    """Efektif RSS haber kaynakları (ad → url)."""
    return {"rss_feeds": get_rss_feeds()}


@app.patch("/news-sources", dependencies=[Depends(require_token)])
def news_sources_patch(body: RssFeedsPatch) -> dict[str, Any]:
    """RSS kaynaklarını değiştir (store'da kalıcı; yalnızca http(s))."""
    return {"rss_feeds": set_rss_feeds(body.feeds)}


@app.get("/signals")
def get_signals(limit: int = 500, min_impact: int = 0) -> dict[str, Any]:
    """Kalıcı arşivdeki güçlü haber sinyalleri (restart'tan bağımsız, backtest için)."""
    store = get_store()
    return {"signals": store.list_signals(limit=limit, min_impact=min_impact),
            **store.signal_span()}


# Ağ-yoğun uçlar (backtest/scorecard) aynı anda tek koşsun — Binance'i yormamak
# ve istek yığılmasını önlemek için. İkinci eşzamanlı istek 409 alır.
_heavy_lock = threading.Lock()


@contextmanager
def _heavy_guard() -> Any:
    if not _heavy_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Ağır işlem (backtest/scorecard) zaten çalışıyor — bekleyin")
    try:
        yield
    finally:
        _heavy_lock.release()


@app.get("/scorecard")
def scorecard(hours: float = 4.0, min_impact: int = ALERT_THRESHOLD, limit: int = 300) -> dict[str, Any]:
    """Ham sinyal kalitesi: arşiv sinyallerinin gerçekleşen yön isabeti (SL/TP'siz).

    Binance klines indirir (senkron, threadpool). İşlem simüle etmez — sadece
    haber yönünün fiyatla uyumunu kaynak/güç bazında ölçer.
    """
    import news_backtest as nbt
    with _heavy_guard():
        rows = get_store().list_signals(limit=limit, min_impact=min_impact)
        candidates = nbt._signals_from_rows(rows)
        if not candidates:
            return {"ok": False, "reason": "yeterli sinyal yok (arşiv boş veya çok yeni)", "n": 0}
        signals = nbt.prefetch(candidates, int(hours * 60))
        if not signals:
            return {"ok": False, "reason": "fiyat verisi indirilemedi (Binance)", "n": 0}
        return {"ok": True, **nbt.signal_scorecard(signals)}


def _run_backtest_impl(
    sl: float = 3.0, tp: float = 6.0, fee: float = 0.2, usdt: float = 100.0,
    hours: float = 4.0, min_impact: int = ALERT_THRESHOLD, limit: int = 300,
    mode: str = "simple", train_frac: float = 0.7,
    slip: float = 0.0, entry_delay: int = 0,
) -> dict[str, Any]:
    """Arşivlenmiş sinyaller üzerinde backtest koşar.

    Her sinyal için Binance geçmiş klines indirip çıkışı simüle eder (komisyon +
    `slip` bacak-başı kayma %% + `entry_delay` dk gecikmeli giriş = canlı-gerçekçilik).
    `mode`: "simple" (tek SL/TP), "smart" (akıllı çıkış / preset), "grid" (en kârlı
    SL/TP araması), "walk" (walk-forward overfit testi). Ağ gerektirir; senkron çalışır
    (FastAPI bunu threadpool'da koşturur, olay döngüsünü bloklamaz).
    """
    import news_backtest as nbt

    rows = get_store().list_signals(limit=limit, min_impact=min_impact)
    candidates = nbt._signals_from_rows(rows)
    if not candidates:
        return {"ok": False, "reason": "yeterli sinyal yok (arşiv boş veya sinyaller çok yeni)", "n": 0}
    signals = nbt.prefetch(candidates, int(hours * 60))
    if not signals:
        return {"ok": False, "reason": "fiyat verisi indirilemedi (Binance)", "n": 0}

    common: dict[str, Any] = {"fee": fee, "usdt": usdt, "hours": hours, "min_impact": min_impact}

    if mode == "walk":
        wf = nbt.walk_forward(signals, train_frac=train_frac, fee=fee, usdt=usdt, min_trades=3)
        wf["mode"] = "walk"
        wf["tested"] = len(signals)
        if wf.get("ok") and wf.get("out_of_sample"):
            p, oos = wf.get("params") or {}, wf["out_of_sample"]
            _persist_backtest("walk", sl=p.get("sl"), tp=p.get("tp"), stats=oos,
                              note=wf.get("verdict", ""), **common)
        return wf

    if mode == "grid":
        grid = nbt.grid_search(signals, fee, usdt)
        best = grid[0] if grid else None
        if best:
            _persist_backtest("grid", sl=best["sl"], tp=best["tp"], stats=best,
                              note="grid en kârlı", **common)
        return {"ok": True, "mode": "grid", "tested": len(signals),
                "rows": grid, "best": best}

    if mode == "smart":
        # Mevcut çıkış ayarlarını (preset dahil) arşiv üzerinde simüle et
        params = {
            "sl_pct": trader.S.stop_loss_pct, "tp_pct": trader.S.take_profit_pct,
            "breakeven_pct": trader.S.breakeven_pct, "partial_tp_pct": trader.S.partial_tp_pct,
            "partial_tp_frac": trader.S.partial_tp_frac, "trailing_stop_pct": trader.S.trailing_stop_pct,
            "time_stop_min": trader.S.time_stop_min,
            "slip_pct": slip, "entry_delay_min": entry_delay,
        }
        results = nbt.simulate_smart_all(signals, params, fee)
        summary = nbt._summarize(results, usdt)
        summary["ok"] = True
        summary["mode"] = "smart"
        summary["tested"] = len(signals)
        summary["params"] = params
        summary["breakdown"] = nbt.breakdown(results, usdt)
        if summary.get("n"):
            _persist_backtest("smart", sl=params["sl_pct"], tp=params["tp_pct"],
                              stats=summary, note="akıllı çıkış (mevcut ayarlar)", **common)
        return summary

    results = nbt.simulate_all(signals, sl, tp, fee, slip_pct=slip, entry_delay_min=entry_delay)
    summary = nbt._summarize(results, usdt)
    summary["ok"] = True
    summary["mode"] = "simple"
    summary["tested"] = len(signals)
    summary["candidates"] = len(candidates)
    summary["breakdown"] = nbt.breakdown(results, usdt)
    if summary.get("n"):
        _persist_backtest("simple", sl=sl, tp=tp, stats=summary, note="", **common)
    return summary


@app.get("/backtest")
def run_backtest(
    sl: float = 3.0, tp: float = 6.0, fee: float = 0.2, usdt: float = 100.0,
    hours: float = 4.0, min_impact: int = ALERT_THRESHOLD, limit: int = 300,
    mode: str = "simple", train_frac: float = 0.7,
    slip: float = 0.0, entry_delay: int = 0,
) -> dict[str, Any]:
    """Backtest çalıştır (ağ-yoğun; aynı anda tek koşar — bkz `_heavy_guard`)."""
    with _heavy_guard():
        return _run_backtest_impl(sl=sl, tp=tp, fee=fee, usdt=usdt, hours=hours,
                                  min_impact=min_impact, limit=limit, mode=mode,
                                  train_frac=train_frac, slip=slip, entry_delay=entry_delay)


def _persist_backtest(mode: str, *, sl: float | None, tp: float | None, fee: float,
                      usdt: float, hours: float, min_impact: int,
                      stats: dict[str, Any], note: str) -> None:
    """Bir backtest özetini arşive yaz (karşılaştırma için). Hata akışı bozmaz."""
    try:
        get_store().add_backtest_run({
            "mode": mode, "sl": sl, "tp": tp, "fee": fee, "usdt": usdt,
            "hours": hours, "min_impact": min_impact,
            "n": stats.get("n"), "win_rate": stats.get("win_rate"),
            "avg_net_pct": stats.get("avg_net_pct"),
            "total_pnl_usdt": stats.get("total_pnl_usdt"), "note": note,
        })
    except Exception as e:
        log.warning("Backtest kaydı yazılamadı: %s", e)


@app.get("/backtest/runs")
def backtest_runs(limit: int = 50) -> dict[str, Any]:
    """Geçmiş backtest çalıştırmaları (en yeniden eskiye, karşılaştırma için)."""
    return {"runs": get_store().list_backtest_runs(limit)}


# ── İşlem endpoint'leri ──────────────────────────────────────────────────
class TradeRequest(BaseModel):
    symbol: str | None = None       # örn. BTCUSDT
    coin: str | None = None         # ya da sadece BTC (USDT eklenir)
    side: str = "long"              # long/buy | short/sell
    usdt: float | None = None       # boşsa ayarlardaki trade_usdt


class SettingsPatch(BaseModel):
    paper_trading: bool | None = None
    auto_trade: bool | None = None
    market: str | None = None
    trade_usdt: float | None = None
    leverage: int | None = None
    max_positions: int | None = None
    auto_min_impact: int | None = None
    auto_require_confirm: bool | None = None
    tier1_skip_confirm_impact: int | None = None
    cooldown_sec: int | None = None
    use_sl_tp: bool | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    daily_loss_limit_usdt: float | None = None
    max_total_exposure_usdt: float | None = None
    max_per_coin_usdt: float | None = None
    order_type: str | None = None
    slippage_guard_pct: float | None = None
    min_orderbook_usd: float | None = None
    size_by_impact: bool | None = None
    time_stop_min: int | None = None
    breakeven_pct: float | None = None
    partial_tp_pct: float | None = None
    partial_tp_frac: float | None = None
    max_open_risk_usdt: float | None = None
    reduce_after_losses: int | None = None
    suppress_losing_sources: bool | None = None
    min_source_samples: int | None = None
    skip_already_priced_pct: float | None = None


@app.get("/settings")
def get_trade_settings() -> dict[str, Any]:
    return trader.get_settings()


@app.patch("/settings", dependencies=[Depends(require_token)])
def patch_trade_settings(body: SettingsPatch) -> dict[str, Any]:
    try:
        return trader.update_settings(body.model_dump(exclude_none=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/settings/preset/{name}", dependencies=[Depends(require_token)])
def apply_settings_preset(name: str) -> dict[str, Any]:
    """Çıkış preset'i uygula: 'news' (haber-trade: hızlı breakeven + erken kısmi TP +
    trailing + time-stop + tier-1 refleks) veya 'safe' (muhafazakâr varsayılan)."""
    try:
        return trader.apply_preset(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/trade", dependencies=[Depends(require_token)])
def post_trade(body: TradeRequest) -> dict[str, Any]:
    symbol = body.symbol or (f"{body.coin.upper()}USDT" if body.coin else None)
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol veya coin gerekli")
    try:
        return trader.place_trade(symbol, body.side, body.usdt, source="manual")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/positions")
def get_positions() -> dict[str, Any]:
    positions, total = trader.get_positions()
    return {"positions": positions, "total_pnl": total}


@app.get("/performance")
def get_performance() -> dict[str, Any]:
    return trader.get_performance()


@app.get("/tuning")
def get_tuning() -> dict[str, Any]:
    """Öğrenen beyin (öneri modu): kapanan GERÇEK işlemlerden eşik ayarı önerileri.
    Otomatik UYGULAMAZ — kaynak-tier eşlemesi için `_source_tier` geçirilir."""
    return trader.suggest_tuning(tier_of=_source_tier)


@app.get("/tuning/pretrade")
def get_tuning_pretrade(
    hours: float = 4.0, min_impact: int = ALERT_THRESHOLD, limit: int = 1000,
    fee: float = 0.2, slip: float = 0.1, entry_delay: int = 1,
) -> dict[str, Any]:
    """İşlemsiz ÖN-BİLGİ: arşivlenmiş sinyalleri gerçekçi maliyetlerle (slippage +
    gecikmeli giriş) backtest edip eşik önerileri çıkarır — gerçek para riske atmadan
    kalibrasyon, sistem ilk işlemden itibaren akıllı. Ağ-yoğun (aynı anda tek koşar)."""
    import news_backtest as nbt
    with _heavy_guard():
        rows = get_store().list_signals(limit=limit, min_impact=min_impact)
        candidates = nbt._signals_from_rows(rows)
        if not candidates:
            return {"ready": False, "reason": "arşiv boş veya sinyaller çok yeni",
                    "samples": 0, "suggestions": [], "pretrade": True}
        signals = nbt.prefetch(candidates, int(hours * 60))
        if not signals:
            return {"ready": False, "reason": "fiyat verisi indirilemedi (Binance)",
                    "samples": 0, "suggestions": [], "pretrade": True}
        results = nbt.simulate_all(signals, trader.S.stop_loss_pct, trader.S.take_profit_pct,
                                   fee, slip_pct=slip, entry_delay_min=entry_delay)
        out = trader.suggest_from_backtest(results, tier_of=_source_tier)
        out["tested"] = len(signals)
        return out


class PositionPatch(BaseModel):
    sl_price: float | None = None   # 0/negatif = SL kaldır
    tp_price: float | None = None   # 0/negatif = TP kaldır


@app.patch("/positions/{pid}", dependencies=[Depends(require_token)])
def patch_position(pid: str, body: PositionPatch) -> dict[str, Any]:
    """Açık pozisyonun SL/TP'sini güncelle (canlı yönetim)."""
    try:
        return trader.update_position(pid, sl_price=body.sl_price, tp_price=body.tp_price)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/positions/close-all", dependencies=[Depends(require_token)])
def close_all_positions() -> dict[str, Any]:
    """ACİL: tüm açık pozisyonları kapat (flatten). Detaylı rapor döner."""
    report = trader.close_all(reason="acil-toplu")
    for c in report["closed"]:
        _persist_closed(c)
    if report["closed"]:
        notify_remote(
            f"⛔ TÜMÜ KAPATILDI: {report['count']} pozisyon · P&L "
            f"{report['total_pnl']:+.2f} USDT"
            + (f" · {report['failed']} hata" if report["failed"] else "")
        )
    return report


@app.delete("/positions/{pid}", dependencies=[Depends(require_token)])
def delete_position(pid: str) -> dict[str, Any]:
    try:
        closed = trader.close_position(pid)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    _persist_closed(closed)
    return closed


def main() -> None:
    parser = argparse.ArgumentParser(description="Kripto haber-trade uyarı motoru")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cli", action="store_true", help="Sadece konsol (API yok)")
    args = parser.parse_args()

    if args.cli:
        setup_logging()
        log.info(
            "CLI modu | TreeNews=%s | eşik=%d | RSS tarama=%ds",
            "açık" if USE_TREENEWS else "kapalı", ALERT_THRESHOLD, SCAN_INTERVAL_SEC,
        )
        session = requests.Session()
        session.headers.setdefault("User-Agent", "kripto-haber-bot/1.0")
        stop = threading.Event()
        threading.Thread(target=_monitor_loop, args=(stop,), daemon=True).start()
        if USE_TREENEWS:
            threading.Thread(target=_tree_ws_loop, args=(stop,), daemon=True).start()
        try:
            while True:
                refresh(session)
                if stop.wait(SCAN_INTERVAL_SEC):
                    break
        except KeyboardInterrupt:
            log.info("Durduruldu.")
        return

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
