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

import latency
import sourcehealth
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
# Failover: asıl gerçek-zamanlı kaynak (TreeNews WS) bayatlayınca RSS/Binance
# yedeği bu HIZLI aralıkta tarar (20s yerine) → kopuk realtime'da boşluğu doldur.
SCAN_INTERVAL_FAST_SEC = int(os.environ.get("SCAN_INTERVAL_FAST_SEC", "5"))
# Yedek kaynak sağlığı: üst üste bu kadar hata veren kaynağı geçici devre dışı bırak.
SOURCE_FAIL_THRESHOLD = int(os.environ.get("SOURCE_FAIL_THRESHOLD", "3"))
SOURCE_COOLDOWN_SEC = float(os.environ.get("SOURCE_COOLDOWN_SEC", "300"))
# Gecikme kalıcılığı: periyodik olarak latency özetini arşivle (restart'a dayanıklı trend).
LATENCY_SNAPSHOT_EVERY_SEC = float(os.environ.get("LATENCY_SNAPSHOT_EVERY_SEC", "300"))
MAX_LATENCY_SNAPSHOTS = int(os.environ.get("MAX_LATENCY_SNAPSHOTS", "10000"))
# Operasyonel olay zaman çizelgesi: incident günlüğünde tutulacak max kayıt.
MAX_OPS_EVENTS = int(os.environ.get("MAX_OPS_EVENTS", "5000"))
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
# RVOL (göreceli hacim): son mumun hacmi, önceki mumların ortalamasının kaç katı.
# Haber + fiyat + HACİM birlikte patlıyorsa haber gerçek; hacimsiz hareket = fake.
# Teyit penceresiyle aynı mum aralığı; baseline için bu kadar mum çekilir.
RVOL_LOOKBACK = int(os.environ.get("RVOL_LOOKBACK", "48"))  # 48×15dk ≈ 12s baz çizgi
# Binance USDT paritesi olmayan/olağan dışı coinler için stop listesi
_NOT_TRADEABLE = {"USDT", "USDC", "USD", "FDUSD", "TRY", "AED", "OPENAI", "ANTHROPIC"}

# RSS kaynakları — ücretsiz, anahtar gerekmez
RSS_FEEDS: dict[str, str] = {
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt":       "https://decrypt.co/feed",
    "TheBlock":      "https://www.theblock.co/rss.xml",
    "BMag":          "https://bitcoinmagazine.com/feed",
    # Altcoin / geniş kapsam — küçük & yeni coin haberlerini de yakalar
    "CryptoSlate":   "https://cryptoslate.com/feed/",
    "BeInCrypto":    "https://beincrypto.com/feed/",
    "CryptoBriefing":"https://cryptobriefing.com/feed/",
    "CoinJournal":   "https://coinjournal.net/feed/",
    "AMBCrypto":     "https://ambcrypto.com/feed/",
    "U.Today":       "https://u.today/rss",
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
    body: str = ""                  # haber gövdesi/özeti (beyin nüans için; başlıktan fazlası)
    # puanlama sonuçları
    coins: list[str] = field(default_factory=list)
    impact: int = 0                 # 1-10
    direction: str = "neutral"      # bullish | bearish | neutral
    reason: str = ""
    scorer: str = "rule"            # rule | claude
    mismatch: bool = False          # başlık↔gövde çelişkisi (clickbait/şişirilmiş başlık)
    source_count: int = 1           # aynı olayı bildiren farklı kaynak sayısı (çapraz-doğrulama)
    confirming_sources: list[str] = field(default_factory=list)  # teyit eden kaynaklar
    # fiyat teyidi (Binance)
    symbol: str | None = None       # işlem yapılacak parite (örn. BTCUSDT)
    price_24h_pct: float | None = None
    price_15m_pct: float | None = None
    price_60m_pct: float | None = None   # ~1 saatlik hareket (çoklu zaman dilimi teyidi)
    volume_usd: float | None = None
    rel_volume: float | None = None  # RVOL: son mum hacmi / ortalama (kaç kat = haber gerçek mi)
    atr_pct: float | None = None    # son mumların ortalama gerçek aralığı (%) — ATR çıkış için
    confirmed: bool = False         # haber + fiyat hareketi uyumlu mu
    price_note: str = ""            # teyit açıklaması
    # gecikme ölçümü — alım anı (monotonic, serialize edilmez); boru hattı süreleri için
    recv_monotonic: float = field(default_factory=time.monotonic, repr=False, compare=False)

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
            "rel_volume": self.rel_volume,
            "atr_pct": self.atr_pct,
            "confirmed": self.confirmed,
            "price_note": self.price_note,
        }


