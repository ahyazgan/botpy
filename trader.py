"""
Binance işlem modülü — profesyonel haber-trade için.

Modlar:
  • PAPER (varsayılan): gerçek emir GÖNDERMEZ, fiyatı çekip simüle eder. Risksiz.
  • CANLI: CCXT ile Binance'e gerçek emir. .env'de BINANCE_API_KEY/SECRET gerekir.

Profesyonel özellikler:
  - Kalıcılık: pozisyon/işlem/ayarlar JSON dosyada; restart'ı atlatır.
  - Stop-loss / take-profit / trailing stop: otomatik çıkış (monitor_positions).
  - Risk limitleri: günlük zarar freni (circuit breaker), toplam + coin maruziyet sınırı.
  - Emir kalitesi: orderbook derinlik + slippage tahmini, market/limit seçimi.
  - Performans: işlem günlüğü + kazanma oranı / P&L istatistikleri.

GÜVENLİK: Kod borsada "para çekme" iznini denetleyemez — KULLANICI para-çekme-KAPALI
bir API anahtarı oluşturmalıdır.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from netutil import get_json

log = logging.getLogger(__name__)

BINANCE_API = "https://api.binance.com/api/v3"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_state.json")


# ── Ayarlar (çalışırken /settings ile değişir, dosyaya kaydedilir) ───────
class Settings:
    paper_trading: bool = True       # True = simülasyon
    auto_trade: bool = False         # otomatik işlem
    market: str = "spot"             # "spot" | "futures"
    trade_usdt: float = 100.0        # pozisyon başına USDT
    leverage: int = 1                # yalnızca futures
    max_positions: int = 20
    auto_min_impact: int = 8
    auto_require_confirm: bool = True
    tier1_skip_confirm_impact: int = 0  # >0: bu güç ve üstü "net" haberde teyit BEKLEME (refleks giriş)
    cooldown_sec: int = 1800
    # Otomatik çıkış
    use_sl_tp: bool = True
    stop_loss_pct: float = 3.0       # -%3'te zarar durdur
    take_profit_pct: float = 6.0     # +%6'da kâr al
    trailing_stop_pct: float = 0.0   # 0 = kapalı; >0 ise kârı takip eden stop
    # Akıllı çıkış yönetimi
    time_stop_min: int = 0           # >0: bu kadar dk sonra hâlâ açıksa kapat (haber edge'i söndü)
    breakeven_pct: float = 0.0       # >0: +%X kâra ulaşınca SL'i girişe çek (kârı koru)
    partial_tp_pct: float = 0.0      # >0: +%X'te pozisyonun bir kısmını al (scale-out)
    partial_tp_frac: float = 0.5     # kısmi TP'de kapatılacak oran (0-1)
    # Risk limitleri
    daily_loss_limit_usdt: float = 200.0   # günlük gerçekleşen zarar bu USDT'yi geçerse dur (0=kapalı)
    max_total_exposure_usdt: float = 2000.0  # toplam açık pozisyon USDT tavanı (0=kapalı)
    max_per_coin_usdt: float = 500.0       # tek coin için açık pozisyon tavanı (0=kapalı)
    max_open_risk_usdt: float = 0.0  # >0: açık pozisyonların SL'de toplam riski bu USDT'yi geçemez
    reduce_after_losses: int = 0     # >0: son N işlem zararsa boyutu yarıla (kayıp serisi freni)
    # Emir kalitesi
    order_type: str = "market"       # "market" | "limit"
    slippage_guard_pct: float = 0.8  # tahmini slippage bu %'yi geçerse girme (0=kapalı)
    min_orderbook_usd: float = 50_000.0  # girişte orderbook'ta en az bu likidite (0=kapalı)
    size_by_impact: bool = False     # conviction sizing: oto-işlemde güce göre boyutla
    # Sinyal kalitesi / öğrenme
    suppress_losing_sources: bool = False  # negatif beklentili kaynağı oto-işlemde sustur
    min_source_samples: int = 8      # bir kaynağı yargılamak için gereken min kapanmış işlem
    skip_already_priced_pct: float = 0.0   # >0: 24s'te bu % haber yönünde oynamışsa girme (chase önleme)


S = Settings()

_lock = threading.Lock()
_positions: list[dict[str, Any]] = []
_closed: list[dict[str, Any]] = []
_last_trade: dict[str, float] = {}
_daily: dict[str, Any] = {"date": "", "realized": 0.0}
_exchange: Any = None

_PERSIST_KEYS = (
    "paper_trading", "auto_trade", "market", "trade_usdt", "leverage",
    "max_positions", "auto_min_impact", "auto_require_confirm",
    "tier1_skip_confirm_impact", "cooldown_sec",
    "use_sl_tp", "stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
    "daily_loss_limit_usdt", "max_total_exposure_usdt", "max_per_coin_usdt",
    "order_type", "slippage_guard_pct", "min_orderbook_usd", "size_by_impact",
    "time_stop_min", "breakeven_pct", "partial_tp_pct", "partial_tp_frac",
    "max_open_risk_usdt", "reduce_after_losses",
    "suppress_losing_sources", "min_source_samples", "skip_already_priced_pct",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Kalıcılık ────────────────────────────────────────────────────────────
def _save_state() -> None:
    try:
        data = {
            "positions": _positions,
            "closed": _closed[-500:],   # son 500 işlem yeter
            "daily": _daily,
            "settings": {k: getattr(S, k) for k in _PERSIST_KEYS},
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("Durum kaydedilemedi: %s", e)


def load_state() -> None:
    global _positions, _closed, _daily
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        with _lock:
            _positions = data.get("positions", [])
            _closed = data.get("closed", [])
            _daily = data.get("daily", {"date": "", "realized": 0.0})
            for k, v in (data.get("settings") or {}).items():
                if k in _PERSIST_KEYS and v is not None:
                    setattr(S, k, v)
        log.info("Durum yüklendi: %d açık pozisyon, %d kapanmış işlem", len(_positions), len(_closed))
    except Exception as e:
        log.warning("Durum yüklenemedi: %s", e)


# ── Yardımcılar ──────────────────────────────────────────────────────────
def has_live_keys() -> bool:
    return bool(os.environ.get("BINANCE_API_KEY") and os.environ.get("BINANCE_SECRET"))


def get_price(symbol: str) -> float | None:
    data = get_json(f"{BINANCE_API}/ticker/price", params={"symbol": symbol}, timeout=10)
    try:
        return float(data["price"]) if data else None
    except (KeyError, TypeError, ValueError):
        return None


def _estimate_fill(symbol: str, is_long: bool, usdt: float) -> dict[str, Any] | None:
    """Orderbook'tan bu büyüklükteki emrin ortalama dolum fiyatı + slippage + likidite."""
    book = get_json(f"{BINANCE_API}/depth", params={"symbol": symbol, "limit": "50"}, timeout=10)
    if not book:
        return None
    levels = book.get("asks") if is_long else book.get("bids")
    if not levels:
        return None
    best = float(levels[0][0])
    avail = sum(float(p) * float(q) for p, q in levels)
    remaining, cost, qty = usdt, 0.0, 0.0
    for p, q in levels:
        p, q = float(p), float(q)
        take = min(remaining, p * q)
        if p > 0:
            qty += take / p
        cost += take
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0 or qty <= 0:
        return {"avg": None, "slippage": None, "avail": avail, "enough": False, "best": best}
    avg = cost / qty
    slippage = abs(avg - best) / best * 100
    return {"avg": avg, "slippage": slippage, "avail": avail, "enough": True, "best": best}


