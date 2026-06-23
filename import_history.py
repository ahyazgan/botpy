"""Tarihsel haber içe-aktarıcı — geçmiş haber dataset'ini sinyal arşivine yükler.

"Başkalarının trade'lerini yüklemek" sistemi akıllı YAPMAZ (farklı stratejinin
sonucu = bizim kurallarımız hakkında bilgi taşımaz). Doğru yol: geçmiş HABER+zaman
verisini alıp **kendi kural-puanlayıcımızla** (`news_bot.score_item`) puanlamak ve
`news_signals` arşivine yazmak. Sonra `/alpha`, `/ablation`, `/backtest`, `/montecarlo`,
`/tuning/pretrade` bu geçmiş üzerinde BUGÜN çalışır — gerçek para riske atmadan,
aylarca beklemeden veriden kalibrasyon.

Fiyat verisi import edilmez; backtest sırasında Binance'ten symbol+zaman ile çekilir.
Kullanım: `python import_history.py haberler.csv` (CSV/JSON; esnek sütun adları).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from typing import Any

import news_bot as nb

# Esnek sütun adları (farklı dataset biçimlerini tolere et)
_TITLE_KEYS = ("title", "headline", "text", "en", "body", "news", "message", "content")
_TIME_KEYS = ("published", "time", "date", "timestamp", "datetime", "created_at", "ts", "published_at")
_COIN_KEYS = ("coin", "symbol", "ticker", "asset", "coins", "pair")
_SOURCE_KEYS = ("source", "feed", "origin", "author", "site")

_STABLES = {"USDT", "USDC", "USD", "BUSD", "FDUSD", "DAI", "TUSD", "USD"}


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Satırdan ilk dolu anahtarı seç (büyük/küçük harf duyarsız)."""
    lower = {str(k).lower(): v for k, v in row.items()}
    for k in keys:
        v = lower.get(k)
        if v not in (None, ""):
            return v
    return None


def parse_ts(val: Any) -> str | None:
    """Zaman damgasını ISO-8601'e normalle. epoch (s/ms) | ISO | RFC822 kabul eder. Saf."""
    if val is None or val == "":
        return None
    # Sayısal epoch (saniye veya milisaniye)
    try:
        num = float(val)
        if num > 1e11:      # ms
            num /= 1000.0
        if 1e8 < num < 1e11:  # makul epoch aralığı (~1973-5138)
            return datetime.fromtimestamp(num, timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    dt = nb._parse_time(str(val))   # ISO / RFC822 (news_bot ortak ayrıştırıcı)
    return dt.isoformat() if dt else None


def normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Ham satırı {title, published, coin, source} biçimine indir. Eksikse None. Saf."""
    title = _pick(row, _TITLE_KEYS)
    ts = parse_ts(_pick(row, _TIME_KEYS))
    if not title or not ts:
        return None
    return {"title": str(title).strip(), "published": ts,
            "coin": _pick(row, _COIN_KEYS), "source": _pick(row, _SOURCE_KEYS)}


def _symbol_from_coins(coins: list[str]) -> str | None:
    """İlk işlem-yapılabilir coin'den USDT paritesi (stablecoin atlanır). Saf."""
    for c in coins:
        cu = str(c).upper().replace("USDT", "").replace("/", "").strip()
        if cu and cu not in _STABLES:
            return f"{cu}USDT"
    return None


def build_signal(norm: dict[str, Any], default_source: str = "imported") -> Any:
    """Normalize satırdan, KENDİ kural-puanlayıcımızla puanlanmış NewsItem üret.

    Geçmiş haberi canlı sistemle AYNI şekilde puanlar (impact/direction/coins) ki
    backtest bizim stratejimizi yansıtsın. Sağlanan coin ipucu eklenir, symbol türetilir.
    """
    src = str(norm.get("source") or default_source)
    title = norm["title"]
    pub = norm["published"]
    item = nb.NewsItem(id=nb._news_id(src, "", title), source=src, title=title,
                       url="", published=pub, fetched_at=pub)
    nb.score_item(item)
    coin = norm.get("coin")
    if coin:
        c = str(coin).upper().replace("USDT", "").replace("/", "").strip()
        if c and c not in item.coins:
            item.coins = [c, *item.coins]
    sym = _symbol_from_coins(item.coins)
    if sym:
        item.symbol = sym
    return item


def import_rows(rows: list[dict[str, Any]], *, default_source: str = "imported",
                store: Any = None, min_impact: int = 0) -> dict[str, Any]:
    """Ham satırları puanlayıp arşive yaz. Sayımlarla özet döndürür.

    Yalnız işlem-yapılabilir (symbol + bullish/bearish + impact≥min_impact) sinyaller
    arşivlenir — backtest/alpha bunları kullanır. Diğerleri sebebiyle sayılır.
    `store` verilmezse `news_bot.get_store()` (kalıcı BOTPY_DB).
    """
    st = store if store is not None else nb.get_store()
    imported = dupe = no_field = no_symbol = neutral = low_impact = 0
    for raw in rows:
        norm = normalize_row(raw)
        if norm is None:
            no_field += 1
            continue
        item = build_signal(norm, default_source)
        if not item.symbol:
            no_symbol += 1
            continue
        if item.direction not in ("bullish", "bearish"):
            neutral += 1
            continue
        if item.impact < min_impact:
            low_impact += 1
            continue
        if st.add_signal(item.to_dict()):
            imported += 1
        else:
            dupe += 1
    return {"imported": imported, "total": len(rows), "skipped": {
        "duplicate": dupe, "missing_title_or_time": no_field,
        "no_tradeable_symbol": no_symbol, "neutral_direction": neutral,
        "below_min_impact": low_impact}}


def load_file(path: str) -> list[dict[str, Any]]:
    """CSV veya JSON dosyasından satır listesi yükle (uzantı/içerikten anla)."""
    with open(path, encoding="utf-8") as f:
        head = f.read(64).lstrip()
        f.seek(0)
        if path.endswith(".json") or head[:1] in ("[", "{"):
            data = json.load(f)
            if isinstance(data, dict):  # {"news":[...]} gibi sarmalayıcı
                for v in data.values():
                    if isinstance(v, list):
                        return v
                return [data]
            return list(data)
        return list(csv.DictReader(f))


def main() -> None:
    ap = argparse.ArgumentParser(description="Tarihsel haberi sinyal arşivine yükle")
    ap.add_argument("file", help="CSV veya JSON dosyası (başlık + zaman sütunları)")
    ap.add_argument("--source", default="imported", help="kaynak etiketi (varsayılan: imported)")
    ap.add_argument("--min-impact", type=int, default=0, help="bu güç altını atla")
    args = ap.parse_args()
    rows = load_file(args.file)
    print(f"{len(rows)} satır okundu, puanlanıp arşivleniyor…", file=sys.stderr)
    res = import_rows(rows, default_source=args.source, min_impact=args.min_impact)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n✓ {res['imported']} sinyal arşivlendi. Şimdi: /alpha · /ablation · "
          f"/backtest --db · /montecarlo ile incele.", file=sys.stderr)


if __name__ == "__main__":
    main()