_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """Kaba HTML temizliği (RSS özetleri sıklıkla HTML içerir)."""
    return _HTML_TAG.sub(" ", s).replace("&nbsp;", " ").strip()


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
    "reconcile_drift_total": 0, # mutabakatta bulunan hayalet pozisyon (news_bot gözlemler)
    "protect_errors_total": 0,  # borsa koruyucu stop konamadı (news_bot gözlemler)
    "failover_scans_total": 0,  # WS bayatken hızlı yedek tarama sayısı (kaynak redundansı)
    "source_disabled_total": 0, # üst üste hata sonrası devre dışı bırakılan yedek kaynak sayısı
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
    # feedparser HTTP hatasında (403/500) istisna ATMAZ — boş feed döner. Kaynak sağlık
    # makinesinin kalıcı-bozuk feed'i yakalayabilmesi için gerçek hatayı yüzeye çıkar:
    status = getattr(d, "status", None)
    if isinstance(status, int) and status >= 400:
        raise RuntimeError(f"RSS HTTP {status}")
    # Geçerli XML olmayan yanıt (örn. blok/hata sayfası) + hiç entry yok → erişim hatası say.
    # (Geçerli feed'in benign bozo'su [encoding vb.] entry içerir → bu dala girmez.)
    if not d.entries and getattr(d, "bozo", False) and getattr(d, "bozo_exception", None) is not None:
        raise RuntimeError(f"RSS erişim/ayrıştırma hatası: {d.bozo_exception}")
    for e in d.entries[:40]:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        if not title:
            continue
        published = None
        if getattr(e, "published", None):
            published = str(e.published)
        summary = getattr(e, "summary", None) or getattr(e, "description", None) or ""
        items.append(
            NewsItem(
                id=_news_id(name, link, title),
                source=name,
                title=title,
                url=link,
                published=published,
                fetched_at=_now_iso(),
                body=_strip_html(str(summary))[:1000],
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


# Yedek kaynak sağlık kaydı (RSS feed'leri + Binance duyuruları için durum-makinesi)
_source_health = sourcehealth.SourceHealth(SOURCE_FAIL_THRESHOLD, SOURCE_COOLDOWN_SEC)
_BINANCE_SOURCE = "Binance duyuruları"


def _on_source_result(name: str, ok: bool, error: str = "") -> None:
    """Kaynak çekim sonucunu sağlık kaydına işle; devre dışı/toparlandı geçişlerini bildir."""
    transition = (_source_health.record_success(name) if ok
                  else _source_health.record_failure(name, error))
    if transition == "disabled":
        _metrics["source_disabled_total"] += 1
        log.warning("Kaynak DEVRE DIŞI (üst üste hata): %s", name)
        notify_remote(f"⚠️ KAYNAK DEVRE DIŞI: '{name}' üst üste hata verdi, geçici "
                      f"devre dışı (cooldown). Son hata: {error[:120]}")
        _record_event("source_disabled", "warn", f"üst üste hata: {error[:120]}", name)
    elif transition == "recovered":
        log.info("Kaynak toparlandı: %s", name)
        notify_remote(f"✅ KAYNAK TOPARLANDI: '{name}' yeniden çalışıyor.")
        _record_event("source_recovered", "info", "cooldown sonrası yeniden çalışıyor", name)


def fetch_all(session: requests.Session) -> list[NewsItem]:
    """Tüm kaynakları çek; biri patlarsa diğerleri devam etsin.

    Her kaynağın sağlığı izlenir (`_source_health`): üst üste hata veren kaynak
    geçici DEVRE DIŞI bırakılıp atlanır (sürekli timeout'la taramayı yavaşlatmasın),
    cooldown sonrası yeniden denenir. Geçişler uzak kanaldan bildirilir.
    """
    out: list[NewsItem] = []
    feeds: list[tuple[str, str | None]] = list(get_rss_feeds().items())
    feeds.append((_BINANCE_SOURCE, None))   # None = Binance duyuru kaynağı (sentinel)
    for name, url in feeds:
        if _source_health.is_disabled(name):
            continue   # devre dışı: cooldown dolana dek atla
        try:
            items = (fetch_binance_announcements(session) if url is None
                     else fetch_rss(name, url))
            out.extend(items)
            _on_source_result(name, True)
        except Exception as e:
            log.warning("Kaynak başarısız (%s): %s", name, e)
            _on_source_result(name, False, str(e))
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
    mismatch: bool = False  # başlık gövdeyle çelişiyor mu (clickbait/şişirilmiş başlık)


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
    "Bazı kayıtlarda başlığın altında '» gövde:' ile haber metni özeti verilir. "
    "BAŞLIK ile GÖVDE çelişiyorsa (başlık iddialı/sansasyonel ama gövde belirsiz, "
    "söylenti, 'olabilir/iddia ediliyor', eski olay, ilgisiz coin → clickbait/şişirme) "
    "mismatch=true ver ve impact'i DÜŞÜR (şişirilmiş başlığa kanma). Gövde başlığı "
    "doğruluyorsa mismatch=false.\n"
    "Her başlık için tek bir JSON kaydı üret:\n"
    "- index: başlığın numarası\n"
    "- coins: etkilenen coin ticker'ları (örn. ['BTC','SOL']); net coin yoksa boş liste\n"
    "- impact: 1-10 piyasa etkisi (10 = piyasayı anında sert hareket ettirir: hack, "
    "iflas, ETF onayı, büyük borsa listelemesi, yasak, dava; 1 = önemsiz/genel yorum). "
    "Kaynak güvenilirliği, yorgunluğu ve başlık-gövde çelişkisi bu skoru ayarlar.\n"
    "- direction: 'bullish' (fiyatı yukarı), 'bearish' (aşağı) veya 'neutral'\n"
    "- reason: en fazla 12 kelimelik Türkçe gerekçe\n"
    "- mismatch: başlık gövdeyle çelişiyor mu (true/false)\n"
    "Sadece istenen yapıyı döndür."
)


# Tek Claude isteğinde puanlanacak haber sayısı. Küçük tut ki çıktı token
# sınırına (max_tokens) sığsın — büyük gruplarda JSON kesilir.
CLAUDE_BATCH = 25

# Başlık↔gövde çelişkisi (clickbait) tespit edilen haberde impact tavanı:
# şişirilmiş başlık ne derse desin etki en fazla bu (refleks/Tier-1 girişi engeller).
_MISMATCH_IMPACT_CAP = 6


def _score_line(i: int, it: NewsItem, now: datetime, ctx: list[NewsItem]) -> str:
    """Puanlama prompt'unda bir haber satırı: başlık + (varsa) gövde özeti.

    Gövde verilirse Claude başlık↔gövde çelişkisini (clickbait/şişirme) görebilir.
    """
    line = f"{i}. [{it.source} · {_item_context(it, now, ctx)}] {it.title}"
    body = (it.body or "").strip()
    if body:
        line += f"\n   » gövde: {body[:300]}"
    return line


def _score_chunk(client: Any, chunk: list[NewsItem], recent: list[NewsItem] | None = None) -> None:
    now = datetime.now(timezone.utc)
    ctx = recent if recent is not None else chunk
    listing = "\n".join(_score_line(i, it, now, ctx) for i, it in enumerate(chunk))
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
        it.mismatch = bool(getattr(r, "mismatch", False))
        # Başlık↔gövde çelişkisi: şişirilmiş başlığa kanma — deterministik impact tavanı
        if it.mismatch and it.impact > _MISMATCH_IMPACT_CAP:
            it.impact = _MISMATCH_IMPACT_CAP
            it.reason = (it.reason + " [başlık-gövde çelişkisi]")[:160]
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


# ── Giriş beyni: girişin tam anında kararlı son yargı ────────────────────
# Mekanik kapıları geçen Tier-2 adaylarda çalışır. Puanlayıcının görmediği her şeyi
# (canlı fiyat hareketi + ATR + funding + kaynağın geçmiş beklentisi + portföy) görür.
ENTRY_BRAIN_MODEL = os.environ.get("ENTRY_BRAIN_MODEL", "claude-haiku-4-5")
# İki-kademeli: kararsız konviksiyonda (bant içi) daha güçlü modele ikinci bakış
ENTRY_BRAIN_ESCALATE_MODEL = os.environ.get("ENTRY_BRAIN_ESCALATE_MODEL", "claude-sonnet-4-6")
ENTRY_ESCALATE_LOW, ENTRY_ESCALATE_HIGH = 0.4, 0.6


class _EntryDecision(BaseModel):
    enter: bool           # gerçekten girilsin mi (veto yetkisi)
    wait_seconds: int     # >0: henüz net değil, bu kadar sn sonra yeniden değerlendir (girme)
    conviction: float     # 0-1: kurulumun gücü (boyutu ölçekler)
    direction: str        # bullish | bearish | neutral (haber yönünü teyit/düzelt)
    # çok-boyutlu rubrik (0-1; risk alanlarında yüksek=kötü, kalite alanlarında yüksek=iyi)
    chase_risk: float     # zaten fiyatlanmış / hareketi kovalama riski
    fade_risk: float      # 15dk-1s çelişkisi / sönme riski
    liquidity: float      # likidite yeterliliği (yüksek=iyi)
    source_quality: float # kaynak güvenilirliği/geçmişi (yüksek=iyi)
    correlation_risk: float  # aynı yönde küme / BTC-korele risk
    # çıkış önerisi (kuruluma göre)
    sl_tightness: str     # tight | normal | wide
    hold_minutes: int     # önerilen time-stop (0 = öneri yok)
    reason: str           # kısa Türkçe gerekçe


_ENTRY_BRAIN_SYSTEM = (
    "Sen disiplinli bir kripto haber-trade risk yöneticisisin. Sana, teyit kapılarını "
    "geçmiş bir oto-işlem ADAYI verilecek (JSON): haber + canlı fiyat + EMSAL (benzer geçmiş "
    "işlemlerin gerçek sonucu) + PİYASA REJİMİ (BTC) + KÜME (aynı coine yakın haberler) + "
    "BEYİN KALİBRASYONU (senin geçmiş konviksiyonlarının gerçek isabeti) + portföy. "
    "Görevin SON YARGI: girilmeli mi, ne kadar konviksiyonla, hangi çıkışla — yoksa BEKLE mi?\n"
    "Önce her boyutu 0-1 puanla: chase_risk (24s büyük hareket → kovalama), fade_risk "
    "(15dk-1s çelişkisi → sönme), liquidity (hacim yeterli mi, yüksek=iyi), source_quality "
    "(kaynak tier + emsal/kaynak geçmişi, yüksek=iyi), correlation_risk (aynı yönde küme + "
    "BTC rejimi).\n"
    "Emsal (recent_pnls/win_rate) ve kendi kalibrasyonuna AĞIRLIK ver — prior değil gerçek veri. "
    "Piyasa rejimi 'risk-off' iken alt-coin long'a temkinli ol; 'risk-on' iken cesur. "
    "Küme: aynı coine zaten girilmiş/çok haber varsa kümülatif etki azalır. "
    "Mikroyapı (orderbook skew): +ise alıcı baskın (long lehine), -ise satıcı baskın; yön ile "
    "çelişiyorsa temkinli ol. Haber gövdesi (varsa) başlıktaki belirsizliği netleştirir.\n"
    "- enter: yüksek risk/negatif emsalde false ile VETO et\n"
    "- wait_seconds: kurulum HENÜZ net değil ama gelişiyorsa (ör. fiyat henüz teyit etmedi, "
    "spike oturuyor) 0 yerine 30-180 ver → enter=false, sonra yeniden bakılır. Net ise 0.\n"
    "- conviction: 0-1 (1 = temiz, yüksek-olasılık; şüphede düşük tut)\n"
    "- direction: haber yönünü teyit et; fiyat/bağlam ters/belirsizse düzelt (neutral)\n"
    "- sl_tightness: oynak/belirsizde 'tight', net trend + iyi likiditede 'wide', aksi 'normal'\n"
    "- hold_minutes: haber edge'inin süresi (hızlı spike 15-30, yapısal haber 60-120, yoksa 0)\n"
    "- reason: en fazla 15 kelimelik Türkçe gerekçe\n"
    "Sermaye korunması önce gelir: şüphede kal = düşük conviction, BEKLE veya veto."
)


_btc_regime_cache: dict[str, Any] = {"ts": 0.0, "data": None}
BTC_REGIME_TTL = 60.0
CLUSTER_WINDOW_MIN = 30


def _btc_regime() -> dict[str, Any] | None:
    """Piyasa rejimi: BTC 24s + ~1s hareketi → risk-on/off/nötr. 60s cache; hata=None."""
    now = time.time()
    c = _btc_regime_cache
    if c["data"] is not None and now - c["ts"] < BTC_REGIME_TTL:
        return c["data"]  # type: ignore[return-value]
    data: dict[str, Any] | None = None
    try:
        t = get_json(f"{BINANCE_API}/ticker/24hr", params={"symbol": "BTCUSDT"}, timeout=REQUEST_TIMEOUT)
        kl = get_json(f"{BINANCE_API}/klines", params={"symbol": "BTCUSDT", "interval": "15m",
                                                       "limit": "4"}, timeout=REQUEST_TIMEOUT)
        pct24 = float(t.get("priceChangePercent", 0) or 0) if t else 0.0
        move1h = 0.0
        if isinstance(kl, list) and kl:
            o, cl = float(kl[0][1]), float(kl[-1][4])
            if o:
                move1h = (cl - o) / o * 100
        regime = "risk-on" if (pct24 >= 2 and move1h >= 0) else "risk-off" if pct24 <= -2 else "nötr"
        data = {"btc_24s_pct": round(pct24, 2), "btc_1s_pct": round(move1h, 2), "rejim": regime}
    except Exception as e:
        log.warning("BTC rejim alınamadı: %s", e)
    c["ts"], c["data"] = now, data
    return data


def _cluster_context(item: NewsItem) -> dict[str, Any]:
    """Küme: aynı coine son CLUSTER_WINDOW_MIN dakikadaki haber sayısı + yön kırılımı."""
    coin = (item.coins[0] if item.coins else None) or (item.symbol or "").replace("USDT", "")
    if not coin:
        return {"son_haber": 0, "ayni_yon": 0, "ters_yon": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=CLUSTER_WINDOW_MIN)
    same = opp = 0
    with _cache_lock:
        snapshot = list(_news)
    for n in snapshot:
        if n.id == item.id or coin not in n.coins:
            continue
        t = _parse_time(n.published) or _parse_time(n.fetched_at)
        if t is None or t < cutoff:
            continue
        if n.direction == item.direction:
            same += 1
        elif n.direction != "neutral":
            opp += 1
    return {"son_haber": same + opp, "ayni_yon": same, "ters_yon": opp}


# ── Çoklu-haber füzyonu: aynı olayı farklı kaynaklardan çapraz-doğrula ───────
FUSE_NEWS = True              # aynı coin+yön+pencere'deki farklı kaynakları say → güven
FUSE_WINDOW_MIN = 20          # bu pencerede (dk) aynı olay sayılır
FUSE_MAX_IMPACT_BONUS = 2     # çok-kaynak teyidi impact'i en fazla bu kadar artırır (cap'li)


def _fuse_event(item: NewsItem, snapshot: list[NewsItem]) -> dict[str, Any]:
    """Bu haberi aynı olayı bildiren DİĞER KAYNAKLARLA çapraz-doğrula (saf, ağsız).

    Aynı coin + aynı yön + FUSE_WINDOW_MIN penceresinde FARKLI kaynaktan gelen haberler
    "teyit" sayılır (tek kaynak vs 3 kaynak = farklı güven). Döner: {source_count,
    confirming_sources, impact_bonus}. Tek kaynak → bonus 0. Bonus = min(kaynak−1,
    FUSE_MAX_IMPACT_BONUS) — her ek bağımsız kaynak +1, cap'li. Aynı kaynağın tekrarı
    (echo) sayılmaz; nötr yön katkı yapmaz.
    """
    coin = item.symbol or (item.coins[0] if item.coins else None)
    base_src = (item.source or "").lower().strip()
    if not coin or item.direction == "neutral":
        return {"source_count": 1, "confirming_sources": [], "impact_bonus": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=FUSE_WINDOW_MIN)
    confirming: set[str] = set()
    for n in snapshot:
        if n.id == item.id or n.direction != item.direction:
            continue
        n_coin = n.symbol or (n.coins[0] if n.coins else None)
        if n_coin != coin:
            continue
        nsrc = (n.source or "").lower().strip()
        if not nsrc or nsrc == base_src:   # echo/aynı kaynak → güven katmaz
            continue
        t = _parse_time(n.published) or _parse_time(n.fetched_at)
        if t is None or t < cutoff:
            continue
        confirming.add(n.source)
    source_count = 1 + len(confirming)
    bonus = min(len(confirming), FUSE_MAX_IMPACT_BONUS)
    return {"source_count": source_count, "confirming_sources": sorted(confirming),
            "impact_bonus": bonus}


def _apply_fusion(items: list[NewsItem]) -> None:
    """Yeni haberleri çoklu-kaynak füzyonuyla zenginleştir (yerinde günceller).

    Her item için aynı olayı bildiren farklı kaynakları sayar; çok-kaynak teyidi
    impact'i artırır (cap 10). source_count/confirming_sources item'a yazılır → beyin
    ve panel görür. Snapshot tüm haber havuzu (yeni + eski, pencere içi süzülür).
    """
    if not FUSE_NEWS:
        return
    with _cache_lock:
        snapshot = list(_news)
    for it in items:
        f = _fuse_event(it, snapshot)
        it.source_count = f["source_count"]
        it.confirming_sources = f["confirming_sources"]
        if f["impact_bonus"] > 0 and it.impact < 10:
            it.impact = min(10, it.impact + f["impact_bonus"])
            extra = f"{f['source_count']} kaynak teyidi"
            it.reason = (it.reason + f" [{extra}]")[:160] if it.reason else extra


def _brain_call(client: Any, model: str, ctx: dict[str, Any]) -> _EntryDecision:
    resp = client.messages.parse(
        model=model, max_tokens=500, system=_ENTRY_BRAIN_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}],
        output_format=_EntryDecision,
    )
    return resp.parsed_output


def _brain_calib_summary() -> dict[str, Any]:
    """Beynin context'ine giren HAFİF kalibrasyon özeti (ham diyagram değil — token tasarrufu).

    Beyin kendi geçmiş isabetini görür (aşırı-güven düzeltir) ama her çağrıda büyük
    reliability/rubric dizilerini taşımaz. Panel/uç ham veriyi /brain-scorecard'tan alır.
    """
    sc = trader.brain_scorecard()
    return {"ornek": sc.get("samples"), "kalibre": sc.get("calibrated"),
            "brier": sc.get("brier"), "asiri_guvenli": sc.get("overconfident"),
            "ort_konviksiyon": sc.get("mean_conviction"), "gercek_oran": sc.get("base_rate")}