def _ccxt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol


def _get_exchange() -> Any:
    global _exchange
    if _exchange is None:
        import ccxt
        key = os.environ.get("BINANCE_API_KEY")
        sec = os.environ.get("BINANCE_SECRET")
        if not key or not sec:
            raise RuntimeError("Canlı işlem için .env'de BINANCE_API_KEY ve BINANCE_SECRET gerekli")
        ex = ccxt.binance({"apiKey": key, "secret": sec, "enableRateLimit": True})
        if S.market == "futures":
            ex.options["defaultType"] = "future"
        _exchange = ex
    return _exchange


def _pnl(pos: dict[str, Any], cur: float | None) -> tuple[float | None, float | None]:
    if cur is None:
        return None, None
    diff = (cur - pos["entry_price"]) / pos["entry_price"]
    if pos["side"] == "short":
        diff = -diff
    lev = pos.get("leverage", 1) or 1
    return round(pos["usdt"] * diff * lev, 2), round(diff * lev * 100, 2)


# ── Risk kontrolleri ─────────────────────────────────────────────────────
def _reset_daily_if_needed() -> None:
    if _daily.get("date") != _today():
        _daily["date"] = _today()
        _daily["realized"] = 0.0


def _exposure() -> tuple[float, dict[str, float]]:
    total = 0.0
    per_coin: dict[str, float] = {}
    for p in _positions:
        total += p["usdt"]
        per_coin[p["symbol"]] = per_coin.get(p["symbol"], 0.0) + p["usdt"]
    return total, per_coin


def _position_risk(p: dict[str, Any]) -> float:
    """Pozisyonun SL'de potansiyel zararı (USDT). SL yoksa tüm tutar riskte."""
    sl = p.get("sl_price")
    entry = p.get("entry_price")
    if not sl or not entry:
        return p["usdt"]
    return round(p["usdt"] * abs(entry - sl) / entry, 2)


def _open_risk() -> float:
    """Açık pozisyonların SL'de toplam potansiyel zararı (lock'suz; caller tutar)."""
    return round(sum(_position_risk(p) for p in _positions), 2)


def _losing_streak() -> int:
    """Üst üste kapanmış zararlı işlem sayısı (en yeniden geriye)."""
    with _lock:
        rows = list(_closed)
    n = 0
    for c in reversed(rows):
        if c.get("pnl") is None:
            continue
        if c["pnl"] < 0:
            n += 1
        else:
            break
    return n


def source_stats(news_source: str) -> dict[str, Any]:
    """Bir haber kaynağının kapanmış işlem beklentisi: {count, avg_pnl}."""
    with _lock:
        rows = [c for c in _closed
                if c.get("news_source") == news_source and c.get("pnl") is not None]
    if not rows:
        return {"count": 0, "avg_pnl": 0.0}
    return {"count": len(rows), "avg_pnl": round(sum(c["pnl"] for c in rows) / len(rows), 2)}


