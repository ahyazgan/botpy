"""
Backtest — güçlü haber sinyallerinin gerçekten kâr edip etmediğini geçmiş fiyatla ölç.

Mantık: çalışan haber motorundan (/news) güçlü sinyalleri çeker. Her sinyal için
(coin + yön + zaman) Binance geçmiş 1dk mumlarını indirir ve "bu sinyalde girseydim
SL mi TP mi önce vururdu, sonuç ne olurdu" diye simüle eder. Komisyon dahil.

Ayrıca grid search: hangi stop-loss / take-profit kombinasyonu en kârlı olurdu.

NOT (dürüstlük): geçmiş haber arşivi tutmuyoruz; bu yüzden backtest yalnızca
motorun ŞU AN belleğindeki sinyalleri kapsar (motoru ne kadar uzun çalıştırırsan
o kadar çok veri birikir). Gerçek ileri-test için paper modda biriken /performance
istatistikleri esastır. Mum içi SL+TP aynı anda olursa kötümser (SL önce) sayılır.

Kullanım:
  python backtest.py                      # varsayılan SL=3 TP=6, 4 saat pencere
  python backtest.py --sl 2 --tp 5 --hours 6
  python backtest.py --grid               # en iyi SL/TP kombinasyonunu ara
  python backtest.py --min-impact 8 --fee 0.2 --usdt 100
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


def _to_ms(s: str | None) -> int | None:
    """ISO (TreeNews/fetched_at) veya RFC822 (RSS published) tarihini ms'ye çevir."""
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        pass
    try:  # RSS: "Wed, 04 Jun 2026 12:00:00 GMT"
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def fetch_signals(api_base: str, min_impact: int) -> list[dict]:
    r = requests.get(f"{api_base}/news", params={"limit": 300, "min_impact": min_impact}, timeout=15)
    r.raise_for_status()
    out = []
    for n in r.json().get("news", []):
        if not n.get("symbol") or n.get("direction") not in ("bullish", "bearish"):
            continue
        t = _to_ms(n.get("published"))
        if t is None:
            t = _to_ms(n.get("fetched_at"))
        if t is None:
            continue
        # Arkasında en az ~30 dk fiyat verisi olmayan (çok yeni) sinyalleri atla
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if now_ms - t < 30 * 60 * 1000:
            continue
        out.append({
            "symbol": n["symbol"], "direction": n["direction"], "time": t,
            "impact": n["impact"], "title": n["title"][:60],
        })
    return out


def fetch_klines(symbol: str, start_ms: int, minutes: int) -> list[list]:
    try:
        r = requests.get(BINANCE_KLINES, params={
            "symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": min(minutes, 1000),
        }, timeout=15)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


def prefetch(signals: list[dict], minutes: int) -> list[dict]:
    """Her sinyal için klines'ı bir kez çek (grid'de tekrar tekrar çekmemek için)."""
    out = []
    for s in signals:
        s["candles"] = fetch_klines(s["symbol"], s["time"], minutes)
        if len(s["candles"]) >= 2:
            out.append(s)
    return out


def simulate(sig: dict, sl_pct: float, tp_pct: float, fee_pct: float) -> dict | None:
    """Önceden çekilmiş mumlarla tek sinyali simüle et. outcome + net % (komisyon dahil)."""
    candles = sig.get("candles") or []
    if len(candles) < 2:
        return None
    is_long = sig["direction"] == "bullish"
    entry = float(candles[0][1])  # ilk mum açılışı
    if is_long:
        sl = entry * (1 - sl_pct / 100)
        tp = entry * (1 + tp_pct / 100)
    else:
        sl = entry * (1 + sl_pct / 100)
        tp = entry * (1 - tp_pct / 100)

    outcome, gross = "timeout", 0.0
    for c in candles[1:]:
        high = float(c[2]); low = float(c[3])
        if is_long:
            hit_sl = low <= sl
            hit_tp = high >= tp
        else:
            hit_sl = high >= sl
            hit_tp = low <= tp
        if hit_sl and hit_tp:          # aynı mum: kötümser (SL önce)
            outcome, gross = "sl", -sl_pct; break
        if hit_sl:
            outcome, gross = "sl", -sl_pct; break
        if hit_tp:
            outcome, gross = "tp", tp_pct; break
    if outcome == "timeout":
        last = float(candles[-1][4])
        move = (last - entry) / entry * 100
        gross = move if is_long else -move

    net = gross - fee_pct  # gidiş-dönüş komisyon
    return {"outcome": outcome, "net_pct": net, **sig}