def entry_brain_decision(item: NewsItem, decision: dict[str, Any], *,
                         backtest: bool = False) -> dict[str, Any] | None:
    """Giriş anında Claude kararlı yargı. None = beyin yok/başarısız (mekanik karar geçerli).

    `decision`: trader.auto_decision çıktısı (side, usdt, news_source). Yan etkisiz.
    Emsal + rubrik + çıkış önerisi + (kararsızda) eskalasyon + mikroyapı/rejim/küme/tam-metin.
    `backtest=True`: canlı-anlık girdileri (orderbook/rejim/küme) atla — offline replay için.
    """
    if not USE_CLAUDE:
        return None
    src = getattr(item, "source", "") or ""
    side = decision.get("side") or ""
    st = trader.source_stats(src)
    # Mikroyapı: orderbook skew (canlı yolda); aynı book likidasyon-baskısına da verilir
    book = None if (backtest or not item.symbol) else trader.orderbook_imbalance(item.symbol)
    # Likidasyon-baskısı: funding+premium → squeeze setup (futures, canlı yolda, auth'suz)
    squeeze = (None if (backtest or not item.symbol or trader.S.market != "futures")
               else trader.liquidation_pressure(item.symbol, side, book))
    ctx = {
        "haber": {
            "kaynak": src, "kaynak_tier": _source_tier(src),
            "baslik": item.title[:200], "govde": (item.body or "")[:600],
            "guc": item.impact, "yon": item.direction, "gerekce": item.reason[:160],
            "coin": item.symbol, "yas_sn": round(_news_age_sec(item) or 0),
            "baslik_govde_celiskisi": item.mismatch,  # clickbait/şişirme tespit edildi mi
            "kaynak_teyidi": item.source_count,       # kaç farklı kaynak aynı olayı bildirdi
        },
        "fiyat": {
            "deg_24s_pct": item.price_24h_pct, "deg_15dk_pct": item.price_15m_pct,
            "deg_1s_pct": item.price_60m_pct, "hacim_usd": item.volume_usd,
            "rvol": item.rel_volume,   # göreceli hacim: >1.5x haber gerçek, hacimsiz=fake
            "atr_pct": item.atr_pct, "teyitli": item.confirmed, "not": item.price_note[:160],
        },
        "mikroyapi": book,
        "likidasyon_baskisi": squeeze,   # funding/premium aşırılığı → squeeze setup (futures)
        "kaynak_gecmisi": {"kapanmis_islem": st["count"], "ort_pnl_usdt": st["avg_pnl"]},
        "emsal": {
            "kaynak_geneli": trader.precedent_stats(news_source=src),
            "ayni_coin_yon": trader.precedent_stats(symbol=item.symbol, side=side),
        },
        "piyasa_rejimi": None if backtest else _btc_regime(),
        "kume": {"son_haber": 0, "ayni_yon": 0, "ters_yon": 0} if backtest else _cluster_context(item),
        "beyin_kalibrasyon": _brain_calib_summary(),
        "portfoy": {"ayni_yonde_acik": trader._open_side_count(side), "yon": side},
    }
    client = _get_anthropic()
    # Çoklu-oylama: N bağımsız çağrı → çoğunluk enter + medyan conviction (gürültü azaltma).
    # vote_count=1 ise tek çağrı (eski davranış). Bağımsızlık: aynı model, ayrı API çağrıları.
    n_votes = max(1, trader.S.brain_vote_count)
    _t_brain = time.monotonic()
    r, vote = _brain_vote(client, ENTRY_BRAIN_MODEL, ctx, n_votes)
    model_used, escalated = ENTRY_BRAIN_MODEL, False
    # İki-kademeli: kararsız bantta daha güçlü modele ikinci bakış (nihai karar onun)
    conv0 = max(0.0, min(1.0, float(r.conviction)))
    if (trader.S.brain_escalate and ENTRY_ESCALATE_LOW <= conv0 <= ENTRY_ESCALATE_HIGH
            and ENTRY_BRAIN_ESCALATE_MODEL != ENTRY_BRAIN_MODEL):
        try:
            r = _brain_call(client, ENTRY_BRAIN_ESCALATE_MODEL, ctx)
            model_used, escalated = ENTRY_BRAIN_ESCALATE_MODEL, True
        except Exception as e:
            log.warning("Beyin eskalasyonu başarısız, haiku kararı geçerli: %s", e)
    out = {
        "enter": bool(r.enter), "wait_seconds": max(0, min(300, int(r.wait_seconds))),
        "conviction": max(0.0, min(1.0, float(r.conviction))),
        "direction": r.direction, "sl_tightness": r.sl_tightness,
        "hold_minutes": max(0, int(r.hold_minutes)), "reason": (r.reason or "")[:160],
        "scores": {
            "chase_risk": round(float(r.chase_risk), 2), "fade_risk": round(float(r.fade_risk), 2),
            "liquidity": round(float(r.liquidity), 2),
            "source_quality": round(float(r.source_quality), 2),
            "correlation_risk": round(float(r.correlation_risk), 2),
        },
        "model": model_used, "escalated": escalated,
    }
    if vote is not None:
        out["vote"] = vote   # şeffaflık: oy sayısı + enter-oranı + oybirliği
    if not backtest:
        # Beyin (Claude) çağrı gecikmesi — Tier-2'de asıl maliyet; eskalasyon/oylama dahil
        latency.record("brain", (time.monotonic() - _t_brain) * 1000.0)
    return out


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _brain_vote(client: Any, model: str, ctx: dict[str, Any],
                n: int) -> tuple[Any, dict[str, Any] | None]:
    """N bağımsız beyin çağrısı → birleştirilmiş karar. n=1'de tek çağrı (oy yok).

    Çoğunluk `enter` (≥yarısı evet), medyan conviction, oybirliği oranı. Temsili karar
    olarak medyan-conviction'a EN YAKIN oyu seçer (sl_tightness/hold/scores tutarlı kalsın),
    ama enter ve conviction'ı çoğunluk/medyanla EZER. Bir çağrı patlarsa atlanır; hepsi
    patlarsa istisna yükselir (çağıran fail-safe yakalar). Döner: (temsili_r, vote|None).
    """
    if n <= 1:
        return _brain_call(client, model, ctx), None
    results = []
    for _ in range(n):
        try:
            results.append(_brain_call(client, model, ctx))
        except Exception as e:
            log.warning("Oylama çağrısı başarısız (atlandı): %s", e)
    if not results:
        raise RuntimeError("tüm oylama çağrıları başarısız")
    enters = [bool(x.enter) for x in results]
    convs = [max(0.0, min(1.0, float(x.conviction))) for x in results]
    enter_ratio = sum(enters) / len(results)
    majority_enter = enter_ratio >= 0.5
    med_conv = _median(convs)
    agreement = max(enter_ratio, 1.0 - enter_ratio)   # oybirliği gücü (0.5=bölünmüş, 1.0=tam)
    # Temsili: medyan conviction'a en yakın oy (rubrik/çıkış alanları tutarlı)
    rep = min(results, key=lambda x: abs(max(0.0, min(1.0, float(x.conviction))) - med_conv))
    rep.enter = majority_enter            # çoğunlukla ez
    rep.conviction = med_conv             # medyanla ez (aykırı oy etkisini kır)
    vote = {"n": len(results), "enter_ratio": round(enter_ratio, 2),
            "agreement": round(agreement, 2), "convictions": [round(c, 2) for c in convs]}
    return rep, vote


# ── Bekle/izle: kararsız ama gelişen kurulumu kısa süre ertele, sonra yeniden bak ──
_deferred_lock = threading.Lock()
_brain_due: dict[str, float] = {}        # item_id -> yeniden bakılacak monotonic zaman
_brain_items: dict[str, NewsItem] = {}   # item_id -> item
_brain_tries: dict[str, int] = {}        # item_id -> kaç kez 'bekle' dendi
MAX_BRAIN_DEFERS = 3
MAX_DEFERRED = 50


def _clear_defer(iid: str) -> None:
    _brain_due.pop(iid, None)
    _brain_items.pop(iid, None)
    _brain_tries.pop(iid, None)


def _verdict_of(v: dict[str, Any]) -> str:
    """Beyin çıktısından karar etiketi: wait | enter | veto."""
    if int(v.get("wait_seconds", 0) or 0) > 0:
        return "wait"
    return "enter" if v.get("enter") else "veto"


def _log_brain_decision(item: NewsItem, side: str, v: dict[str, Any]) -> None:
    """Bir canlı beyin kararını kalıcı günlüğe yaz (veto/bekle hesap verebilirliği). Hata yutar."""
    try:
        get_store().add_brain_decision({
            "news_id": item.id, "source": item.source, "title": item.title[:120],
            "symbol": item.symbol, "side": side, "impact": item.impact,
            "direction": item.direction, "verdict": _verdict_of(v),
            "conviction": v.get("conviction"), "sl_tightness": v.get("sl_tightness"),
            "hold_minutes": v.get("hold_minutes"), "wait_seconds": v.get("wait_seconds"),
            "escalated": v.get("escalated"), "model": v.get("model"),
            "reason": v.get("reason"), "scores": v.get("scores"),
            "published": item.published or item.fetched_at,
            "price_24h_pct": item.price_24h_pct, "price_15m_pct": item.price_15m_pct,
            "atr_pct": item.atr_pct,
        })
    except Exception as e:
        log.warning("Beyin kararı günlüğe yazılamadı: %s", e)


def _brain_for_trade(item: NewsItem, decision: dict[str, Any]) -> dict[str, Any] | None:
    """Canlı işlem yolunda beyin: 'bekle' (wait_seconds) kararını erteleme olarak yönetir.

    entry_brain_decision saf kalır; deferral bookkeeping + karar günlüğü burada.
    """
    v = entry_brain_decision(item, decision)
    if v is None:
        return None
    _log_brain_decision(item, decision.get("side", "") or "", v)   # her kararı günlüğe yaz
    wait = int(v.get("wait_seconds", 0) or 0)
    if wait > 0:
        with _deferred_lock:
            tries = _brain_tries.get(item.id, 0)
            if tries < MAX_BRAIN_DEFERS and len(_brain_due) < MAX_DEFERRED:
                _brain_tries[item.id] = tries + 1
                _brain_due[item.id] = time.monotonic() + wait
                _brain_items[item.id] = item
                log.info("Giriş beyni BEKLE | %s | %ds (deneme %d) | %s",
                         item.symbol, wait, tries + 1, v.get("reason", ""))
            else:
                _clear_defer(item.id)   # deneme/kapasite bitti → bırak
        return {**v, "enter": False}
    with _deferred_lock:
        _clear_defer(item.id)   # net karar → ertelemeyi temizle
    return v


def _recheck_deferred_entries() -> None:
    """Süresi gelen 'bekle' adaylarını yeniden değerlendir (canlı işlem yolu). _monitor_loop'tan."""
    now = time.monotonic()
    with _deferred_lock:
        due_ids = [iid for iid, due in _brain_due.items() if due <= now]
        for iid in due_ids:
            _brain_due.pop(iid, None)   # bu turda tekrar seçilmesin (tries/item korunur)
    for iid in due_ids:
        item = _brain_items.get(iid)
        if item is None:
            continue
        if _too_old(item):
            with _deferred_lock:
                _clear_defer(iid)
            continue
        try:
            pos = trader.maybe_auto_trade(item, **_trade_context(item), brain=_brain_for_trade)
        except Exception as e:
            log.warning("Ertelenen giriş yeniden değerlendirme hatası (%s): %s", item.symbol, e)
            pos = None
        if pos:
            _metrics["trades_opened_total"] += 1
            log.info("OTO İŞLEM (ertelenen) | %s %s | %s", pos["side"], pos["symbol"], pos["mode"])
            notify_remote(_fmt_trade_msg(pos, opened=True))
        else:
            # brain yeniden 'bekle' dediyse due geri yazıldı; aksi halde (veto/uygunsuz) temizle
            with _deferred_lock:
                if iid in _brain_items and iid not in _brain_due:
                    _clear_defer(iid)


# ── Fiyat teyidi (Binance public) ────────────────────────────────────────
def _compute_rvol(candles: list[Any]) -> float:
    """RVOL: son hareketin quote-hacmi, önceki mumların ortalamasının kaç katı.

    Kısmi (oluşmakta olan) son muma dayanıklı: son iki mumun maks'ını alır
    (sürüş ister oluşan ister yeni kapanan mumda olsun yakalanır), baz çizgi
    bu iki mumu dışlar. >1 = normalin üstü ilgi; haberin GERÇEKliğinin sinyali.
    """
    if not isinstance(candles, list) or len(candles) < 4:
        return 0.0
    def qv(c: Any) -> float:
        try:
            return float(c[7])  # klines[7] = quote asset volume (USD cinsi)
        except (IndexError, TypeError, ValueError):
            return 0.0
    recent = max(qv(candles[-1]), qv(candles[-2]))
    prior = [v for c in candles[:-2] if (v := qv(c)) > 0]
    if not prior or recent <= 0:
        return 0.0
    baseline = sum(prior) / len(prior)
    return round(recent / baseline, 2) if baseline > 0 else 0.0