def _check_risk(symbol: str, usdt: float) -> None:
    """Risk limitlerini ihlal eden işlemde RuntimeError fırlatır."""
    _reset_daily_if_needed()
    if S.daily_loss_limit_usdt > 0 and _daily["realized"] <= -abs(S.daily_loss_limit_usdt):
        raise RuntimeError(f"Günlük zarar limiti aşıldı ({_daily['realized']:.2f} USDT) — bugün işlem durduruldu")
    total, per_coin = _exposure()
    if S.max_total_exposure_usdt > 0 and total + usdt > S.max_total_exposure_usdt:
        raise RuntimeError(f"Toplam maruziyet tavanı ({S.max_total_exposure_usdt:.0f} USDT) aşılır")
    if S.max_per_coin_usdt > 0 and per_coin.get(symbol, 0.0) + usdt > S.max_per_coin_usdt:
        raise RuntimeError(f"{symbol} için coin maruziyet tavanı ({S.max_per_coin_usdt:.0f} USDT) aşılır")
    if S.max_open_risk_usdt > 0:
        new_risk = usdt * (S.stop_loss_pct / 100) if S.stop_loss_pct > 0 else usdt
        if _open_risk() + new_risk > S.max_open_risk_usdt:
            raise RuntimeError(f"Açık risk tavanı aşılır (SL'de toplam ≤ {S.max_open_risk_usdt:.0f} USDT)")


# ── İşlem açma ───────────────────────────────────────────────────────────
def _find_order(ex: Any, symbol: str, coid: str) -> dict[str, Any] | None:
    """clientOrderId ile borsadaki emri getir (yoksa/hata None)."""
    try:
        return ex.fetch_order(coid, symbol, {"origClientOrderId": coid})
    except Exception:
        return None


def _create_order_idempotent(ex: Any, symbol: str, otype: str, side: str, amount: float,
                             *, price: float | None = None, params: dict[str, Any] | None = None,
                             retries: int = 3, sleep: Any = time.sleep) -> dict[str, Any]:
    """create_order — ÇİFT EMİR'e karşı idempotent.

    Sabit `newClientOrderId` tüm denemelerde aynı kalır → borsa (Binance) aynı
    clientOrderId'li ikinci emri reddeder; yani yanıt kaybolup retry edilse bile
    çift emir oluşmaz. Hata sonrası emir borsada varsa onu döndürür, yoksa dener.
    """
    params = dict(params or {})
    coid = params.get("newClientOrderId") or ("botpy" + uuid.uuid4().hex[:20])
    params["newClientOrderId"] = coid
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return ex.create_order(symbol, otype, side, amount, price, params)
        except Exception as e:
            last_exc = e
            # Yanıt alınamamış olabilir ya da duplicate reddi gelmiş olabilir →
            # bu coid ile emir borsada oluştuysa onu kullan (tekrar gönderme).
            found = _find_order(ex, symbol, coid)
            if found is not None:
                log.warning("create_order tekrar gönderilmedi — emir borsada mevcut (%s)", coid)
                return found
            if attempt < retries - 1:
                sleep(0.5 * (2 ** attempt))
    raise last_exc if last_exc else RuntimeError("emir gönderilemedi")


