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
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import trader

load_dotenv()  # .env dosyasındaki ANTHROPIC_API_KEY'i okur

# ── Ayarlar ──────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 20      # saniye — kaynaklar ne sıklıkta taransın
ALERT_THRESHOLD   = 7       # bu güç (1-10) ve üstü = bildirim at
MAX_NEWS_KEEP     = 300     # bellekte tutulacak haber sayısı
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
CONFIRM_MOVE_PCT = 0.5         # son 15dk'da haber yönünde en az bu % hareket = teyit
ALREADY_PRICED_PCT = 25.0      # 24s'te bu % üzeri hareket = büyük kısmı fiyatlanmış
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


def fetch_all(session: requests.Session) -> list[NewsItem]:
    """Tüm kaynakları çek; biri patlarsa diğerleri devam etsin."""
    out: list[NewsItem] = []
    for name, url in RSS_FEEDS.items():
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
    "verilecek. Her başlık için tek bir JSON kaydı üret:\n"
    "- index: başlığın numarası\n"
    "- coins: etkilenen coin ticker'ları (örn. ['BTC','SOL']); net coin yoksa boş liste\n"
    "- impact: 1-10 piyasa etkisi (10 = piyasayı anında sert hareket ettirir: hack, "
    "iflas, ETF onayı, büyük borsa listelemesi, yasak, dava; 1 = önemsiz/genel yorum)\n"
    "- direction: 'bullish' (fiyatı yukarı), 'bearish' (aşağı) veya 'neutral'\n"
    "- reason: en fazla 12 kelimelik Türkçe gerekçe\n"
    "Sadece istenen yapıyı döndür."
)


# Tek Claude isteğinde puanlanacak haber sayısı. Küçük tut ki çıktı token
# sınırına (max_tokens) sığsın — büyük gruplarda JSON kesilir.
CLAUDE_BATCH = 25


def _score_chunk(client: Any, chunk: list[NewsItem]) -> None:
    listing = "\n".join(f"{i}. [{it.source}] {it.title}" for i, it in enumerate(chunk))
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
    for start in range(0, len(items), CLAUDE_BATCH):
        chunk = items[start:start + CLAUDE_BATCH]
        try:
            _score_chunk(client, chunk)
        except Exception as e:
            log.warning("Claude grup puanlama başarısız (kural-tabanlı): %s", e)
            for it in chunk:
                if it.scorer != "claude":
                    score_item(it)