def _fetch_symbol_stats(session: requests.Session, symbol: str) -> dict[str, float] | None:
    """Bir parite için 24s değişim, hacim, son ~15dk/~1s hareketi, ATR% ve RVOL döndür."""
    t = get_json(f"{BINANCE_API}/ticker/24hr", params={"symbol": symbol},
                 timeout=REQUEST_TIMEOUT, session=session)
    if not t:
        return None
    # Tek istek: teyit penceresi (son CONFIRM_LIMIT mum) + RVOL baz çizgisi (RVOL_LOOKBACK).
    limit = max(CONFIRM_LIMIT, RVOL_LOOKBACK)
    candles = get_json(
        f"{BINANCE_API}/klines",
        params={"symbol": symbol, "interval": CONFIRM_INTERVAL, "limit": str(limit)},
        timeout=REQUEST_TIMEOUT, session=session,
    )
    move15 = move60 = atr_pct = rvol = 0.0
    if isinstance(candles, list) and candles:
        last = candles[-1]
        o15, c15 = float(last[1]), float(last[4])
        if o15:
            move15 = (c15 - o15) / o15 * 100
        # Kısa pencere (move60 + ATR) = son CONFIRM_LIMIT mum (RVOL baz çizgisi değil)
        window = candles[-CONFIRM_LIMIT:] if len(candles) >= CONFIRM_LIMIT else candles
        o60, c60 = float(window[0][1]), float(window[-1][4])
        if o60:
            move60 = (c60 - o60) / o60 * 100
        # ATR% ≈ mumların ortalama (yüksek-düşük)/kapanış — oynaklık ölçüsü
        ranges = [(float(k[2]) - float(k[3])) / float(k[4]) * 100
                  for k in window if float(k[4]) > 0]
        if ranges:
            atr_pct = sum(ranges) / len(ranges)
        rvol = _compute_rvol(candles)
    return {
        "pct24": float(t.get("priceChangePercent", 0) or 0),
        "vol": float(t.get("quoteVolume", 0) or 0),
        "move15": move15,
        "move60": move60,
        "atr_pct": atr_pct,
        "rvol": rvol,
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
    item.rel_volume = stats.get("rvol") or None
    item.atr_pct = round(stats.get("atr_pct", 0.0), 3) or None

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
        brain = pos.get("brain")
        if brain:  # giriş beyni yargısı (şeffaflık)
            esc = " ⬆️" if brain.get("escalated") else ""
            tail += f"\n🧠 konv {brain.get('conviction')}{esc} · {brain.get('reason', '')}"
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


_ops_event_count = 0


def _record_event(kind: str, severity: str, detail: str = "", source: str = "") -> None:
    """Operasyonel olayı (incident) kalıcı zaman çizelgesine yaz. Hata akışı bozmaz.

    Geçiş-temelli (durum-makinesi) çağrılır — feed kopuk/geri, kaynak devre-dışı/toparlandı,
    gecikme SLA aşıldı/düzeldi, devre kesici tetiklendi/temizlendi. Canlı post-mortem için
    kalıcı kayıt (`/events`). Bildirimlerle çakışmaz: bildirim anlık, bu arşivlenebilir tarih.
    """
    global _ops_event_count
    try:
        get_store().add_ops_event(kind, severity, detail, source)
        _ops_event_count += 1
        if _ops_event_count % 50 == 0:
            get_store().prune_ops_events(MAX_OPS_EVENTS)
    except Exception as e:
        log.warning("Operasyonel olay yazılamadı (%s): %s", kind, e)


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
        _record_event("feed_stale", "warn", detail, "treenews")
    elif not stale and _ws_alert_active:
        _ws_alert_active = False
        notify_remote("✅ Haber akışı geri geldi (WS bağlı, mesaj akıyor).")
        _record_event("feed_recovered", "info", "WS bağlı, mesaj akıyor", "treenews")


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


def _source_bucket(item: NewsItem) -> str:
    """Kaynak adını kaba bir kategoriye indir (gecikme kırılımı; kardinalite kontrolü)."""
    s = (item.source or "").lower()
    if s.startswith("⚡") or "tree" in s:
        return "treenews"
    if "binance" in s:
        return "binance"
    return "rss"


def _ingest_ms(item: NewsItem) -> float | None:
    """Kaynak yayını → bot alımı gecikmesi (ms). published bilinmiyorsa None.

    TreeNews/RSS yayın zamanı ile bizim alım (fetched_at) zamanımız arasındaki fark:
    kaynak + ağ gecikmesini ölçer. Saat kayması negatif verirse tracker yok sayar.
    """
    pub = _parse_time(item.published)
    recv = _parse_time(item.fetched_at)
    if pub is None or recv is None:
        return None
    return (recv - pub).total_seconds() * 1000.0


def _too_old(item: NewsItem) -> bool:
    t = _parse_time(item.published) or _parse_time(item.fetched_at)
    if t is None:
        return False
    return datetime.now(timezone.utc) - t > timedelta(hours=MAX_NEWS_AGE_HOURS)


def _news_age_sec(item: NewsItem) -> float | None:
    """Haberin yaşı (saniye) — latency kapısı için. Zaman yoksa None."""
    t = _parse_time(item.published) or _parse_time(item.fetched_at)
    if t is None:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


_PORTFOLIO_CORR_LIMIT = 30   # korelasyon için kaç mum (CONFIRM_INTERVAL bazında)
_corr_cache: dict[str, tuple[float, list[float]]] = {}   # symbol -> (monotonic_ts, getiri_serisi)
_CORR_CACHE_TTL = 120.0      # getiri serisi cache (saniye) — aynı turda tekrar çekme


def _return_series(session: requests.Session, symbol: str) -> list[float]:
    """Bir parite için getiri serisi (kapanışlardan), TTL cache'li. Hata → boş liste."""
    now = time.monotonic()
    hit = _corr_cache.get(symbol)
    if hit and now - hit[0] < _CORR_CACHE_TTL:
        return hit[1]
    try:
        candles = get_json(
            f"{BINANCE_API}/klines",
            params={"symbol": symbol, "interval": CONFIRM_INTERVAL, "limit": str(_PORTFOLIO_CORR_LIMIT)},
            timeout=REQUEST_TIMEOUT, session=session)
        closes = [float(c[4]) for c in candles] if isinstance(candles, list) else []
        series = trader._returns(closes)
    except Exception as e:
        log.debug("Getiri serisi çekilemedi (%s): %s", symbol, e)
        series = []
    _corr_cache[symbol] = (now, series)
    return series


def _portfolio_series(item: NewsItem) -> dict[str, list[float]] | None:
    """Portföy korelasyon-yükü için yeni aday + açık pozisyon coinlerinin getiri serileri.

    Yalnız portfolio_risk AÇIK + açık pozisyon VAR + adayın sembolü varsa kline çeker
    (aksi halde None — boşuna ağ yok). Cache'li, hatalar boş seriye düşer (nötr).
    """
    if not trader.S.portfolio_risk or not item.symbol:
        return None
    open_syms = set(trader.open_symbols())
    if not open_syms:
        return None
    syms = open_syms | {item.symbol}
    sess = requests.Session()
    try:
        return {s: _return_series(sess, s) for s in syms}
    finally:
        sess.close()


def _trade_context(item: NewsItem) -> dict[str, Any]:
    """Oto-işlem güvenlik kapıları için bağlam (akış durumu + haber yaşı + portföy serisi)."""
    return {"feed_stale": _ws_feed_stale(), "news_age_sec": _news_age_sec(item),
            "latency_slow": bool(_latency_breaches()), "price_series": _portfolio_series(item)}


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

    # Gecikme: kaynak yayını → bot alımı (boru hattının ilk halkası) + kaynak kırılımı
    for it in new_items:
        ms = _ingest_ms(it)
        latency.record("ingest", ms)
        latency.record_source(_source_bucket(it), ms)

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
        _t_score = time.monotonic()
        try:
            score_with_claude(new_items)
            latency.record("score", (time.monotonic() - _t_score) * 1000.0)
        except Exception as e:
            log.warning("Claude puanlama başarısız, kural skoru geçerli: %s", e)

    # Faz 3 — Çoklu-kaynak füzyonu: aynı olayı bildiren farklı kaynaklar impact'i artırır
    # (nihai skordan sonra → eşik/oto-işlem çapraz-doğrulanmış gücü görür)
    _apply_fusion(new_items)

    # Nihai güçlü haberler: teyit + arşiv + oto-işlem (para yolu nihai skorda)
    alerts = [it for it in new_items if it.impact >= threshold]
    _metrics["alerts_total"] += len(alerts)
    if alerts:
        _t_confirm = time.monotonic()
        _confirm_alerts(session, alerts)   # paralel fiyat teyidi (çoklu alert'te hız)
        latency.record("confirm", (time.monotonic() - _t_confirm) * 1000.0)

    if allow_notify:
        for it in alerts:
            _archive_signal(it)
            if it.id not in notified:   # erken bildirilenleri tekrar bildirme
                notify(it)
            _log_shadow_decision(it)    # A/B: aday ayar bu sinyalde ne karar verirdi (sanal)
            _t_order = time.monotonic()
            pos = trader.maybe_auto_trade(it, **_trade_context(it), brain=_brain_for_trade)
            if pos:
                # Gecikme: karar→emir + uçtan uca alım→emir (manşet aksiyon gecikmesi)
                _now_mono = time.monotonic()
                latency.record("order", (_now_mono - _t_order) * 1000.0)
                latency.record("pipeline", (_now_mono - it.recv_monotonic) * 1000.0)
                _metrics["trades_opened_total"] += 1
                log.info("OTO İŞLEM AÇILDI | %s %s | %s", pos["side"], pos["symbol"], pos["mode"])
                notify_remote(_fmt_trade_msg(pos, opened=True))
                if pos.get("protect_error"):   # borsa stop konulamadı → KORUMASIZ canlı pozisyon
                    _metrics["protect_errors_total"] += 1
                    notify_remote(f"⚠️ DİKKAT: {pos['symbol']} borsa koruyucu stop KONULAMADI "
                                  f"— pozisyon yalnız bot çalışırken korumalı. Sebep: {pos['protect_error']}")

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


def _log_shadow_decision(item: NewsItem) -> None:
    """A/B: aday ayar (gölge) bu canlı sinyalde ne karar verirdi — SANAL, kalıcı kaydet.

    Gölge override yoksa no-op. Gerçek emir YOK; yalnız canlı vs aday karar farkını
    günlükler (sonradan /shadow ile kıyaslanır). Hata akışı bozmaz.
    """
    if not trader.get_shadow_overrides():
        return
    try:
        res = trader.shadow_decision(item, **_trade_context(item))
        if res is None:
            return
        get_store().add_shadow_decision({
            "news_id": item.id, "symbol": item.symbol, "side": res["live"].get("side"),
            "impact": item.impact, "published": item.published or item.fetched_at,
            "live_trade": res["live"]["would_trade"], "shadow_trade": res["shadow"]["would_trade"],
            "live_usdt": res["live"].get("usdt"), "shadow_usdt": res["shadow"].get("usdt"),
            "diverged": res["diverged"],
            "overrides": json.dumps(trader.get_shadow_overrides(), ensure_ascii=False),
        })
    except Exception as e:
        log.warning("Gölge karar günlüğü hatası: %s", e)


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


def _scan_interval(now: float | None = None) -> int:
    """Yedek tarama aralığı: asıl realtime kaynak (WS) bayatsa HIZLI, yoksa normal. Saf.

    TreeNews WS kopuk/sessizse RSS+Binance yedeği boşluğu doldurmak için sıklaşır
    (kaynak redundansı). WS sağlıklıyken normal cadence — gereksiz API yükü yok.
    """
    if _ws_feed_stale(now):
        return min(SCAN_INTERVAL_FAST_SEC, SCAN_INTERVAL_SEC)
    return SCAN_INTERVAL_SEC


def _background_loop(stop: threading.Event) -> None:
    session = requests.Session()
    session.headers.setdefault("User-Agent", "kripto-haber-bot/1.0")
    while not stop.is_set():
        refresh(session)
        _maybe_daily_digest()      # gün dönümünde dünün özetini gönder
        _maybe_deadman_alert()     # haber akışı durduysa uyar (ölü-adam anahtarı)
        interval = _scan_interval()
        if interval < SCAN_INTERVAL_SEC:   # failover: WS bayat → hızlı yedek tarama
            _metrics["failover_scans_total"] += 1
        if stop.wait(interval):
            break


# Açık pozisyonları SL/TP/trailing için sık aralıkla izle
MONITOR_INTERVAL_SEC = 8
RECONCILE_INTERVAL_SEC = 300   # periyodik mutabakat (canlıda hayalet pozisyon taraması)
_last_reconcile = 0.0


def _periodic_reconcile() -> None:
    """Canlıda periyodik mutabakat: bot çalışırken oluşan drift'i (hayalet pozisyon) yakala."""
    global _last_reconcile
    now = time.time()
    if now - _last_reconcile < RECONCILE_INTERVAL_SEC:
        return
    _last_reconcile = now
    rec = trader.reconcile_and_heal(autoclose=trader.S.reconcile_autoclose)
    if rec.get("checked") and rec.get("phantoms"):
        syms = [o["symbol"] for o in rec["phantoms"]]
        _metrics["reconcile_drift_total"] += len(syms)
        action = (f"{len(rec['healed'])}'i otomatik kapatıldı" if rec.get("healed")
                  else "ELLE kontrol et (oto-kapatma kapalı)")
        log.warning("Periyodik mutabakat: %d hayalet pozisyon: %s", len(syms), syms)
        notify_remote(f"⚠️ MUTABAKAT (çalışırken): borsada görünmeyen {len(syms)} pozisyon "
                      f"({', '.join(syms)}) — {action}.")


_last_latency_snapshot = 0.0
_latency_snapshot_count = 0


def _maybe_snapshot_latency() -> None:
    """Periyodik olarak latency özetini kalıcı arşive yaz (restart'a dayanıklı trend).

    `latency.summary()` bellekte kayan penceredir; restart'ta kaybolur. Bu, "gerçek
    edge"in günler boyu trendini görmek için aşama p50/p95/max'ı `latency_snapshots`
    tablosuna işler. Boş özet (örnek yok) atlanır. Sınırsız büyümeyi önlemek için budar.
    """
    global _last_latency_snapshot, _latency_snapshot_count
    now = time.time()
    if now - _last_latency_snapshot < LATENCY_SNAPSHOT_EVERY_SEC:
        return
    _last_latency_snapshot = now
    try:
        summary = latency.summary()
        if not summary:
            return
        n = get_store().add_latency_snapshot(summary)
        if n:
            _latency_snapshot_count += 1
            if _latency_snapshot_count % 20 == 0:   # ara sıra buda
                get_store().prune_latency_snapshots(MAX_LATENCY_SNAPSHOTS)
    except Exception as e:
        log.warning("Gecikme anlık görüntüsü yazılamadı: %s", e)


_ops_state: dict[str, Any] = {"latency_breaches": set(), "halt_active": False}


def _check_ops_transitions() -> None:
    """Gecikme SLA + devre kesici DURUM GEÇİŞLERİNİ yakala → olay zaman çizelgesine yaz.

    Kaynak/feed geçişleri kendi durum-makinelerinden (`_on_source_result`/deadman) yazılır;
    bu, kalan iki ekseni (latency breach onset/clear + halt trip/clear) son-görülen duruma
    göre kıyaslayıp YALNIZ geçişte olay üretir (sürekli tekrar değil). Monitör döngüsünden."""
    # Gecikme SLA: yeni aşan / yeni düzelen aşamalar
    cur = set(_latency_breaches())
    prev = _ops_state["latency_breaches"]
    for stage in cur - prev:
        _record_event("latency_breach", "warn", "p95 SLA eşiğini aştı (yavaş)", stage)
    for stage in prev - cur:
        _record_event("latency_clear", "info", "p95 SLA içine döndü", stage)
    _ops_state["latency_breaches"] = cur
    # Devre kesici: tetiklendi / temizlendi
    halt = trader.get_halt()
    if halt["active"] and not _ops_state["halt_active"]:
        _record_event("halt_tripped", "critical", str(halt.get("reason", "")), "")
    elif not halt["active"] and _ops_state["halt_active"]:
        _record_event("halt_cleared", "info", "devre kesici temizlendi", "")
    _ops_state["halt_active"] = bool(halt["active"])


def _persist_closed(pos: dict[str, Any]) -> None:
    """Kapanan işlemi kalıcı deftere yaz (trade_state.json 500 sınırı dışı). Hata akışı bozmaz."""
    try:
        get_store().add_closed_news_trade(pos)
    except Exception as e:
        log.warning("Kapanan işlem arşivlenemedi: %s", e)


def _monitor_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            closed_any = False
            for pos in trader.monitor_positions():
                _persist_closed(pos)
                notify_remote(_fmt_trade_msg(pos, opened=False))
                closed_any = True
            # Kapalı döngü öğrenme: yeni işlem kapandıysa, auto_tune açıksa
            # öğrenen beyin önerilerini korkuluklarla oto-uygula (kapalıyken no-op)
            if closed_any:
                try:
                    res = trader.auto_apply_tuning(tier_of=_source_tier)
                    if res.get("changes"):
                        log.info("Oto-öğrenme uygulandı: %s", res["changes"])
                        notify_remote("🧠 Oto-öğrenme: " + ", ".join(
                            f"{c['field']} {c['from']}→{c['to']}" for c in res["changes"]))
                    trader.refresh_learned_vetoes()   # koşullu öğrenilen-veto listesini tazele
                    # Rejim adaptasyonu: bozulmada eşiği geçici sıkılaştır / toparlanınca geri al
                    reg = trader.regime_adapt_step()
                    if reg.get("acted") and reg.get("change"):
                        c = reg["change"]
                        yon = "sıkılaştır" if reg["state"] == "tighten" else "geri al"
                        notify_remote(f"🌀 Rejim adaptasyonu ({yon}): "
                                      f"{c['field']} {c['from']}→{c['to']}")
                except Exception as e:
                    log.warning("Oto-öğrenme hatası: %s", e)
        except Exception as e:
            log.warning("Pozisyon izleme hatası: %s", e)
        try:
            _recheck_deferred_entries()   # giriş beyni 'bekle' adaylarını yeniden değerlendir
        except Exception as e:
            log.warning("Ertelenen giriş kontrolü hatası: %s", e)
        try:
            _periodic_reconcile()         # canlıda hayalet pozisyon taraması (5dk)
        except Exception as e:
            log.warning("Periyodik mutabakat hatası: %s", e)
        _maybe_snapshot_latency()         # gecikme trendini kalıcı arşivle (5dk)
        try:
            _check_ops_transitions()      # latency-breach/halt geçişlerini olaya yaz
        except Exception as e:
            log.warning("Operasyonel geçiş kontrolü hatası: %s", e)
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

    _body = data.get("body") or data.get("description") or ""
    item = NewsItem(
        id=_news_id(f"Tree:{src}", url or tid, title),
        source=f"⚡{src}",
        title=title,
        url=url,
        published=published,
        fetched_at=_now_iso(),
        body=_strip_html(str(_body))[:1000] if str(_body).strip() != title else "",
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


_instance_lock: Any = None   # tek-instance kilidi (handle'ı canlı tut → OS kilidi açık kalır)


def _acquire_singleton_lock() -> bool:
    """Aynı hesaba karşı ÇİFT bot çalışmasını önle (çift işlem felaketi).

    Taşınabilir OS kilidi (fcntl/msvcrt); süreç ölünce OS serbest bırakır (çökme-dayanıklı,
    bayat kilit sorunu yok). Alınamazsa False (başka örnek çalışıyor). Kilit yoksa True (sakınca yok).
    """
    global _instance_lock
    path = os.environ.get("BOTPY_LOCK", trader.STATE_FILE + ".lock")
    try:
        f = open(path, "w")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False   # kilit başkası tarafından tutuluyor
    except Exception as e:
        log.warning("Tek-instance kilidi kurulamadı (atlanıyor): %s", e)
        return True    # kilit altyapısı yoksa engelleme
    try:
        f.write(f"{os.getpid()}\n{_now_iso()}\n")
        f.flush()
    except Exception:
        pass
    _instance_lock = f   # GC'lenirse kilit açılır → referansı tut
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_thread, _ws_thread, _mon_thread
    setup_logging()
    if not _acquire_singleton_lock():
        raise RuntimeError(
            "Başka bir bot örneği zaten çalışıyor (tek-instance kilidi) — çift işlem riskini "
            "önlemek için başlatma iptal edildi. Diğer örneği kapat veya BOTPY_LOCK'u değiştir.")
    _load_news_settings()   # kalıcı eşik/bildirim ayarlarını yükle (restart'a dayanıklı)
    try:
        store = get_store()
        store.prune_signals(MAX_ARCHIVE_SIGNALS)   # arşivi sınırla (başlangıç budama)
        for t in trader.closed_trades(1000):       # trade_state.json geçmişini kalıcı deftere taşı
            store.add_closed_news_trade(t)
        # Açılış mutabakatı: bot kapalıyken borsa stop'u tetiklenmiş olabilir → hayalet pozisyon.
        rec = trader.reconcile_and_heal(autoclose=trader.S.reconcile_autoclose)
        if rec.get("checked") and rec.get("phantoms"):
            syms = [o["symbol"] for o in rec["phantoms"]]
            _metrics["reconcile_drift_total"] += len(syms)
            log.warning("Mutabakat: borsada bulunmayan %d hayalet pozisyon: %s", len(syms), syms)
            action = (f"{len(rec['healed'])}'i otomatik kapatıldı"
                      if rec.get("healed") else "ELLE kontrol et (oto-kapatma kapalı)")
            notify_remote(f"⚠️ MUTABAKAT: borsada görünmeyen {len(syms)} yerel pozisyon "
                          f"({', '.join(syms)}) — {action}. Bot kapalıyken stop tetiklenmiş olabilir.")
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
    "botpy_order_rejects_total": ("counter", "Borsa emir red/dolmama (canlı)"),
    "botpy_reconcile_drift_total": ("counter", "Mutabakatta bulunan hayalet pozisyon"),
    "botpy_protect_errors_total": ("counter", "Borsa koruyucu stop konamadı"),
    "botpy_failover_scans_total": ("counter", "WS bayatken hızlı yedek tarama (failover)"),
    "botpy_backup_scan_interval_seconds": ("gauge", "Aktif yedek tarama aralığı (failover'da düşer)"),
    "botpy_source_disabled_total": ("counter", "Üst üste hatada devre dışı bırakılan yedek kaynak"),
    "botpy_sources_disabled": ("gauge", "Şu an devre dışı yedek kaynak sayısı"),
    "botpy_halts_total": ("counter", "Operasyonel devre kesici tetiklenme"),
    "botpy_trading_halted": ("gauge", "Operasyonel durdurma aktif mi (1/0)"),
    "botpy_open_positions": ("gauge", "Açık pozisyon sayısı"),
    "botpy_signals_archived": ("gauge", "Arşivlenmiş sinyal sayısı"),
    "botpy_ws_connected": ("gauge", "TreeNews WS bağlı mı (1/0)"),
    "botpy_ws_last_msg_age_seconds": ("gauge", "Son WS mesajından bu yana saniye"),
    "botpy_rate_limited_total": ("counter", "Binance 429/418 rate-limit yanıtı"),
    "botpy_http_retries_total": ("counter", "Dış API yeniden deneme sayısı"),
}


def _render_metrics(values: dict[str, int | float]) -> str:
    """Prometheus exposition formatı (saf).

    Kayıtlı metrikler `_METRIC_META`'dan; dinamik `botpy_latency_*` gauge'leri
    (aşama × istatistik kombinasyonu) jenerik olarak yayınlanır.
    """
    lines = []
    for name, (mtype, help_text) in _METRIC_META.items():
        if name not in values:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        lines.append(f"{name} {values[name]}")
    for name in sorted(values):
        if name.startswith("botpy_latency_"):
            lines.append(f"# HELP {name} Boru hattı gecikme metriği (ms)")
            lines.append(f"# TYPE {name} gauge")
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
        "botpy_order_rejects_total": trader._order_rejects,
        "botpy_reconcile_drift_total": _metrics["reconcile_drift_total"],
        "botpy_protect_errors_total": _metrics["protect_errors_total"],
        "botpy_failover_scans_total": _metrics["failover_scans_total"],
        "botpy_backup_scan_interval_seconds": _scan_interval(),
        "botpy_source_disabled_total": _metrics["source_disabled_total"],
        "botpy_sources_disabled": sum(1 for v in _source_health.snapshot().values() if v["disabled"]),
        "botpy_halts_total": trader._halts,
        "botpy_trading_halted": 1 if trader.get_halt()["active"] else 0,
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
    values.update(latency.get_metrics())   # boru hattı gecikme gauge'leri (p50/p95/max/count)
    return PlainTextResponse(_render_metrics(values), media_type="text/plain; version=0.0.4")


# Boru hattı gecikme SLA'ları (p95 ms) — aşılırsa "yavaş" sayılır (env ile ayarlanır).
# Haber-trade'de yavaş boru hattı = hareketin gerisinde giriş; preflight/health uyarır.
LATENCY_SLA_MS: dict[str, float] = {
    "ingest": float(os.environ.get("SLA_INGEST_MS", "8000")),    # kaynak→bot (besleme+ağ)
    "score": float(os.environ.get("SLA_SCORE_MS", "6000")),      # Claude puanlama batch
    "brain": float(os.environ.get("SLA_BRAIN_MS", "9000")),      # giriş beyni (eskalasyon/oylama)
    "confirm": float(os.environ.get("SLA_CONFIRM_MS", "5000")),  # fiyat teyidi
    "pipeline": float(os.environ.get("SLA_PIPELINE_MS", "12000")),  # alım→emir uçtan uca
}


def _latency_sla() -> dict[str, dict[str, Any]]:
    """Aşama p95'lerini SLA'larla kıyasla (yeterli örneği olanlar)."""
    return latency.evaluate_sla(latency.summary(), LATENCY_SLA_MS)


def _latency_breaches() -> list[str]:
    """SLA'yı aşan (yavaş) aşama adları."""
    return [s for s, v in _latency_sla().items() if not v["ok"]]


@app.get("/latency")
def latency_report() -> dict[str, Any]:
    """Boru hattı gecikme özeti (ms) — haber-trade'in gerçek edge'i.

    Aşamalar: ingest (kaynak→bot), score (Claude puanlama), brain (giriş beyni
    çağrısı — Tier-2'de asıl maliyet), confirm (fiyat teyidi), order (karar→emir),
    pipeline (alım→emir uçtan uca). Her aşama count/avg/p50/p95/max/last. `by_source`:
    kaynak-bazlı ingest kırılımı (hangi besleme yavaş). `sla`: p95'in eşiği aşıp aşmadığı.
    """
    sla = _latency_sla()
    try:
        span = get_store().latency_span()
    except Exception:
        span = {"count": 0, "first_ts": None, "last_ts": None}
    return {"stages": latency.summary(), "by_source": latency.source_summary(),
            "sla": sla, "sla_ok": all(v["ok"] for v in sla.values()),
            "breaches": [s for s, v in sla.items() if not v["ok"]],
            "archive_span": span}


@app.get("/latency/history")
def latency_history(hours: float = 24.0, stage: str | None = None) -> dict[str, Any]:
    """Kalıcı gecikme trendi (restart'a dayanıklı): aşama p50/p95/max zaman serisi.

    Bellekteki `/latency` kayan penceredir; bu, periyodik arşivlenmiş anlık görüntülerden
    (her `LATENCY_SNAPSHOT_EVERY_SEC`) günler boyu trendi döner. `stage` ile tek aşama;
    `hours` ile pencere. "Gerçek edge" zamanla iyileşiyor/bozuluyor mu görmek için."""
    try:
        rows = get_store().latency_history(hours=hours, stage=stage)
        span = get_store().latency_span()
    except Exception as e:
        return {"ok": False, "reason": str(e), "points": []}
    return {"ok": True, "hours": hours, "stage": stage, "span": span, "points": rows}


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
        "backup_scan_interval_sec": _scan_interval(),   # failover'da düşer (hızlı yedek tarama)
        "latency_breaches": _latency_breaches(),         # SLA aşan boru hattı aşamaları (yavaş)
        "rate_limited": get_stats()["rate_limited"],
        "signals_archived": archived,
        "trading_halted": trader.get_halt()["active"],
        "halt_reason": trader.get_halt()["reason"],
    }