def place_trade(symbol: str, side: str, usdt: float | None = None,
                source: str = "manual", reason: str = "",
                news_source: str = "", impact: int | None = None) -> dict[str, Any]:
    side = side.lower()
    is_long = side in ("long", "buy")
    if S.market == "spot" and not is_long:
        raise RuntimeError("Spot'ta açığa satış yok — short yalnızca futures'ta")

    usdt = usdt or S.trade_usdt

    with _lock:
        if len(_positions) >= S.max_positions:
            raise RuntimeError(f"Maksimum açık pozisyon ({S.max_positions}) doldu")
        _check_risk(symbol, usdt)

    # Emir kalitesi: orderbook derinlik + slippage tahmini
    est = _estimate_fill(symbol, is_long, usdt)
    if est is not None:
        if S.min_orderbook_usd > 0 and est["avail"] < S.min_orderbook_usd:
            raise RuntimeError(f"Yetersiz likidite (orderbook ${est['avail']:,.0f} < ${S.min_orderbook_usd:,.0f})")
        if not est["enough"]:
            raise RuntimeError("Orderbook bu büyüklüğü karşılayamıyor (çok düşük likidite)")
        if S.slippage_guard_pct > 0 and est["slippage"] is not None and est["slippage"] > S.slippage_guard_pct:
            raise RuntimeError(f"Slippage çok yüksek (%{est['slippage']:.2f} > %{S.slippage_guard_pct}) — giriş iptal")

    price = (est["avg"] if est and est.get("avg") else None) or get_price(symbol)
    if not price:
        raise RuntimeError(f"{symbol} fiyatı alınamadı")
    amount = round(usdt / price, 6)
    mode = "paper" if S.paper_trading else "live"

    if not S.paper_trading:
        ex = _get_exchange()
        csym = _ccxt_symbol(symbol)
        ex_side = "buy" if is_long else "sell"
        if S.market == "futures" and S.leverage > 1:
            try:
                ex.set_leverage(S.leverage, csym)
            except Exception as e:
                log.warning("Kaldıraç ayarlanamadı (%s): %s", csym, e)
        if S.order_type == "limit":
            order = _create_order_idempotent(ex, csym, "limit", ex_side, amount, price=price)
        else:
            order = _create_order_idempotent(ex, csym, "market", ex_side, amount)
        if order.get("average"):
            price = float(order["average"])
        if order.get("filled"):
            amount = float(order["filled"]) or amount

    # SL/TP fiyatları
    sl_price = tp_price = None
    if S.use_sl_tp:
        if is_long:
            if S.stop_loss_pct > 0:
                sl_price = round(price * (1 - S.stop_loss_pct / 100), 8)
            if S.take_profit_pct > 0:
                tp_price = round(price * (1 + S.take_profit_pct / 100), 8)
        else:
            if S.stop_loss_pct > 0:
                sl_price = round(price * (1 + S.stop_loss_pct / 100), 8)
            if S.take_profit_pct > 0:
                tp_price = round(price * (1 - S.take_profit_pct / 100), 8)

    pos = {
        "id": str(uuid.uuid4())[:8],
        "symbol": symbol,
        "side": "long" if is_long else "short",
        "market": S.market,
        "mode": mode,
        "usdt": usdt,
        "entry_price": price,
        "amount": amount,
        "leverage": S.leverage if S.market == "futures" else 1,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "trailing_pct": S.trailing_stop_pct,
        "high_water": price,
        "opened_at": _now(),
        "source": source,
        "news_source": news_source,
        "impact": impact,
        "reason": reason,
    }
    with _lock:
        _positions.append(pos)
        _last_trade[symbol] = time.monotonic()
        _save_state()
    log.info("%s AÇ | %s %s | %.2f USDT @ %.6f | SL=%s TP=%s | %s",
             mode.upper(), pos["side"], symbol, usdt, price, sl_price, tp_price, source)
    return pos


def update_position(pid: str, *, sl_price: float | None = None,
                    tp_price: float | None = None) -> dict[str, Any]:
    """Açık pozisyonun SL/TP'sini güncelle (0/negatif = kaldır). Güncel pozisyonu döner.

    SL/TP yerel olarak `monitor_positions` ile izlenir; borsa emri gerektirmez.
    """
    with _lock:
        pos = next((p for p in _positions if p["id"] == pid), None)
        if pos is None:
            raise RuntimeError("Pozisyon bulunamadı")
        if sl_price is not None:
            pos["sl_price"] = round(float(sl_price), 8) if sl_price > 0 else None
        if tp_price is not None:
            pos["tp_price"] = round(float(tp_price), 8) if tp_price > 0 else None
        _save_state()
        return dict(pos)


def close_position(pid: str, reason: str = "manuel") -> dict[str, Any]:
    with _lock:
        idx = next((i for i, p in enumerate(_positions) if p["id"] == pid), None)
        if idx is None:
            raise RuntimeError("Pozisyon bulunamadı")
        pos = _positions.pop(idx)

    cur = get_price(pos["symbol"])
    if pos["mode"] == "live":
        try:
            ex = _get_exchange()
            csym = _ccxt_symbol(pos["symbol"])
            ex_side = "sell" if pos["side"] == "long" else "buy"
            params = {"reduceOnly": True} if pos["market"] == "futures" else {}
            _create_order_idempotent(ex, csym, "market", ex_side, pos["amount"], params=params)
        except Exception as e:
            log.warning("Canlı kapatma hatası (%s): %s", pos["symbol"], e)

    pnl, pct = _pnl(pos, cur)
    pos["closed_at"] = _now()
    pos["close_price"] = cur
    pos["pnl"] = pnl
    pos["pnl_pct"] = pct
    pos["close_reason"] = reason
    with _lock:
        _closed.append(pos)
        _reset_daily_if_needed()
        if pnl is not None:
            _daily["realized"] = round(_daily["realized"] + pnl, 2)
        _save_state()
    log.info("%s KAPAT | %s %s | P&L=%s USDT | sebep=%s",
             pos["mode"].upper(), pos["side"], pos["symbol"], pnl, reason)
    return pos


def _fetch_exchange_symbols() -> set[str]:
    """Borsada açık görünen pariteler (canlı). futures→pozisyonlar, spot→bakiye."""
    ex = _get_exchange()
    out: set[str] = set()
    if S.market == "futures":
        for p in ex.fetch_positions() or []:
            amt = float(p.get("contracts") or 0)
            if amt:
                sym = str(p.get("symbol", "")).split(":")[0].replace("/", "")
                if sym:
                    out.add(sym)
    else:
        total = (ex.fetch_balance() or {}).get("total") or {}
        for asset, amt in total.items():
            if asset != "USDT" and amt and float(amt) > 0:
                out.add(f"{asset}USDT")
    return out