def run(signals: list[dict], sl: float, tp: float, fee: float, usdt: float, verbose: bool) -> dict:
    results = []
    for s in signals:
        r = simulate(s, sl, tp, fee)
        if r:
            results.append(r)
    if not results:
        return {"n": 0}
    wins = [r for r in results if r["net_pct"] > 0]
    total_pct = sum(r["net_pct"] for r in results)
    summary = {
        "n": len(results),
        "win_rate": round(len(wins) / len(results) * 100, 1),
        "tp": sum(1 for r in results if r["outcome"] == "tp"),
        "sl": sum(1 for r in results if r["outcome"] == "sl"),
        "timeout": sum(1 for r in results if r["outcome"] == "timeout"),
        "avg_net_pct": round(total_pct / len(results), 3),
        "total_pnl_usdt": round(total_pct / 100 * usdt, 2),
    }
    if verbose:
        for r in sorted(results, key=lambda x: x["net_pct"], reverse=True):
            print(f"  [{r['impact']}/10] {r['symbol']:<10} {r['direction']:<8} "
                  f"{r['outcome']:<7} net%={r['net_pct']:+.2f} | {r['title']}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Haber sinyali backtest")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--min-impact", type=int, default=7)
    ap.add_argument("--sl", type=float, default=3.0)
    ap.add_argument("--tp", type=float, default=6.0)
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--fee", type=float, default=0.2, help="gidiş-dönüş komisyon %% (spot ~0.2)")
    ap.add_argument("--usdt", type=float, default=100.0)
    ap.add_argument("--grid", action="store_true", help="en iyi SL/TP kombinasyonunu ara")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    minutes = int(args.hours * 60)
    print(f"Sinyaller çekiliyor ({args.api}, güç ≥ {args.min_impact})...")
    try:
        signals = fetch_signals(args.api, args.min_impact)
    except Exception as e:
        print(f"HATA: motora bağlanılamadı ({e}). Motor çalışıyor mu?")
        return
    print(f"{len(signals)} aday sinyal — fiyat verisi indiriliyor...")
    signals = prefetch(signals, minutes)
    print(f"{len(signals)} sinyal test edilebilir (yeterli fiyat verisi olan).")
    if not signals:
        print("Yeterli sinyal yok — motoru bir süre çalıştırıp tekrar dene.")
        return

    if args.grid:
        print("\nGrid search (komisyon %{:.1f} dahil, {:.0f}s pencere):".format(args.fee, args.hours))
        print(f"{'SL%':>5}{'TP%':>5}{'n':>5}{'kazanma%':>10}{'ort.net%':>10}{'P&L USDT':>10}")
        best = None
        for sl in (1.5, 2, 3, 4, 5):
            for tp in (2, 3, 5, 6, 8, 10):
                s = run(signals, sl, tp, args.fee, args.usdt, False)
                if s["n"] == 0:
                    continue
                print(f"{sl:>5}{tp:>5}{s['n']:>5}{s['win_rate']:>10}{s['avg_net_pct']:>10}{s['total_pnl_usdt']:>10}")
                if best is None or s["total_pnl_usdt"] > best[1]:
                    best = ((sl, tp), s["total_pnl_usdt"])
        if best:
            print(f"\n>>> En kârlı: SL={best[0][0]}% TP={best[0][1]}% → {best[1]:+.2f} USDT")
        return

    print(f"\nBacktest: SL={args.sl}% TP={args.tp}% pencere={args.hours}s komisyon=%{args.fee}\n")
    s = run(signals, args.sl, args.tp, args.fee, args.usdt, args.verbose)
    print(f"\n{'='*50}")
    print(f"Sinyal sayısı     : {s['n']}")
    print(f"Kazanma oranı     : %{s['win_rate']}")
    print(f"TP / SL / timeout : {s['tp']} / {s['sl']} / {s['timeout']}")
    print(f"Ortalama net      : %{s['avg_net_pct']}")
    print(f"Toplam P&L        : {s['total_pnl_usdt']:+.2f} USDT (pozisyon {args.usdt} USDT)")
    print(f"{'='*50}")
    if s["total_pnl_usdt"] <= 0:
        print("⚠ Bu sinyaller bu ayarlarla kâr etmiyor — eşiği yükselt veya SL/TP'yi --grid ile optimize et.")


if __name__ == "__main__":
    main()