@app.get("/sources-health")
def sources_health() -> dict[str, Any]:
    """Yedek kaynak (RSS + Binance) sağlık kaydı: hangi kaynak çalışıyor/devre dışı.

    Üst üste hata veren kaynak geçici devre dışı bırakılır (cooldown sonrası yeniden
    denenir); `disabled`=şu an atlanıyor, `retry_in_sec`=ne zaman tekrar denenecek,
    `consecutive_fails`/`total_*`=istatistik. Asıl realtime kaynak (WS) için /health."""
    snap = _source_health.snapshot()
    return {"sources": snap,
            "disabled": [n for n, v in snap.items() if v["disabled"]],
            "n_sources": len(snap),
            "n_disabled": sum(1 for v in snap.values() if v["disabled"])}


@app.get("/events")
def ops_events(limit: int = 200, kind: str | None = None,
               severity: str | None = None, hours: float | None = None) -> dict[str, Any]:
    """Operasyonel olay zaman çizelgesi (incident günlüğü) — canlı post-mortem için.

    Kalıcı kayıt: feed kopuk/geri, kaynak devre-dışı/toparlandı, gecikme SLA aşıldı/
    düzeldi, devre kesici tetiklendi/temizlendi (yalnız DURUM GEÇİŞLERİ, spam yok).
    `kind`/`severity`/`hours` ile filtrele; en yeniden eskiye. Bildirimler anlık;
    bu, geriye dönük incelenebilir tarihtir."""
    try:
        events = get_store().list_ops_events(limit=limit, kind=kind,
                                              severity=severity, hours=hours)
        span = get_store().ops_event_span()
    except Exception as e:
        return {"ok": False, "reason": str(e), "events": []}
    return {"ok": True, "events": events, "span": span}


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
def auto_preview(limit: int = 20, brain: bool = False) -> dict[str, Any]:
    """Mevcut güçlü haberler için oto-işlem kararı önizlemesi (çalıştırmadan).

    Her haber için hangi gerekçeyle işlem açılır/açılmaz ve hangi boyutta — config
    kalibrasyonu için. Global oto-işlem kapalı olsa da değerlendirir (yan etkisiz).

    `brain=true`: mekanik kapıları geçen (Tier-2) adaylarda **giriş beyni verdiktini** de
    çalıştırır (gir/bekle/veto + konviksiyon + rubrik) — canlıdan önce beyni gözlemle. Her
    aday için 1 Claude çağrısı (ağ-yoğun); talep üzerine kullan, 15s polling'e koyma.
    """
    threshold = get_news_settings()["alert_threshold"]
    with _cache_lock:
        items = [n for n in _news if n.impact >= threshold][:limit]
    use_brain = brain and USE_CLAUDE
    preview = []
    for it in items:
        d = trader.auto_decision(it, **_trade_context(it))
        row: dict[str, Any] = {
            "id": it.id, "title": it.title[:80], "symbol": it.symbol,
            "impact": it.impact, "direction": it.direction,
            "would_trade": d["would_trade"], "reason": d["reason"],
            "side": d["side"], "usdt": d["usdt"],
        }
        # Beyin verdikti: yalnız mekanik geçen + refleks olmayan adayda (canlı yolla aynı koşul)
        if use_brain and d["would_trade"] and d["reason"] != "tier1-refleks":
            try:
                row["brain"] = entry_brain_decision(it, d)
            except Exception as e:
                log.warning("Önizleme beyin hatası (%s): %s", it.symbol, e)
        preview.append(row)
    return {"preview": preview, "auto_trade_on": trader.S.auto_trade, "brain_used": use_brain}


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