def reconcile_positions(exchange_symbols: set[str] | None = None) -> dict[str, Any]:
    """Yerel açık pozisyonları borsanın bildirdikleriyle karşılaştır (READ-ONLY).

    `exchange_symbols` verilmezse canlı modda ccxt'ten çekilir (paper'da atlanır).
    Güvenlik: otomatik kapatma YOK — yalnızca uyumsuzlukları (orphan) raporlar.
    """
    with _lock:
        local = [(p["id"], p["symbol"]) for p in _positions]
    if exchange_symbols is None:
        if S.paper_trading or not has_live_keys():
            return {"checked": False, "reason": "paper modu / canlı anahtar yok",
                    "orphans": [], "matched": []}
        try:
            exchange_symbols = _fetch_exchange_symbols()
        except Exception as e:
            return {"checked": False, "reason": f"borsa sorgulanamadı: {e}",
                    "orphans": [], "matched": []}
    orphans = [{"id": pid, "symbol": s} for pid, s in local if s not in exchange_symbols]
    matched = [{"id": pid, "symbol": s} for pid, s in local if s in exchange_symbols]
    return {"checked": True, "orphans": orphans, "matched": matched}


def close_all(reason: str = "toplu-kapat") -> dict[str, Any]:
    """Tüm açık pozisyonları kapat (acil/panic). Detaylı rapor döner.

    Pozisyon başına izole: biri kapanamazsa diğerleri kapanır. Dönen:
    {closed: [...], errors: [{id, symbol, error}], count, failed, total_pnl}.
    """
    with _lock:
        targets = [(p["id"], p["symbol"]) for p in _positions]
    closed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for pid, sym in targets:
        try:
            closed.append(close_position(pid, reason=reason))
        except Exception as e:
            log.warning("Toplu kapatmada hata (%s): %s", sym, e)
            errors.append({"id": pid, "symbol": sym, "error": str(e)})
    total = round(sum((c.get("pnl") or 0.0) for c in closed), 2)
    return {
        "closed": closed,
        "errors": errors,
        "count": len(closed),
        "failed": len(errors),
        "total_pnl": total,
    }


def get_positions() -> tuple[list[dict[str, Any]], float]:
    with _lock:
        snap = list(_positions)
    out: list[dict[str, Any]] = []
    total = 0.0
    for p in snap:
        cur = get_price(p["symbol"])
        pnl, pct = _pnl(p, cur)
        row = dict(p)
        row["current_price"] = cur
        row["pnl"] = pnl
        row["pnl_pct"] = pct
        out.append(row)
        if pnl is not None:
            total += pnl
    return out, round(total, 2)


# ── Otomatik çıkış (SL/TP/trailing) ──────────────────────────────────────
def _parse_dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _partial_close(p: dict[str, Any], frac: float, reason: str, cur: float) -> dict[str, Any] | None:
    """Pozisyonun `frac` oranını kapat (scale-out). Kapanan kısmı kayıt eder,
    canlı pozisyonu küçültür. Kapanan satırı döndürür (canlı emir hatasında None)."""
    frac = max(0.0, min(1.0, frac))
    close_usdt = round(p["usdt"] * frac, 2)
    close_amt = round(p["amount"] * frac, 8)
    if close_amt <= 0 or close_usdt <= 0:
        return None
    if p["mode"] == "live":
        try:
            ex = _get_exchange()
            csym = _ccxt_symbol(p["symbol"])
            ex_side = "sell" if p["side"] == "long" else "buy"
            params = {"reduceOnly": True} if p["market"] == "futures" else {}
            _create_order_idempotent(ex, csym, "market", ex_side, close_amt, params=params)
        except Exception as e:
            log.warning("Kısmi kapatma hatası (%s): %s", p["symbol"], e)
            return None
    pnl, pct = _pnl({**p, "usdt": close_usdt}, cur)
    rec = dict(p)
    rec.update(usdt=close_usdt, amount=close_amt, closed_at=_now(), close_price=cur,
               pnl=pnl, pnl_pct=pct, close_reason=reason)
    p["usdt"] = round(p["usdt"] - close_usdt, 2)
    p["amount"] = round(p["amount"] - close_amt, 8)
    p["partial_done"] = True
    with _lock:
        _closed.append(rec)
        _reset_daily_if_needed()
        if pnl is not None:
            _daily["realized"] = round(_daily["realized"] + pnl, 2)
        _save_state()
    log.info("%s KISMİ TP | %s %s | P&L=%s | kalan %.2f USDT",
             p["mode"].upper(), p["side"], p["symbol"], pnl, p["usdt"])
    return rec


