"""
Haber sinyali çekirdeği — haber metninden Binance spot işlem sinyali üret.

Saf ve test edilebilir (ağ yok). Kaynaklar (Twitter / haber API / webhook)
bu çekirdeği ortak kullanır:

    haber metni → coin sembolü çıkar → bullish/bearish ayır → AL sinyali

Spot + long-only olduğu için yalnızca BULLISH haberlerde alım sinyali üretir;
bearish/nötr haberler atlanır. Yanlış pozitifi azaltmak için sembol çıkarımı
bilinen (Binance'de işlem gören) semboller whitelist'i ile sınırlıdır.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Cashtag: $BTC, $PEPE  (en güvenilir sinyal)
_CASHTAG_RE = re.compile(r"\$([A-Z]{2,15})\b")

# Bullish / bearish katalizör kelimeleri (küçük harfe normalize edilmiş metinde aranır)
BULLISH_WORDS = frozenset({
    "listing", "lists", "listed", "launch", "launches", "mainnet", "partnership",
    "partners", "integration", "integrates", "airdrop", "burn", "buyback",
    "upgrade", "staking", "adoption", "etf", "approval", "approved",
})
BEARISH_WORDS = frozenset({
    "hack", "hacked", "exploit", "exploited", "delisting", "delist", "delisted",
    "lawsuit", "sues", "scam", "rug", "rugpull", "halt", "halted", "suspend",
    "suspended", "ban", "banned", "fud", "downtime", "outage",
})

# "listing" gibi kelimeler sembol sanılmasın diye genel İngilizce kelime engeli
_STOPWORD_TICKERS = frozenset({
    "THE", "FOR", "AND", "NEW", "NOW", "ALL", "ETF", "CEO", "USD", "USDT",
    "BUY", "SELL", "WILL", "HAS", "ARE", "YOU", "API",
})


@dataclass
class NewsItem:
    source: str                 # "twitter" | "newsapi" | "webhook"
    text: str
    url: str | None = None
    ts: str | None = None
    external_id: str | None = None   # kaynak verirse dedup için


@dataclass
class NewsSignal:
    symbol: str        # baz varlık, ör. "PEPE"
    pair: str          # işlem çifti, ör. "PEPEUSDT"
    sentiment: str     # "bullish"
    source: str
    reason: str


def news_key(item: NewsItem) -> str:
    """Dedup anahtarı: external_id varsa onu, yoksa metnin hash'ini kullan."""
    if item.external_id:
        return f"{item.source}:{item.external_id}"
    digest = hashlib.sha256(item.text.strip().lower().encode()).hexdigest()[:16]
    return f"{item.source}:{digest}"


def sentiment(text: str) -> str:
    """Basit katalizör temelli duygu: "bullish" / "bearish" / "neutral"."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    bull = len(words & BULLISH_WORDS)
    bear = len(words & BEARISH_WORDS)
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"


def extract_symbols(
    text: str, known_symbols: set[str], *, max_symbols: int = 3,
) -> list[str]:
    """Metinden aday coin sembolleri çıkar (cashtag öncelikli, sonra whitelist).

    known_symbols: Binance spot'ta işlem gören baz varlıklar (büyük harf).
    """
    known = {s.upper() for s in known_symbols}
    text_up = text.upper()
    found: list[str] = []

    # 1) Cashtag $XXX — en güvenilir
    for m in _CASHTAG_RE.findall(text):
        if m in known and m not in found:
            found.append(m)

    # 2) Whitelist'teki sembolün kelime sınırıyla geçmesi
    for sym in sorted(known, key=len, reverse=True):
        if sym in found or sym in _STOPWORD_TICKERS:
            continue
        if re.search(rf"\b{re.escape(sym)}\b", text_up):
            found.append(sym)

    return found[:max_symbols]


def to_pair(symbol: str, quote: str = "USDT") -> str:
    return f"{symbol.upper()}{quote}"


def evaluate_news(
    item: NewsItem, known_symbols: set[str], *, quote: str = "USDT",
) -> NewsSignal | None:
    """Haberi AL sinyaline çevir — sadece bullish + sembol bulunursa.

    Bearish/nötr haber ya da tanınan sembol yoksa None (işlem yok).
    """
    senti = sentiment(item.text)
    if senti != "bullish":
        return None
    symbols = extract_symbols(item.text, known_symbols)
    if not symbols:
        return None
    sym = symbols[0]
    return NewsSignal(
        symbol=sym,
        pair=to_pair(sym, quote),
        sentiment=senti,
        source=item.source,
        reason=f"bullish haber: {item.text[:80]}",
    )