@app.get("/ablation")
def ablation(hours: float = 4.0, min_impact: int = ALERT_THRESHOLD, limit: int = 300,
             sl: float = 3.0, tp: float = 6.0, fee: float = 0.2, usdt: float = 100.0,
             chase_pct: float = 5.0, rvol_min: float = 1.5) -> dict[str, Any]:
    """Mekanik sinyal-kalitesi gatelerinin net katkısı — "hangi filtre gerçekten para kazandırıyor?"

    Her arşiv sinyali BİR KEZ simüle edilir (SL/TP), sonra her gate (impact eşiği /
    fiyat-teyidi / RVOL / chase-guard) AÇIK vs KAPALI kıyaslanır: bloklanan işlemlerin
    ort. net'i negatifse gate kaybedeni eliyor = işe yarıyor. Karmaşıklığı veriyle budamak
    için. Beyin katmanları canlı-anlık girdiye dayandığından ablate EDİLMEZ (bkz /brain-backtest).
    Ağ-yoğun (klines indirir), aynı anda tek koşar."""
    import news_backtest as nbt
    with _heavy_guard():
        rows = get_store().list_signals(limit=limit, min_impact=min_impact)
        candidates = nbt._signals_from_rows(rows)
        if not candidates:
            return {"ok": False, "reason": "yeterli sinyal yok (arşiv boş veya çok yeni)", "n": 0}
        signals = nbt.prefetch(candidates, int(hours * 60))
        if not signals:
            return {"ok": False, "reason": "fiyat verisi indirilemedi (Binance)", "n": 0}
        results = nbt.simulate_all(signals, sl, tp, fee)
        if not results:
            return {"ok": False, "reason": "simüle edilebilir sonuç yok", "n": 0}
        return {"ok": True, **nbt.ablation(results, usdt, chase_pct=chase_pct, rvol_min=rvol_min)}


def _ablation_search_impl(hours: float, min_impact: int, limit: int, sl: float, tp: float,
                          fee: float, usdt: float, chase_pct: float, rvol_min: float,
                          min_improve_pct: float) -> dict[str, Any]:
    """Açgözlü çok-gate aramasının ağ-yoğun çekirdeği (search + apply ortak kullanır).
    _heavy_guard ALTINDA çağrılmalı (çağıran tutar)."""
    import news_backtest as nbt
    rows = get_store().list_signals(limit=limit, min_impact=min_impact)
    candidates = nbt._signals_from_rows(rows)
    if not candidates:
        return {"ok": False, "reason": "yeterli sinyal yok (arşiv boş veya çok yeni)", "n": 0}
    signals = nbt.prefetch(candidates, int(hours * 60))
    if not signals:
        return {"ok": False, "reason": "fiyat verisi indirilemedi (Binance)", "n": 0}
    results = nbt.simulate_all(signals, sl, tp, fee)
    if not results:
        return {"ok": False, "reason": "simüle edilebilir sonuç yok", "n": 0}
    return {"ok": True, **nbt.ablation_search(results, usdt, chase_pct=chase_pct,
                                              rvol_min=rvol_min, min_improve_pct=min_improve_pct)}


@app.get("/ablation/search")
def ablation_search(hours: float = 4.0, min_impact: int = ALERT_THRESHOLD, limit: int = 300,
                    sl: float = 3.0, tp: float = 6.0, fee: float = 0.2, usdt: float = 100.0,
                    chase_pct: float = 5.0, rvol_min: float = 1.5,
                    min_improve_pct: float = 0.05) -> dict[str, Any]:
    """Açgözlü çok-gate araması: edge'i en çok artıran gate KOMBİNASYONU + uygulanabilir öneri.

    `/ablation` her gate'i izole ölçer; bu gateleri BİRLİKTE arar (ileri-seçim): boş kümeden
    başlar, her adımda anlamlı iyileşme (kestiği işlemler net-negatif + ≥`min_improve_pct`)
    katan gate'i ekler. `recommended_settings` = canlıya ELLE uygulanabilir ayar fragmanı
    (oto-uygulanmaz; POST /ablation/apply ile korkuluklu uygula). Ağ-yoğun, _heavy_guard."""
    with _heavy_guard():
        return _ablation_search_impl(hours, min_impact, limit, sl, tp, fee, usdt,
                                     chase_pct, rvol_min, min_improve_pct)


@app.post("/ablation/apply", dependencies=[Depends(require_token)])
def post_ablation_apply(hours: float = 4.0, min_impact: int = ALERT_THRESHOLD, limit: int = 300,
                        sl: float = 3.0, tp: float = 6.0, fee: float = 0.2, usdt: float = 100.0,
                        chase_pct: float = 5.0, rvol_min: float = 1.5,
                        min_improve_pct: float = 0.05) -> dict[str, Any]:
    """Ablation aramasının önerdiği gate'leri KORKULUKLARLA uygula (açık kullanıcı eylemi).

    Aramayı sunucuda yeniden koşar (güvenilir öneri) ve `recommended_settings`'i yalnız
    güvenli KARAR-EŞİĞİ alanlarında kıstırarak uygular (`trader.apply_ablation_recommendation`)
    — risk/boyut/kaldıraç ayarlarına dokunmaz. Öneri boşsa no-op. Ağ-yoğun, _heavy_guard."""
    with _heavy_guard():
        res = _ablation_search_impl(hours, min_impact, limit, sl, tp, fee, usdt,
                                    chase_pct, rvol_min, min_improve_pct)
    if not res.get("ok"):
        return {"applied": False, "reason": res.get("reason", "arama başarısız"), "changes": []}
    rec = res.get("recommended_settings") or {}
    if not rec:
        return {"applied": False, "reason": "öneri yok (hiçbir gate anlamlı iyileşme katmadı)",
                "changes": [], "verdict": res.get("verdict")}
    applied = trader.apply_ablation_recommendation(rec)
    if applied["applied"]:
        notify_remote("🔬 Ablation kalibrasyonu: " + ", ".join(
            f"{c['field']} {c['from']}→{c['to']}" for c in applied["changes"]))
    return {**applied, "recommended_settings": rec, "improvement_pct": res.get("improvement_pct")}


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


