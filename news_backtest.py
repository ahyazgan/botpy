"""
Backtest — güçlü haber sinyallerinin gerçekten kâr edip etmediğini geçmiş fiyatla ölç.

Mantık: çalışan haber motorundan (/news) güçlü sinyalleri çeker. Her sinyal için
(coin + yön + zaman) Binance geçmiş 1dk mumlarını indirir ve "bu sinyalde girseydim
SL mi TP mi önce vururdu, sonuç ne olurdu" diye simüle eder. Komisyon dahil.

Ayrıca grid search: hangi stop-loss / take-profit kombinasyonu en kârlı olurdu.

Sinyal kaynağı: çalışan motorun /news (RAM) ucu VEYA kalıcı SQLite arşivi
(--db). Motor güçlü sinyalleri arşive yazdığı için (news_bot._archive_signal),
restart'tan bağımsız, günlerce biriken veriyle backtest yapılabilir — motorun
o an çalışıyor olması gerekmez. Mum içi SL+TP aynı anda olursa kötümser (SL
önce) sayılır.

Kullanım:
  python news_backtest.py                      # çalışan motordan (/news, RAM)
  python news_backtest.py --db botpy.db        # kalıcı arşivden (motor gerekmez)
  python news_backtest.py --sl 2 --tp 5 --hours 6
  python news_backtest.py --grid               # en iyi SL/TP kombinasyonunu ara
  python news_backtest.py --min-impact 8 --fee 0.2 --usdt 100
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


def _signals_from_rows(rows: list[dict]) -> list[dict]:
    """Ham sinyal kayıtlarını backtest biçimine süz (ortak filtre)."""
    out = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for n in rows:
        if not n.get("symbol") or n.get("direction") not in ("bullish", "bearish"):
            continue
        t = _to_ms(n.get("published")) or _to_ms(n.get("fetched_at"))
        if t is None:
            continue
        # Arkasında en az ~30 dk fiyat verisi olmayan (çok yeni) sinyalleri atla
        if now_ms - t < 30 * 60 * 1000:
            continue
        out.append({
            "symbol": n["symbol"], "direction": n["direction"], "time": t,
            "impact": n["impact"], "title": n["title"][:60],
        })
    return out


def fetch_signals(api_base: str, min_impact: int) -> list[dict]:
    """Çalışan motorun /news (RAM) ucundan sinyalleri çek."""
    r = requests.get(f"{api_base}/news", params={"limit": 300, "min_impact": min_impact}, timeout=15)
    r.raise_for_status()
    return _signals_from_rows(r.json().get("news", []))


def fetch_signals_from_db(db_path: str, min_impact: int) -> list[dict]:
    """Kalıcı SQLite arşivinden sinyalleri çek (motor çalışmasa da olur)."""
    from storage import Store
    store = Store(db_path)
    try:
        return _signals_from_rows(store.list_signals(limit=5000, min_impact=min_impact))
    finally:
        store.close()


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
        high, low = float(c[2]), float(c[3])
        if is_long:
            hit_sl = low <= sl
            hit_tp = high >= tp
        else:
            hit_sl = high >= sl
            hit_tp = low <= tp
        if hit_sl and hit_tp:          # aynı mum: kötümser (SL önce)
            outcome, gross = "sl", -sl_pct
            break
        if hit_sl:
            outcome, gross = "sl", -sl_pct
            break
        if hit_tp:
            outcome, gross = "tp", tp_pct
            break
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


# Walk-forward için varsayılan SL/TP ızgarası (main --grid ile aynı)
SL_GRID = (1.5, 2, 3, 4, 5)
TP_GRID = (2, 3, 5, 6, 8, 10)


def grid_search(signals: list[dict], fee: float, usdt: float, min_trades: int = 1) -> list[dict]:
    """SL_GRID × TP_GRID üzerinde backtest koş; P&L'e göre azalan sırala.

    Her satır: {sl, tp, n, win_rate, avg_net_pct, total_pnl_usdt}. Sinyaller
    önceden prefetch edilmiş olmalı (klines tekrar tekrar çekilmez).
    """
    rows: list[dict] = []
    for sl in SL_GRID:
        for tp in TP_GRID:
            s = run(signals, sl, tp, fee, usdt, False)
            if s.get("n", 0) < min_trades:
                continue
            rows.append({"sl": sl, "tp": tp, "n": s["n"], "win_rate": s["win_rate"],
                         "avg_net_pct": s["avg_net_pct"], "total_pnl_usdt": s["total_pnl_usdt"]})
    rows.sort(key=lambda r: r["total_pnl_usdt"], reverse=True)
    return rows


def _best_params(signals: list[dict], fee: float, usdt: float, min_trades: int):
    """Verilen sinyallerde en kârlı (SL, TP) kombinasyonunu ara (in-sample)."""
    rows = grid_search(signals, fee, usdt, min_trades)
    if not rows:
        return None
    best = rows[0]
    return best["sl"], best["tp"], run(signals, best["sl"], best["tp"], fee, usdt, False)


def walk_forward(
    signals: list[dict], *, train_frac: float = 0.7, fee: float = 0.2,
    usdt: float = 100.0, min_trades: int = 3,
) -> dict:
    """Sinyalleri zamana göre böl: ilk %train'de SL/TP optimize et, son %test'te ölç.

    In-sample harika ama out-of-sample kötüyse strateji geçmişe uydurulmuştur
    (overfit). Sinyaller önceden prefetch edilmiş (candles dolu) olmalı.
    """
    from walkforward import _verdict  # in/out beklenti karşılaştırması (ortak mantık)

    ordered = sorted(signals, key=lambda s: s["time"])
    cut = int(len(ordered) * train_frac)
    train, test = ordered[:cut], ordered[cut:]
    best = _best_params(train, fee, usdt, min_trades)
    if best is None:
        return {"ok": False, "reason": "in-sample'da yeterli işlem yok", "params": None}

    sl, tp, is_stats = best
    oos = run(test, sl, tp, fee, usdt, False)
    oos_n = oos.get("n", 0)
    oos_avg = oos.get("avg_net_pct", 0.0)
    verdict, degradation = _verdict(is_stats["avg_net_pct"], oos_avg, oos_n)
    return {
        "ok": True,
        "params": {"sl": sl, "tp": tp},
        "in_sample": is_stats,
        "out_of_sample": oos,
        "degradation": degradation,
        "verdict": verdict,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Haber sinyali backtest")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--db", default=None,
                    help="SQLite arşivinden oku (motor çalışmasa da olur). Örn: botpy.db")
    ap.add_argument("--min-impact", type=int, default=7)
    ap.add_argument("--sl", type=float, default=3.0)
    ap.add_argument("--tp", type=float, default=6.0)
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--fee", type=float, default=0.2, help="gidiş-dönüş komisyon %% (spot ~0.2)")
    ap.add_argument("--usdt", type=float, default=100.0)
    ap.add_argument("--grid", action="store_true", help="en iyi SL/TP kombinasyonunu ara")
    ap.add_argument("--walk", action="store_true",
                    help="walk-forward: ilk %%70'te optimize, son %%30'da test (overfit ölç)")
    ap.add_argument("--train-frac", type=float, default=0.7, help="walk-forward eğitim oranı")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    minutes = int(args.hours * 60)
    if args.db:
        print(f"Sinyaller arşivden çekiliyor ({args.db}, güç ≥ {args.min_impact})...")
        try:
            signals = fetch_signals_from_db(args.db, args.min_impact)
        except Exception as e:
            print(f"HATA: arşiv okunamadı ({e}). Yol doğru mu?")
            return
    else:
        print(f"Sinyaller çekiliyor ({args.api}, güç ≥ {args.min_impact})...")
        try:
            signals = fetch_signals(args.api, args.min_impact)
        except Exception as e:
            print(f"HATA: motora bağlanılamadı ({e}). Motor çalışıyor mu? (veya --db ile arşivden oku)")
            return
    print(f"{len(signals)} aday sinyal — fiyat verisi indiriliyor...")
    signals = prefetch(signals, minutes)
    print(f"{len(signals)} sinyal test edilebilir (yeterli fiyat verisi olan).")
    if not signals:
        print("Yeterli sinyal yok — motoru bir süre çalıştırıp tekrar dene.")
        return

    if args.walk:
        wf = walk_forward(signals, train_frac=args.train_frac, fee=args.fee,
                          usdt=args.usdt, min_trades=3)
        print(f"\n{'='*50}")
        print("WALK-FORWARD DOĞRULAMA (overfit testi)")
        if not wf["ok"]:
            print(f"  {wf['reason']}")
            print(f"{'='*50}")
            return
        p, is_s, oos = wf["params"], wf["in_sample"], wf["out_of_sample"]
        print(f"En iyi (in-sample): SL={p['sl']}% TP={p['tp']}%")
        print(f"  in-sample  : n={is_s['n']:<3} kazanma%={is_s['win_rate']:<5} "
              f"ort.net%={is_s['avg_net_pct']:+.3f}")
        oos_n = oos.get("n", 0)
        if oos_n:
            print(f"  out-sample : n={oos_n:<3} kazanma%={oos['win_rate']:<5} "
                  f"ort.net%={oos['avg_net_pct']:+.3f}")
        else:
            print("  out-sample : işlem yok")
        if wf["degradation"] is not None:
            print(f"  zayıflama  : %{wf['degradation']*100:.0f}")
        print(f"  KARAR      : {wf['verdict']}")
        print(f"{'='*50}")
        return

    if args.grid:
        print("\nGrid search (komisyon %{:.1f} dahil, {:.0f}s pencere):".format(args.fee, args.hours))
        print(f"{'SL%':>5}{'TP%':>5}{'n':>5}{'kazanma%':>10}{'ort.net%':>10}{'P&L USDT':>10}")
        rows = grid_search(signals, args.fee, args.usdt)
        for r in rows:
            print(f"{r['sl']:>5}{r['tp']:>5}{r['n']:>5}{r['win_rate']:>10}{r['avg_net_pct']:>10}{r['total_pnl_usdt']:>10}")
        if rows:
            b = rows[0]
            print(f"\n>>> En kârlı: SL={b['sl']}% TP={b['tp']}% → {b['total_pnl_usdt']:+.2f} USDT")
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