def monitor_positions() -> list[dict[str, Any]]:
    """Açık pozisyonları izle: trailing, breakeven, kısmi TP, SL/TP/time-stop.

    Otomatik kapatılan (tam veya kısmi) pozisyonların listesini döndürür.
    """
    closed: list[dict[str, Any]] = []
    with _lock:
        snap = list(_positions)
    now = datetime.now(timezone.utc)
    for p in snap:
        cur = get_price(p["symbol"])
        if cur is None:
            continue
        is_long = p["side"] == "long"
        entry = p["entry_price"]
        gain = ((cur - entry) / entry * 100) * (1 if is_long else -1)  # haber yönünde % kazanç
        changed = False

        # 1) Trailing stop: kâr yönünde ilerledikçe stop'u çek
        tr = p.get("trailing_pct", 0) or 0
        if tr > 0:
            if is_long and cur > p.get("high_water", cur):
                p["high_water"] = cur
                new_sl = round(cur * (1 - tr / 100), 8)
                if p.get("sl_price") is None or new_sl > p["sl_price"]:
                    p["sl_price"] = new_sl
                    changed = True
            elif not is_long and cur < p.get("high_water", cur):
                p["high_water"] = cur
                new_sl = round(cur * (1 + tr / 100), 8)
                if p.get("sl_price") is None or new_sl < p["sl_price"]:
                    p["sl_price"] = new_sl
                    changed = True

        # 2) Breakeven: +X% kârda SL'i girişe çek (kârı koru)
        if S.breakeven_pct > 0 and not p.get("breakeven_done") and gain >= S.breakeven_pct:
            be = round(entry, 8)
            if (is_long and (p.get("sl_price") is None or be > p["sl_price"])) or \
               (not is_long and (p.get("sl_price") is None or be < p["sl_price"])):
                p["sl_price"] = be
            p["breakeven_done"] = True
            changed = True

        if changed:
            with _lock:
                _save_state()

        # 3) Kısmi TP (scale-out, bir kez)
        if (S.partial_tp_pct > 0 and S.partial_tp_frac > 0
                and not p.get("partial_done") and gain >= S.partial_tp_pct):
            rec = _partial_close(p, S.partial_tp_frac, "partial-tp", cur)
            if rec:
                closed.append(rec)

        # 4) Tam çıkış: SL / TP / time-stop
        hit = None
        sl, tp = p.get("sl_price"), p.get("tp_price")
        if is_long:
            if sl and cur <= sl:
                hit = "stop-loss"
            elif tp and cur >= tp:
                hit = "take-profit"
        else:
            if sl and cur >= sl:
                hit = "stop-loss"
            elif tp and cur <= tp:
                hit = "take-profit"
        if hit is None and S.time_stop_min > 0:
            opened = _parse_dt(p.get("opened_at"))
            if opened and (now - opened).total_seconds() >= S.time_stop_min * 60:
                hit = "time-stop"
        if hit:
            try:
                closed.append(close_position(p["id"], reason=hit))
            except Exception as e:
                log.warning("Otomatik kapatma hatası (%s): %s", p["symbol"], e)
    return closed


# ── Otomatik işlem ───────────────────────────────────────────────────────
def _can_auto_trade(symbol: str) -> bool:
    with _lock:
        if time.monotonic() - _last_trade.get(symbol, 0.0) < S.cooldown_sec:
            return False
        if len(_positions) >= S.max_positions:
            return False
        if any(p["symbol"] == symbol for p in _positions):
            return False
    return True


def _size_multiplier(impact: int) -> float:
    """Conviction çarpanı: yüksek güç = büyük pozisyon. 8'de 1.0x, [0.5x, 1.5x] arası."""
    return max(0.5, min(1.5, 1.0 + (impact - 8) * 0.25))


def auto_decision(item: Any) -> dict[str, Any]:
    """Bir haberin oto-işlem açıp açmayacağına dair YAN ETKİSİZ karar.

    Global `auto_trade` anahtarını dikkate almaz (kalibrasyon/önizleme için her
    sinyali değerlendirir). Dönen: {would_trade, reason, side, usdt, news_source}.
    """
    no = lambda r: {"would_trade": False, "reason": r, "side": None, "usdt": None, "news_source": ""}  # noqa: E731
    if item.impact < S.auto_min_impact:
        return no(f"güç {item.impact} < eşik {S.auto_min_impact}")
    # Tier-1 "net" haber (hack/ETF/büyük listeleme vb. — yüksek güç): teyit beklemeden
    # refleksle gir; hareket başlamadan önde ol. Diğer her şey (Tier-2) teyit bekler.
    tier1 = S.tier1_skip_confirm_impact > 0 and item.impact >= S.tier1_skip_confirm_impact
    if S.auto_require_confirm and not tier1 and not getattr(item, "confirmed", False):
        return no("fiyat teyidi yok")
    symbol = getattr(item, "symbol", None)
    if not symbol:
        return no("parite (symbol) yok")
    if item.direction == "bullish":
        side = "long"
    elif item.direction == "bearish":
        side = "short"
    else:
        return no("yön nötr")
    if S.market == "spot" and side == "short":
        return no("spot'ta short yok")
    if not _can_auto_trade(symbol):
        return no("cooldown / limit / zaten açık pozisyon")
    if S.skip_already_priced_pct > 0:
        m = getattr(item, "price_24h_pct", None)
        if m is not None and ((side == "long" and m >= S.skip_already_priced_pct)
                              or (side == "short" and m <= -S.skip_already_priced_pct)):
            return no(f"zaten fiyatlanmış (24s %{m:+.1f})")
    news_source = getattr(item, "source", "") or ""
    if S.suppress_losing_sources and news_source:
        st = source_stats(news_source)
        if st["count"] >= S.min_source_samples and st["avg_pnl"] < 0:
            return no(f"kaynak negatif beklenti ({news_source} avg={st['avg_pnl']})")
    # Boyut: conviction (güce göre) + kayıp serisi freni
    usdt = S.trade_usdt
    if S.size_by_impact:
        usdt *= _size_multiplier(int(item.impact))
    if S.reduce_after_losses > 0 and _losing_streak() >= S.reduce_after_losses:
        usdt *= 0.5
    return {"would_trade": True, "reason": "tier1-refleks" if tier1 else "uygun",
            "side": side, "usdt": round(usdt, 2), "news_source": news_source}