# ── Fiyat teyidi (Binance public) ────────────────────────────────────────
def _fetch_symbol_stats(session: requests.Session, symbol: str) -> dict[str, float] | None:
    """Bir parite için 24s değişim, hacim ve son 15dk hareketini döndür."""
    r = session.get(f"{BINANCE_API}/ticker/24hr", params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        return None
    t = r.json()
    k = session.get(
        f"{BINANCE_API}/klines",
        params={"symbol": symbol, "interval": "5m", "limit": 3},
        timeout=REQUEST_TIMEOUT,
    )
    move15 = 0.0
    if k.status_code == 200:
        candles = k.json()
        if candles:
            o = float(candles[0][1]); c = float(candles[-1][4])
            if o:
                move15 = (c - o) / o * 100
    return {
        "pct24": float(t.get("priceChangePercent", 0) or 0),
        "vol": float(t.get("quoteVolume", 0) or 0),
        "move15": move15,
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
    item.volume_usd = stats["vol"]

    liq_ok = stats["vol"] >= MIN_VOLUME_USD
    move = stats["move15"]
    if item.direction == "bullish":
        dir_match = move >= CONFIRM_MOVE_PCT
    elif item.direction == "bearish":
        dir_match = move <= -CONFIRM_MOVE_PCT
    else:
        dir_match = False

    already_priced = abs(stats["pct24"]) >= ALREADY_PRICED_PCT
    item.confirmed = bool(dir_match and liq_ok)

    if not liq_ok:
        item.price_note = f"Düşük likidite (24s hacim ${stats['vol']:,.0f}) — slippage riski"
    elif item.direction == "neutral":
        pass  # not yukarıda set edildi
    elif item.confirmed and already_priced:
        item.price_note = f"Teyitli ama 24s'te %{stats['pct24']:.0f} oynamış — kısmen fiyatlanmış olabilir"
    elif item.confirmed:
        item.price_note = f"Haber + fiyat uyumlu (15dk %{move:+.1f})"
    else:
        item.price_note = f"Fiyat henüz haber yönünde oynamadı (15dk %{move:+.1f})"


# ── Bildirim ─────────────────────────────────────────────────────────────
def notify(item: NewsItem) -> None:
    """Güçlü haber için Windows masaüstü bildirimi."""
    try:
        from winotify import Notification, audio
    except ImportError:
        log.warning("winotify yok — bildirim atlanıyor (pip install winotify)")
        return

    arrow = {"bullish": "🟢 YÜKSELİŞ", "bearish": "🔴 DÜŞÜŞ", "neutral": "⚪ NÖTR"}[item.direction]
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

    # Puanla — Claude varsa akıllı, yoksa kural-tabanlı
    if USE_CLAUDE:
        try:
            score_with_claude(new_items)
        except Exception as e:
            log.warning("Claude puanlama başarısız, kural-tabanlıya dönülüyor: %s", e)
            for it in new_items:
                if it.scorer != "claude":
                    score_item(it)
    else:
        for it in new_items:
            score_item(it)

    # Güçlü haberleri Binance fiyat hareketiyle teyit et
    alerts = [it for it in new_items if it.impact >= ALERT_THRESHOLD]
    for it in alerts:
        try:
            confirm_with_price(session, it)
        except Exception as e:
            log.warning("Fiyat teyidi başarısız (%s): %s", it.symbol or it.coins, e)

    with _cache_lock:
        for it in new_items:
            _news.insert(0, it)
        del _news[MAX_NEWS_KEEP:]
        _status["updated_at"] = _now_iso()
        _status["error"] = None
        _status["total_seen"] = len(_seen_ids)

    if allow_notify:
        for it in alerts:
            notify(it)
            pos = trader.maybe_auto_trade(it)
            if pos:
                log.info("OTO İŞLEM AÇILDI | %s %s | %s", pos["side"], pos["symbol"], pos["mode"])

    return len(new_items), len(alerts)


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
        with _cache_lock:
            _status["error"] = str(e)
            _status["updated_at"] = _now_iso()


def _background_loop(stop: threading.Event) -> None:
    session = requests.Session()
    session.headers.setdefault("User-Agent", "kripto-haber-bot/1.0")
    while not stop.is_set():
        refresh(session)
        if stop.wait(SCAN_INTERVAL_SEC):
            break


# Açık pozisyonları SL/TP/trailing için sık aralıkla izle
MONITOR_INTERVAL_SEC = 8


def _monitor_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            trader.monitor_positions()
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


def _tree_ws_loop(stop: threading.Event) -> None:
    import websocket

    session = requests.Session()
    session.headers.setdefault("User-Agent", "kripto-haber-bot/1.0")
    connect_ts = [0.0]

    def on_open(ws: Any) -> None:
        connect_ts[0] = time.monotonic()
        log.info("TreeNews WebSocket bağlandı — gerçek zamanlı haber akışı açık")

    def on_message(ws: Any, message: str) -> None:
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

    while not stop.is_set():
        try:
            ws = websocket.WebSocketApp(
                TREE_WS, on_open=on_open, on_message=on_message, on_error=on_error,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log.warning("TreeNews WS yeniden bağlanıyor: %s", e)
        if stop.wait(5):
            break


_stop_event = threading.Event()
_bg_thread: threading.Thread | None = None
_ws_thread: threading.Thread | None = None
_mon_thread: threading.Thread | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_thread, _ws_thread, _mon_thread
    setup_logging()
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
    return get_news(limit=limit, min_impact=ALERT_THRESHOLD)


@app.get("/health")
def health() -> dict[str, Any]:
    with _cache_lock:
        return {"ok": _status["error"] is None, **_status}


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


@app.get("/settings")
def get_trade_settings() -> dict[str, Any]:
    return trader.get_settings()


@app.patch("/settings")
def patch_trade_settings(body: SettingsPatch) -> dict[str, Any]:
    try:
        return trader.update_settings(body.model_dump(exclude_none=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/trade")
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


@app.delete("/positions/{pid}")
def delete_position(pid: str) -> dict[str, Any]:
    try:
        return trader.close_position(pid)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


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