@app.get("/shadow")
def shadow() -> dict[str, Any]:
    """Shadow-mode (A/B) durumu: aktif aday override'lar + canlı vs aday karar özeti.

    Gölge canlı sinyallerde SANAL çalışır (gerçek emir yok). diverged = aday ayarın canlıdan
    farklı karar verdiği sinyal sayısı; live_trades/shadow_trades = her ayarın giriş sayısı.
    """
    return {"overrides": trader.get_shadow_overrides(), **get_store().shadow_summary()}


class ShadowPatch(BaseModel):
    overrides: dict[str, Any] = {}   # {ayar: aday_değer}; boş → gölge kapat


@app.patch("/shadow", dependencies=[Depends(require_token)])
def shadow_patch(body: ShadowPatch) -> dict[str, Any]:
    """Gölge (aday) ayar senaryosunu ayarla. Boş overrides → gölge kapalı. Yalnız güvenli
    karar-eşiği alanları kabul edilir (para-büyüklüğü/risk tavanları gölgede override edilemez)."""
    applied = trader.set_shadow_overrides(body.overrides)
    return {"overrides": applied, "enabled": bool(applied)}


def _shadow_eval_impl(limit: int, hours: float, sl: float, tp: float, fee: float) -> dict[str, Any]:
    """Gölge kararların SANAL sonucunu (sinyal-sonrası fiyat) hesapla → terfi önerisi.

    Her DIVERGENCE kaydı için sinyalin gerçek net %%'sini backtest'le (Binance klines,
    ağ-yoğun → threadpool) çıkarır, trader.shadow_promotion_advice ile aday-ayar terfi
    ÖNERİSİ üretir. OTO-UYGULAMAZ — yalnız öneri (kontrol kullanıcıda).
    """
    import news_backtest as nbt

    rows = get_store().shadow_summary(limit=limit)["recent"]
    diverged = [r for r in rows if r.get("diverged")]
    if not diverged:
        return {"ok": False, "reason": "değerlendirilecek divergence yok", "n": 0}
    # Gölge kaydını backtest sinyal-satırına çevir (side→direction)
    sig_rows = [{
        "symbol": r["symbol"], "impact": r.get("impact") or 7, "title": "",
        "published": r.get("published"),
        "direction": "bullish" if r.get("side") == "long" else "bearish",
    } for r in diverged if r.get("symbol") and r.get("side")]
    candidates = nbt._signals_from_rows(sig_rows)
    signals = nbt.prefetch(candidates, int(hours * 60)) if candidates else []
    # sinyal-sonucunu (symbol,time) ile eşle → gölge kayıtlarına outcome_pct yaz
    outcome: dict[tuple[str, int], float] = {}
    for s in signals:
        res = nbt.simulate(s, sl, tp, fee)
        if res is not None:
            outcome[(s["symbol"], s["time"])] = res["net_pct"]
    enriched = []
    for r, sr in zip(diverged, sig_rows):
        t = nbt._to_ms(r.get("published"))
        oc = outcome.get((r["symbol"], t)) if t else None
        enriched.append({**r, "outcome_pct": oc})
    advice = trader.shadow_promotion_advice(enriched)
    return {"ok": True, "overrides": trader.get_shadow_overrides(), **advice}


@app.get("/shadow/evaluate")
def shadow_evaluate(limit: int = 500, hours: float = 6, sl: float = 3, tp: float = 6,
                    fee: float = 0.1) -> dict[str, Any]:
    """Gölge kararların sanal sonucundan aday-ayar TERFİ ÖNERİSİ (ağ-yoğun; sync route =
    FastAPI threadpool'da koşar, olay döngüsünü bloklamaz; aynı anda tek koşar).

    recommend=true ise aday ayar canlıdan tutarlı daha iyi → ELLE terfi düşünülebilir
    (PATCH /settings ile). Oto-terfi YOK — kontrol kaybı riskine karşı kullanıcı onayı şart.
    """
    with _heavy_guard():
        return _shadow_eval_impl(limit, hours, sl, tp, fee)


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
    use_entry_brain: bool | None = None
    brain_escalate: bool | None = None
    brain_self_improve: bool | None = None
    cooldown_sec: int | None = None
    halt_trade_on_stale: bool | None = None
    max_news_age_sec: int | None = None
    max_same_direction: int | None = None
    brain_recalibrate: bool | None = None
    brain_recalibrate_min: int | None = None
    brain_vote_count: int | None = None
    use_sl_tp: bool | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    use_atr_exits: bool | None = None
    atr_sl_mult: float | None = None
    atr_tp_mult: float | None = None
    use_atr_trailing: bool | None = None
    atr_trailing_mult: float | None = None
    daily_loss_limit_usdt: float | None = None
    max_total_exposure_usdt: float | None = None
    max_per_coin_usdt: float | None = None
    order_type: str | None = None
    exchange_native_stops: bool | None = None
    reconcile_autoclose: bool | None = None
    auto_halt_on_anomaly: bool | None = None
    slippage_guard_pct: float | None = None
    min_orderbook_usd: float | None = None
    size_by_volume: bool | None = None
    min_rel_volume: float | None = None
    max_book_frac: float | None = None
    time_stop_min: int | None = None
    breakeven_pct: float | None = None
    partial_tp_pct: float | None = None
    partial_tp_frac: float | None = None
    partial_tp_levels: str | None = None
    max_open_risk_usdt: float | None = None
    reduce_after_losses: int | None = None
    size_by_impact: bool | None = None  # (zaten yukarıda var ama açık tutuluyor)
    size_by_kelly: bool | None = None
    kelly_fraction: float | None = None
    kelly_min_trades: int | None = None
    risk_parity: bool | None = None
    target_risk_usdt: float | None = None
    portfolio_risk: bool | None = None
    corr_threshold: float | None = None
    max_portfolio_heat: float | None = None
    rvol_scale_by_impact: bool | None = None
    suppress_losing_sources: bool | None = None
    min_source_samples: int | None = None
    skip_already_priced_pct: float | None = None
    max_funding_rate_pct: float | None = None
    auto_tune: bool | None = None
    use_learned_vetoes: bool | None = None
    regime_adapt: bool | None = None


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


READINESS_MIN_SAMPLES = 50   # canlıya geçiş için min kapanan işlem (heuristik)
READINESS_PF_MIN = 1.3       # min profit factor


@app.get("/readiness")
def readiness() -> dict[str, Any]:
    """Canlıya hazırlık kokpiti: ucuz (ağsız) yerel ölçütleri eşiklere göre değerlendirir.
    Tek bakışta geç/kal/veri-yetersiz verdikti. Edge/veto için /brain-backtest + /veto-review ayrı."""
    perf = trader.get_performance()
    sc = trader.brain_scorecard()
    n = int(perf.get("total_trades", 0))
    pf = perf.get("profit_factor")
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})  # pass|fail|pending

    enough = n >= READINESS_MIN_SAMPLES
    add("Yeterli örnek", "pass" if enough else "pending",
        f"{n}/{READINESS_MIN_SAMPLES} kapanan işlem" + ("" if enough else " — biriktirmeye devam"))
    if not enough:
        add(f"Profit factor ≥ {READINESS_PF_MIN}", "pending", "yeterli veri yok")
        add("Beyin kalibrasyonu", "pending", "yeterli veri yok")
        verdict = "VERİ YETERSİZ — paper'da çalışmaya devam et"
    else:
        if pf is None:
            add(f"Profit factor ≥ {READINESS_PF_MIN}", "pending", "henüz zarar yok (tanımsız)")
        elif pf >= READINESS_PF_MIN:
            add(f"Profit factor ≥ {READINESS_PF_MIN}", "pass", str(pf))
        else:
            add(f"Profit factor ≥ {READINESS_PF_MIN}", "fail", f"{pf} — beklenti zayıf")
        if sc["samples"] < 5:
            add("Beyin kalibrasyonu", "pending", f"{sc['samples']} beyinli işlem — az")
        elif sc["calibrated"]:
            add("Beyin kalibrasyonu", "pass", "konviksiyon↑ → P&L↑")
        else:
            add("Beyin kalibrasyonu", "fail", "yüksek konviksiyon daha iyi P&L üretmiyor")
        statuses = {c["status"] for c in checks}
        if "fail" in statuses:
            verdict = "HENÜZ DEĞİL — ayarla (/tuning/apply, kaybeden kaynağı sustur) ve yeniden ölç"
        elif "pending" in statuses:
            verdict = "GELİŞİYOR — birkaç ölçüt daha veri bekliyor"
        else:
            verdict = "UMUT VERİCİ — /brain-backtest + /brain-veto-review ile edge'i doğrula, sonra MİNİK canlı"
    return {"verdict": verdict, "samples": n, "win_rate": perf.get("win_rate"),
            "profit_factor": pf, "max_drawdown": perf.get("max_drawdown"), "checks": checks,
            "note": "Edge/veto doğrulaması ağ-yoğun, ayrı çalıştır: /brain-backtest (edge_pct>0) "
                    "+ /brain-veto-review (avg_net<0)."}


_PREFLIGHT_RANK = {"critical": 3, "warn": 2, "info": 1, "ok": 0}


def _preflight_checks(probe: bool = False) -> list[dict[str, Any]]:
    """Ön-uçuş kontrol listesini derle: trader ops + besleme/gecikme/bildirim/token
    (+ probe=True ise canlı bağlantı probu, AĞ)."""
    checks = trader.preflight()

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # Besleme sağlığı: WS kopuk/bayatsa kör giriş riski (oto-işlem kapısı da bunu kullanır)
    if not USE_TREENEWS:
        add("Haber beslemesi (WS)", "warn", "TreeNews kapalı — yalnız RSS/polling yedek")
    elif _ws_feed_stale():
        add("Haber beslemesi (WS)", "critical",
            f"BAYAT/KOPUK — son mesaj {_ws_last_msg_age()}s önce (kör giriş riski)")
    elif _ws_state.get("connected"):
        add("Haber beslemesi (WS)", "ok", f"bağlı — son mesaj {_ws_last_msg_age()}s önce")
    else:
        add("Haber beslemesi (WS)", "warn", "henüz bağlanmadı (başlangıç grace)")

    # Boru hattı gecikmesi: yavaşsa hareketin gerisinde gireriz (SLA p95 kontrolü)
    breaches = _latency_breaches()
    if breaches:
        add("Boru hattı gecikmesi (SLA)", "critical" if trader.S.halt_trade_on_latency else "warn",
            f"YAVAŞ — SLA aşan aşama(lar): {', '.join(breaches)}"
            + (" (oto-işlem durdurulur)" if trader.S.halt_trade_on_latency else ""))
    else:
        add("Boru hattı gecikmesi (SLA)", "ok", "tüm aşamalar SLA içinde")

    # Uzak bildirim: masadan uzaktayken sinyal/uyarı alabilmek için
    add("Uzak bildirim (Telegram/Discord)",
        "ok" if getattr(_notifier, "enabled", False) else "warn",
        "etkin" if getattr(_notifier, "enabled", False)
        else "kapalı — masadan uzaktayken uyarı/işlem bildirimi gelmez")

    # Mutasyon uçları koruması (sunucu dışa açılırsa)
    add("API token koruması", "ok" if API_TOKEN else "info",
        "ayarlı (mutasyon uçları korumalı)" if API_TOKEN
        else "yok — yerel kullanımda sorun değil; sunucu dışa açılırsa ayarla")

    # Canlı bağlantı probu (AĞ — auth/saat/bakiye); yalnız istenirse
    if probe:
        pr = trader.connectivity_probe()
        if pr.get("skipped"):
            add("Canlı bağlantı probu", "info", pr.get("reason", "atlandı (paper/anahtar yok)"))
        else:
            for c in pr.get("checks", []):
                add(f"Canlı: {c['check']}", c["status"], c["detail"])
    return checks