def maybe_auto_trade(item: Any) -> dict[str, Any] | None:
    if not S.auto_trade:
        return None
    d = auto_decision(item)
    if not d["would_trade"]:
        return None
    try:
        return place_trade(item.symbol, d["side"], usdt=d["usdt"], source="auto",
                           news_source=d["news_source"], impact=int(item.impact),
                           reason=getattr(item, "reason", ""))
    except Exception as e:
        log.warning("Otomatik işlem açılamadı (%s): %s", item.symbol, e)
        return None


# ── Performans ───────────────────────────────────────────────────────────
def _equity_from(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kapanan işlemlerden kronolojik kümülatif P&L eğrisi (saf fonksiyon).

    [{closed_at, pnl, cumulative}, ...] — en eskiden en yeniye.
    """
    curve: list[dict[str, Any]] = []
    cum = 0.0
    for c in closed:
        if c.get("pnl") is None:
            continue
        cum = round(cum + c["pnl"], 2)
        curve.append({"closed_at": c.get("closed_at"), "pnl": c["pnl"], "cumulative": cum})
    return curve


def _max_drawdown(equity: list[dict[str, Any]]) -> float:
    """Kümülatif eğride en büyük tepe-dip düşüş (<= 0). Saf fonksiyon."""
    peak = 0.0
    mdd = 0.0
    for p in equity:
        c = p["cumulative"]
        peak = max(peak, c)
        mdd = min(mdd, c - peak)
    return round(mdd, 2)


def _profit_factor(scored: list[dict[str, Any]]) -> float | None:
    """Brüt kâr / brüt zarar. Zarar yoksa None (tanımsız). Saf fonksiyon."""
    gross_win = sum(c["pnl"] for c in scored if c["pnl"] > 0)
    gross_loss = -sum(c["pnl"] for c in scored if c["pnl"] < 0)
    if gross_loss <= 0:
        return None
    return round(gross_win / gross_loss, 2)


def _perf_ratios(scored: list[dict[str, Any]]) -> dict[str, float | None]:
    """Gelişmiş performans metrikleri (saf): ort. kazanç/kayıp, payoff, Sharpe-benzeri.

    payoff_ratio = ort.kazanç / |ort.kayıp| (>1 iyi). sharpe = işlem başına
    P&L'in ortalama/std oranı (yıllıklandırılmamış; tutarlılık göstergesi).
    """
    pnls = [c["pnl"] for c in scored]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None
    payoff = round(avg_win / abs(avg_loss), 2) if (avg_win and avg_loss) else None
    sharpe: float | None = None
    if len(pnls) >= 2:
        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        sd = var ** 0.5
        if sd > 0:
            sharpe = round(mean / sd, 2)
    return {"avg_win": avg_win, "avg_loss": avg_loss, "payoff_ratio": payoff, "sharpe": sharpe}


def equity_curve() -> list[dict[str, Any]]:
    """Kümülatif P&L eğrisi (kapanan işlemlerden, kronolojik)."""
    with _lock:
        closed = list(_closed)
    return _equity_from(closed)


def closed_trades(limit: int = 200) -> list[dict[str, Any]]:
    """Kapanan işlemler, en yeniden eskiye (işlem günlüğü)."""
    with _lock:
        return list(reversed(_closed[-limit:]))


def get_risk() -> dict[str, Any]:
    """Anlık risk/maruziyet özeti: limitler, kullanım, günlük zarar, kill-switch."""
    with _lock:
        _reset_daily_if_needed()
        total, per_coin = _exposure()
        realized = _daily.get("realized", 0.0)
        n_open = len(_positions)
    daily_limit = S.daily_loss_limit_usdt
    return {
        "open_positions": n_open,
        "max_positions": S.max_positions,
        "total_exposure_usdt": round(total, 2),
        "max_total_exposure_usdt": S.max_total_exposure_usdt,
        "per_coin_exposure": {k: round(v, 2) for k, v in per_coin.items()},
        "max_per_coin_usdt": S.max_per_coin_usdt,
        "realized_today": round(realized, 2),
        "daily_loss_limit_usdt": daily_limit,
        # kill-switch: günlük zarar limiti aşıldıysa bugün yeni işlem açılmaz
        "trading_halted": bool(daily_limit > 0 and realized <= -abs(daily_limit)),
        "paper_trading": S.paper_trading,
        "auto_trade": S.auto_trade,
    }


def daily_summary(date: str | None = None) -> dict[str, Any]:
    """Bir günün (varsayılan bugün) işlem özeti: kapanan işlemler + anlık maruziyet.

    `_daily` reset'inden bağımsız — `_closed`'tan `closed_at` tarihine göre süzer.
    """
    d = date or _today()
    with _lock:
        rows = [c for c in _closed
                if c.get("pnl") is not None and str(c.get("closed_at", "")).startswith(d)]
        total, _ = _exposure()
        n_open = len(_positions)
    pnls = [c["pnl"] for c in rows]
    return {
        "date": d,
        "trades": len(rows),
        "wins": len([p for p in pnls if p > 0]),
        "losses": len([p for p in pnls if p < 0]),
        "realized": round(sum(pnls), 2),
        "best": round(max(pnls, default=0.0), 2),
        "worst": round(min(pnls, default=0.0), 2),
        "open_positions": n_open,
        "open_exposure_usdt": round(total, 2),
    }


def get_performance() -> dict[str, Any]:
    with _lock:
        closed = list(_closed)
        realized_today = _daily.get("realized", 0.0)
    scored = [c for c in closed if c.get("pnl") is not None]
    wins = [c for c in scored if c["pnl"] > 0]
    losses = [c for c in scored if c["pnl"] < 0]
    total = round(sum(c["pnl"] for c in scored), 2)
    equity = _equity_from(closed)

    def _agg(key: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for c in scored:
            k = str(c.get(key) or "?")
            d = out.setdefault(k, {"count": 0, "pnl": 0.0, "wins": 0})
            d["count"] += 1
            d["pnl"] = round(d["pnl"] + c["pnl"], 2)
            if c["pnl"] > 0:
                d["wins"] += 1
        return out

    return {
        "total_trades": len(scored),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(scored) * 100, 1) if scored else 0.0,
        "total_pnl": total,
        "avg_pnl": round(total / len(scored), 2) if scored else 0.0,
        "best": round(max((c["pnl"] for c in scored), default=0.0), 2),
        "worst": round(min((c["pnl"] for c in scored), default=0.0), 2),
        "realized_today": realized_today,
        "by_source": _agg("source"),
        "by_news_source": _agg("news_source"),
        "by_impact": _agg("impact"),
        "by_symbol": _agg("symbol"),
        "by_reason": _agg("close_reason"),
        "recent": list(reversed(closed[-30:])),
        "equity": equity,
        "max_drawdown": _max_drawdown(equity),
        "profit_factor": _profit_factor(scored),
        **_perf_ratios(scored),
    }


# ── Ayarlar ──────────────────────────────────────────────────────────────
def get_settings() -> dict[str, Any]:
    _reset_daily_if_needed()
    total, _ = _exposure()
    return {k: getattr(S, k) for k in _PERSIST_KEYS} | {
        "has_live_keys": has_live_keys(),
        "open_exposure_usdt": round(total, 2),
        "realized_today": _daily.get("realized", 0.0),
    }


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    global _exchange
    market_changed = "market" in patch and patch["market"] != S.market
    for k in _PERSIST_KEYS:
        if k in patch and patch[k] is not None and k not in ("paper_trading", "auto_trade"):
            setattr(S, k, patch[k])
    if "paper_trading" in patch and patch["paper_trading"] is not None:
        S.paper_trading = bool(patch["paper_trading"])
    if "auto_trade" in patch and patch["auto_trade"] is not None:
        if patch["auto_trade"] and not S.paper_trading and not has_live_keys():
            raise RuntimeError("Canlı otomatik işlem için .env'de Binance anahtarları gerekli")
        S.auto_trade = bool(patch["auto_trade"])
    if market_changed:
        _exchange = None
    with _lock:
        _save_state()
    return get_settings()


# ── Çıkış preset'leri ──────────────────────────────────────────────────────
# Haber-trade hamlesi öne yüklüdür: hızlı koru, erken kısmi al, kalanı trailing'le
# sür, süre dolunca kes. "news" preset'i bu davranışı tek tıkla uygular; "safe"
# muhafazakâr varsayılana döner. Yalnızca giriş/çıkış davranışını değiştirir —
# risk tavanları/likidite/anahtarlar dokunulmaz.
PRESETS: dict[str, dict[str, Any]] = {
    "news": {
        "stop_loss_pct": 3.0, "take_profit_pct": 6.0,
        "breakeven_pct": 1.5,                       # +%1.5'te SL girişe (yanlış okumada zararsız çık)
        "partial_tp_pct": 2.5, "partial_tp_frac": 0.5,  # ilk sıçramada yarısını kasaya al
        "trailing_stop_pct": 1.5,                   # kalanı trend devam ederse sür
        "time_stop_min": 60,                        # edge söndüyse 60dk'da kes
        "size_by_impact": True,                     # conviction sizing
        "tier1_skip_confirm_impact": 9,             # güç≥9 net haberde refleks giriş
    },
    "safe": {
        "stop_loss_pct": 3.0, "take_profit_pct": 6.0,
        "breakeven_pct": 0.0,
        "partial_tp_pct": 0.0, "partial_tp_frac": 0.5,
        "trailing_stop_pct": 0.0,
        "time_stop_min": 0,
        "size_by_impact": False,
        "tier1_skip_confirm_impact": 0,
    },
}


def apply_preset(name: str) -> dict[str, Any]:
    """Adlandırılmış çıkış preset'ini uygula (news | safe)."""
    preset = PRESETS.get(name)
    if preset is None:
        raise ValueError(f"bilinmeyen preset: {name} (geçerli: {', '.join(PRESETS)})")
    return update_settings(dict(preset))


# Modül yüklenince kayıtlı durumu geri yükle
load_state()