def _preflight_verdict(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Kontrol listesinden verdikt + sayım üret (saf)."""
    worst = max((_PREFLIGHT_RANK.get(c["status"], 0) for c in checks), default=0)
    if worst >= 3:
        verdict = "CANLIYA HAZIR DEĞİL — kritik güvenlik eksiği var (aşağıyı düzelt)"
    elif worst == 2:
        verdict = "DİKKATLE — bloke edici yok ama uyarıları gözden geçir"
    else:
        verdict = "OPERASYONEL OLARAK HAZIR — edge için ayrıca /readiness + /brain-backtest"
    counts = {s: sum(1 for c in checks if c["status"] == s)
              for s in ("critical", "warn", "info", "ok")}
    return {"verdict": verdict, "counts": counts}


@app.get("/complexity")
def complexity() -> dict[str, Any]:
    """Karmaşıklık/overfitting denetimi: aktif opt-in katmanlar kanıt tabanıyla uyumlu mu.

    `/ablation` (mekanik gateler) + `/brain-attribution` (beyin katmanları) GERÇEK sonuçla
    edge ölçer; bu ondan ÖNCE gelir — "elimdeki veri bu katmanı açmaya yeter mi?". Her aktif
    katman: structural / data-ready / premature (veri yetersizken açık). 'premature' =
    erken karmaşıklık → veri birikene dek kapat. Yalın taban için `POST /settings/preset/lean`."""
    n = int(trader.get_performance().get("total_trades", 0))
    return trader.complexity_audit(n)


@app.get("/preflight")
def preflight(probe: bool = False) -> dict[str, Any]:
    """Canlıya geçiş operasyonel ön-uçuş: sistem gerçek parayı riske atacak şekilde
    GÜVENLİ yapılandırılmış mı (anahtarlar/koruyucu-stop/risk-limitleri/besleme/bildirim).

    `/readiness` track-record edge'ini (strateji yeterince iyi mi) sorgular; bu ayrı bir
    eksen — ops/konfig güvenliği. `probe=true`: canlı borsa bağlantı probu da koşar
    (auth/saat-kayması/bakiye — AĞ). 'critical' eksik = canlıya geçme (PATCH /settings
    canlı oto-işlemi de bu kritiklerde bloklar)."""
    checks = _preflight_checks(probe=probe)
    res = _preflight_verdict(checks)
    return {**res, "paper_trading": trader.S.paper_trading, "checks": checks,
            "note": "Bu operasyonel/konfig güvenliği ölçer; strateji edge'i için /readiness."}


@app.get("/golive")
def golive(probe: bool = False) -> dict[str, Any]:
    """Canlıya hazırlık kokpiti: iki ekseni TEK verdiktte birleştirir —
    (a) **edge** (`/readiness`: track-record yeterince iyi mi) + (b) **operasyonel
    güvenlik** (`/preflight`: sistem güvenli yapılandırılmış mı). `probe=true` canlı
    bağlantı probunu da koşar (AĞ).

    Nihai verdikt iki tarafın EN KÖTÜSÜdür: ops'ta kritik VEYA edge HENÜZ DEĞİL ise
    canlıya geçme. İkisi de yeşilse → MİNİK canlı başlat (kontrol kullanıcıda)."""
    pre_checks = _preflight_checks(probe=probe)
    pre = _preflight_verdict(pre_checks)
    rd = readiness()
    ops_critical = pre["counts"]["critical"] > 0
    edge_ok = rd["verdict"].startswith("UMUT VERİCİ")
    edge_blocked = rd["verdict"].startswith(("HENÜZ DEĞİL", "VERİ YETERSİZ"))
    if ops_critical:
        verdict = "CANLIYA GEÇME — operasyonel kritik eksik (bkz preflight)"
    elif edge_blocked:
        verdict = f"CANLIYA GEÇME — edge hazır değil ({rd['verdict'].split('—')[0].strip()})"
    elif edge_ok and pre["counts"]["warn"] == 0:
        verdict = "HAZIR — MİNİK canlı başlatmayı düşün (kontrol sende)"
    else:
        verdict = "NEREDEYSE — uyarıları gözden geçir; edge'i /brain-backtest ile doğrula"
    return {
        "verdict": verdict,
        "operational": {**pre, "checks": pre_checks},
        "edge": rd,
        "blockers": [c for c in pre_checks if c["status"] == "critical"],
        "note": "PATCH /settings canlı oto-işlemi operasyonel kritiklerde otomatik bloklar.",
    }


@app.get("/halt")
def get_halt() -> dict[str, Any]:
    """Operasyonel devre kesici durumu (anomalide oto-işlem durdurulur)."""
    return trader.get_halt()


@app.post("/halt/clear", dependencies=[Depends(require_token)])
def clear_halt() -> dict[str, Any]:
    """Devre kesiciyi elle sıfırla (anomali giderildikten sonra oto-işlemi yeniden aç)."""
    return trader.clear_halt()


@app.get("/brain-scorecard")
def brain_scorecard() -> dict[str, Any]:
    """Giriş beyni kalibrasyonu: conviction dilimi → gerçek win-rate/P&L (girilen işlemler).
    `calibrated` = yüksek konviksiyon daha yüksek ort. P&L üretiyor mu (beyin edge katıyor mu)."""
    return trader.brain_scorecard()


@app.get("/brain-attribution")
def brain_attribution() -> dict[str, Any]:
    """Beyin KATMAN atıfı: hangi katman (eskalasyon/oylama/rekalibrasyon/rubrik) gerçek
    kapanmış işlemlerde edge katıyor — tek konsolide rapor. `/ablation` mekanik gateleri
    ölçer; bu beyin katmanlarını ölçer. Her katman: edge+/edge-/yetersiz-veri verdikti.
    Karmaşıklığı veriyle budamak için (edge katmayan katmanı kapatmayı düşün)."""
    return trader.brain_attribution()


@app.get("/brain-log")
def brain_log(limit: int = 200, verdict: str | None = None) -> dict[str, Any]:
    """Giriş beyni karar günlüğü (gir/bekle/veto), en yeniden eskiye. `verdict` ile filtrele.
    Veto/bekle kararları da kaydedilir — scorecard yalnız girilenleri görür, bu hepsini."""
    return {"decisions": get_store().list_brain_decisions(limit=limit, verdict=verdict)}


@app.get("/brain-veto-review")
def brain_veto_review(hours: float = 4.0, limit: int = 300,
                      sl: float | None = None, tp: float | None = None,
                      fee: float = 0.2) -> dict[str, Any]:
    """Veto/bekle hesap verebilirliği: beynin VETOLADIĞI sinyalleri geçmiş fiyatla simüle et.
    `avg_net_pct` < 0 → vetolar kaybedeni eledi (DOĞRU); > 0 → kazananı kaçırdı. Ağ-yoğun."""
    import news_backtest as nbt
    with _heavy_guard():
        rows = [r for r in get_store().list_brain_decisions(limit=limit)
                if r["verdict"] in ("veto", "wait")]
        brain_rows = [{"symbol": r.get("symbol"), "direction": r.get("direction"),
                       "published": r.get("published"), "fetched_at": r.get("published"),
                       "impact": r.get("impact") or 0, "title": r.get("title") or "",
                       "source": r.get("source", "?")} for r in rows]
        cands = nbt._signals_from_rows(brain_rows)
        if not cands:
            return {"ready": False, "reason": "fiyat geçmişi olan vetolanmış karar yok", "n": 0}
        signals = nbt.prefetch(cands, int(hours * 60))
        if not signals:
            return {"ready": False, "reason": "fiyat verisi indirilemedi (Binance)", "n": 0}
        sl_v = sl if sl is not None else trader.S.stop_loss_pct
        tp_v = tp if tp is not None else trader.S.take_profit_pct
        results = nbt.simulate_all(signals, sl_v, tp_v, fee)
        s = _bt_summary([r["net_pct"] for r in results])
        avg = s["avg_net_pct"]
        s["ready"] = True
        s["verdict"] = ("vetolar DOĞRU (kaybedeni eledi)" if (avg is not None and avg < 0)
                        else "vetolar kazananı kaçırdı" if (avg is not None and avg > 0)
                        else "nötr / yetersiz")
        return s


def _item_from_bt(r: dict[str, Any]) -> NewsItem:
    """Backtest sonucundan beyin için minimal NewsItem kur (fiyat alanları yok — arşiv kısıtı)."""
    sym = r["symbol"]
    return NewsItem(id=str(r.get("time", "")), source=r.get("source", "?"),
                    title=r.get("title", ""), url="", published=None, fetched_at=_now_iso(),
                    coins=[sym.replace("USDT", "")], impact=int(r.get("impact", 0)),
                    direction=r.get("direction", "neutral"), symbol=sym, confirmed=True)


def _bt_summary(xs: list[float]) -> dict[str, Any]:
    if not xs:
        return {"n": 0, "avg_net_pct": None, "win_rate": None}
    return {"n": len(xs), "avg_net_pct": round(sum(xs) / len(xs), 3),
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1)}


@app.get("/brain-backtest")
def brain_backtest(hours: float = 4.0, min_impact: int = ALERT_THRESHOLD, limit: int = 40,
                   sl: float | None = None, tp: float | None = None, fee: float = 0.2) -> dict[str, Any]:
    """OFFLINE beyin replay: arşiv sinyallerini geçmiş fiyatlarla simüle edip beynin
    gir/veto kararıyla mekanik tabanı karşılaştırır → beyin edge katıyor mu (para riske atmadan).

    Kısıt: canlı-anlık girdiler (orderbook/BTC-rejimi/küme) geçmişe yeniden kurulamaz → atlanır;
    bu, haber+emsal+kalibrasyon yargısının kazananı kaybedenden AYIRMA gücünü ölçer. Ağ-yoğun
    (sinyal başına 1 Claude çağrısı); aynı anda tek koşar."""
    if not USE_CLAUDE:
        return {"ready": False, "reason": "Claude yok (ANTHROPIC_API_KEY ayarla)"}
    import news_backtest as nbt
    with _heavy_guard():
        rows = get_store().list_signals(limit=limit, min_impact=min_impact)
        cands = nbt._signals_from_rows(rows)
        if not cands:
            return {"ready": False, "reason": "arşiv boş veya sinyaller çok yeni", "tested": 0}
        signals = nbt.prefetch(cands, int(hours * 60))
        if not signals:
            return {"ready": False, "reason": "fiyat verisi indirilemedi (Binance)", "tested": 0}
        sl_v = sl if sl is not None else trader.S.stop_loss_pct
        tp_v = tp if tp is not None else trader.S.take_profit_pct
        results = nbt.simulate_all(signals, sl_v, tp_v, fee)
        enter_net: list[float] = []
        veto_net: list[float] = []
        for r in results:
            side = "long" if r["direction"] == "bullish" else "short"
            v = entry_brain_decision(_item_from_bt(r),
                                     {"side": side, "usdt": 100.0, "news_source": r.get("source", "")},
                                     backtest=True)
            entered = bool(v and v.get("enter") and not (v.get("wait_seconds", 0) > 0))
            (enter_net if entered else veto_net).append(r["net_pct"])
        mech_s = _bt_summary([r["net_pct"] for r in results])
        enter_s, veto_s = _bt_summary(enter_net), _bt_summary(veto_net)
        m, b = mech_s["avg_net_pct"], enter_s["avg_net_pct"]
        edge = round(b - m, 3) if (m is not None and b is not None) else None
        return {"ready": True, "tested": len(results), "sl": sl_v, "tp": tp_v,
                "mechanical": mech_s, "brain_enter": enter_s, "brain_veto": veto_s, "edge_pct": edge}


@app.post("/tuning/apply", dependencies=[Depends(require_token)])
def post_tuning_apply(min_impact_floor: int = 7) -> dict[str, Any]:
    """Öğrenen beynin önerilerini KORKULUKLARLA otomatik uygula (oto-kalibrasyon).

    Kapanan gerçek işlemlerden `suggest_tuning` çalıştırır; güvenli ayarları (auto_min_impact
    tabana kıstırılmış + kaynak susturma) uygular. Risk/boyut ayarlarına dokunmaz. Yeterli
    örnek yoksa hiçbir şey değiştirmez. Uygulanan değişiklikleri döner."""
    sug = trader.suggest_tuning(tier_of=_source_tier)
    result = trader.apply_tuning(sug, min_impact_floor=min_impact_floor)
    if result["applied"]:
        notify_remote("⚙️ Oto-kalibrasyon: " + ", ".join(
            f"{c['field']} {c['from']}→{c['to']}" for c in result["changes"]))
    return result


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
