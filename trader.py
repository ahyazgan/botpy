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
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from netutil import get_json

log = logging.getLogger(__name__)

BINANCE_API = "https://api.binance.com/api/v3"
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"   # futures (funding rate)
STATE_FILE = os.environ.get("BOTPY_STATE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "trade_state.json")


# ── Ayarlar (çalışırken /settings ile değişir, dosyaya kaydedilir) ───────
class Settings:
    paper_trading: bool = True       # True = simülasyon
    auto_trade: bool = False         # otomatik işlem
    market: str = "spot"             # "spot" | "futures"
    trade_usdt: float = 100.0        # pozisyon başına USDT (sabit taban; risk_per_trade_pct kapalıyken)
    # Yüzde-bazlı risk: >0 ise pozisyon TABANI = sermayenin bu %'sini SL'de riske atacak
    # şekilde boyutlanır (sabit lot yerine bakiyeye oranlı). Sermaye = account_equity_usdt
    # + kümülatif realized (P&L ile birleşik). trade_usdt'yi geçersiz kılar.
    risk_per_trade_pct: float = 0.0
    leverage: int = 1                # yalnızca futures
    max_positions: int = 20
    auto_min_impact: int = 8
    auto_require_confirm: bool = True
    tier1_skip_confirm_impact: int = 0  # >0: bu güç ve üstü "net" haberde teyit BEKLEME (refleks giriş)
    use_entry_brain: bool = False    # giriş anında Claude kararlı yargı (Tier-2 adaylarda; refleks atlanır)
    brain_escalate: bool = False     # kararsız konviksiyonda (0.4-0.6) daha güçlü modele ikinci bakış
    brain_self_improve: bool = False # kalibrasyondan öğren: negatif conviction dilimini oto-veto + boyut eğ
    brain_recalibrate: bool = False  # ham conviction'ı geçmiş isabetle düzelt (reliability-bin remap)
    brain_recalibrate_min: int = 20  # bu kadar beyinli-kapanmış işlem yoksa düzeltme yok (ham geçerli)
    brain_vote_count: int = 1        # >1: N bağımsız beyin çağrısı → çoğunluk-oylama (medyan conviction)
    cooldown_sec: int = 1800
    # Güvenlik kapıları (oto-işlem)
    halt_trade_on_stale: bool = True   # haber akışı (WS) kopukken yeni oto-işlem açma
    halt_trade_on_latency: bool = True # boru hattı gecikme SLA'sı aşıldıysa yeni oto-işlem açma
    max_news_age_sec: int = 0          # >0: haber bu kadar saniyeden eskiyse girme (hareket bitti)
    max_same_direction: int = 0        # >0: aynı yönde açık pozisyon sayısı tavanı (korelasyon riski)
    # Otomatik çıkış
    use_sl_tp: bool = True
    stop_loss_pct: float = 3.0       # -%3'te zarar durdur
    take_profit_pct: float = 6.0     # +%6'da kâr al
    trailing_stop_pct: float = 0.0   # 0 = kapalı; >0 ise kârı takip eden stop
    # Volatilite-bazlı çıkış (ATR): sabit % yerine coin oynaklığına göre SL/TP
    use_atr_exits: bool = False      # açıksa SL/TP = çarpan × ATR% (haber teyidinden)
    atr_sl_mult: float = 1.5         # SL = atr_sl_mult × ATR% ([0.5, 15] kıstırılır)
    atr_tp_mult: float = 3.0         # TP = atr_tp_mult × ATR% ([1, 30] kıstırılır)
    use_atr_trailing: bool = False   # açıksa trailing % = atr_trailing_mult × ATR% (oynak coinde geniş)
    atr_trailing_mult: float = 1.0   # trailing = atr_trailing_mult × ATR% ([0.3, 10] kıstırılır)
    # Akıllı çıkış yönetimi
    time_stop_min: int = 0           # >0: bu kadar dk sonra hâlâ açıksa kapat (haber edge'i söndü)
    breakeven_pct: float = 0.0       # >0: +%X kâra ulaşınca SL'i girişe çek (kârı koru)
    partial_tp_pct: float = 0.0      # >0: +%X'te pozisyonun bir kısmını al (scale-out)
    partial_tp_frac: float = 0.5     # kısmi TP'de kapatılacak oran (0-1)
    # Çok-kademeli scale-out: "pct:frac,pct:frac" (örn "3:0.33,6:0.33,10:0.34").
    # Doluysa partial_tp_pct/frac'ı GEÇERSİZ kılar (her kademe ayrı tetiklenir).
    partial_tp_levels: str = ""
    # Risk limitleri
    daily_loss_limit_usdt: float = 200.0   # günlük gerçekleşen zarar bu USDT'yi geçerse dur (0=kapalı)
    max_total_exposure_usdt: float = 2000.0  # toplam açık pozisyon USDT tavanı (0=kapalı)
    max_per_coin_usdt: float = 500.0       # tek coin için açık pozisyon tavanı (0=kapalı)
    max_open_risk_usdt: float = 0.0  # >0: açık pozisyonların SL'de toplam riski bu USDT'yi geçemez
    # Drawdown kill-switch: sermaye tepe-noktadan bu % düşerse yeni işlem DURUR (0=kapalı).
    # account_equity_usdt = drawdown %'sinin paydası (paper sermaye tabanı / canlı nominal sermaye).
    max_drawdown_pct: float = 0.0
    account_equity_usdt: float = 10000.0
    reduce_after_losses: int = 0     # >0: son N işlem zararsa boyutu yarıla (kayıp serisi freni)
    # Bot iç-watchdog: pozisyon-izleme döngüsü takılırsa (SL tetiklenemez) devre kesiciyi aç
    halt_on_monitor_stall: bool = True
    # Emir kalitesi
    order_type: str = "market"       # "market" | "limit"
    exchange_native_stops: bool = True   # canlıda borsaya DURAN SL/TP emri koy (bot çökse de korur)
    reconcile_autoclose: bool = False    # açılış mutabakatında borsada olmayan hayalet pozisyonu kapat
    auto_halt_on_anomaly: bool = True    # anomalide (emir-hata serisi/protect-error) yeni oto-işlemi durdur
    slippage_guard_pct: float = 0.8  # tahmini slippage bu %'yi geçerse girme (0=kapalı)
    min_orderbook_usd: float = 50_000.0  # girişte orderbook'ta en az bu likidite (0=kapalı)
    size_by_impact: bool = False     # conviction sizing: oto-işlemde güce göre boyutla
    # Kelly + risk-eşitleme (boyut matematiği — kazanma istatistiğine bağlar)
    size_by_kelly: bool = False      # fraksiyonel-Kelly: gerçek win-rate+payoff'tan optimal-f çarpanı
    kelly_fraction: float = 0.25     # çeyrek-Kelly (aşırı-bahis önleme); tam-Kelly çok agresif
    kelly_min_trades: int = 20       # bu kadar kapanmış işlem yoksa Kelly nötr (gürültüden öğrenme yok)
    risk_parity: bool = False        # vol-hedef: SL mesafesi geniş işlemde boyutu kıs (sabit USDT-risk)
    target_risk_usdt: float = 0.0    # >0 ise risk-eşitleme hedefi; 0 → trade_usdt'nin %stop_loss'u
    # Portföy-seviye risk: açık pozisyonlar korelasyonluysa "tek bahis" → boyutu kıs
    portfolio_risk: bool = False     # korelasyon-farkında boyut: yeni pozisyon mevcutlarla koreleyse küçült
    corr_threshold: float = 0.6      # bu korelasyonun üstündeki açık pozisyon "aynı bahis" sayılır
    max_portfolio_heat: float = 2.5  # etkin (korelasyon-düzeltilmiş) pozisyon sayısı tavanı
    # Hacim Beyni — profesyonel haber-trade hacim mantığı
    size_by_volume: bool = False     # likidite-katmanlı boyut: ince coinde küçül (exit-trap önleme)
    min_rel_volume: float = 0.0      # >0: RVOL (göreceli hacim) bu katın altındaysa girme (hacimsiz=fake)
    rvol_scale_by_impact: bool = False  # impact-ölçekli RVOL eşiği: yüksek-güç haber daha çok hacim bekler
    max_book_frac: float = 0.0       # >0: pozisyon orderbook derinliğinin en fazla bu oranı olsun (örn 0.10)
    # Sinyal kalitesi / öğrenme
    suppress_losing_sources: bool = False  # negatif beklentili kaynağı oto-işlemde sustur
    min_source_samples: int = 8      # bir kaynağı yargılamak için gereken min kapanmış işlem
    skip_already_priced_pct: float = 0.0   # >0: 24s'te bu % haber yönünde oynamışsa girme (chase önleme)
    auto_tune: bool = False          # kapalı döngü: öğrenen beyin önerilerini OTO-uygula (korkuluklu)
    use_learned_vetoes: bool = False # koşullu öğrenme: anlamlı-negatif segmentte (kaynak×rvol vb.) girme
    regime_adapt: bool = False       # rejim BOZULMASINDA eşiği geçici sıkılaştır; toparlanınca geri al
    max_funding_rate_pct: float = 0.0  # >0 (futures): yön funding'e ters & maliyet bu %'yi geçerse girme


S = Settings()

_lock = threading.Lock()
_positions: list[dict[str, Any]] = []
_closed: list[dict[str, Any]] = []
_last_trade: dict[str, float] = {}
_daily: dict[str, Any] = {"date": "", "realized": 0.0}
_exchange: Any = None

# Operasyonel devre kesici: anomalide yeni oto-işlemi durdurur (manuel işlem etkilenmez)
_halt: dict[str, Any] = {"active": False, "reason": "", "since": ""}

# Rejim adaptasyonu: bozulmada eşik geçici sıkılaştırılır; toparlanınca geri alınır.
# active=True iken `restore` orijinal auto_min_impact'i tutar (kalıcı değil — durum bilgisi).
_regime_state: dict[str, Any] = {"active": False, "restore": None, "bump": 0, "since": ""}
_order_fail_streak = 0
_HALT_FAIL_THRESHOLD = 3   # üst üste bu kadar oto-emir hatası → durdur
_order_rejects = 0         # toplam oto-emir red/dolmama (gözlemlenebilirlik)
_halts = 0                 # devre kesici toplam tetiklenme

_PERSIST_KEYS = (
    "paper_trading", "auto_trade", "market", "trade_usdt", "risk_per_trade_pct", "leverage",
    "max_positions", "auto_min_impact", "auto_require_confirm",
    "tier1_skip_confirm_impact", "use_entry_brain", "brain_escalate",
    "brain_self_improve", "brain_recalibrate", "brain_recalibrate_min",
    "brain_vote_count", "cooldown_sec",
    "halt_trade_on_stale", "halt_trade_on_latency", "max_news_age_sec", "max_same_direction",
    "use_sl_tp", "stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
    "use_atr_exits", "atr_sl_mult", "atr_tp_mult",
    "use_atr_trailing", "atr_trailing_mult",
    "daily_loss_limit_usdt", "max_total_exposure_usdt", "max_per_coin_usdt",
    "max_drawdown_pct", "account_equity_usdt",
    "order_type", "slippage_guard_pct", "min_orderbook_usd", "size_by_impact",
    "size_by_kelly", "kelly_fraction", "kelly_min_trades",
    "risk_parity", "target_risk_usdt",
    "portfolio_risk", "corr_threshold", "max_portfolio_heat",
    "size_by_volume", "min_rel_volume", "rvol_scale_by_impact", "max_book_frac",
    "exchange_native_stops", "reconcile_autoclose", "auto_halt_on_anomaly",
    "halt_on_monitor_stall",
    "time_stop_min", "breakeven_pct", "partial_tp_pct", "partial_tp_frac",
    "partial_tp_levels",
    "max_open_risk_usdt", "reduce_after_losses",
    "suppress_losing_sources", "min_source_samples", "skip_already_priced_pct",
    "max_funding_rate_pct", "auto_tune", "use_learned_vetoes", "regime_adapt",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Operasyonel devre kesici ──────────────────────────────────────────────
def trip_halt(reason: str) -> bool:
    """Anomalide yeni oto-işlemi durdur. Dönen: gerçekten tetiklendi mi (yeni)."""
    global _halts
    if not S.auto_halt_on_anomaly or _halt["active"]:
        return False
    _halts += 1
    _halt.update(active=True, reason=reason, since=_now())
    log.error("⛔ OPERASYONEL DURDURMA: %s — yeni oto-işlem durduruldu (manuel /halt/clear ile aç)", reason)
    return True


def clear_halt() -> dict[str, Any]:
    """Devre kesiciyi elle sıfırla (anomali giderildikten sonra)."""
    global _order_fail_streak
    _order_fail_streak = 0
    _halt.update(active=False, reason="", since="")
    log.info("Operasyonel durdurma temizlendi — oto-işlem yeniden aktif")
    return dict(_halt)


def get_halt() -> dict[str, Any]:
    return dict(_halt)


def _note_order_result(ok: bool) -> bool:
    """Oto-emir sonucunu kaydet; üst üste hata eşiği aşılırsa halt tetikler. Dönen: halt yeni mi."""
    global _order_fail_streak, _order_rejects
    if ok:
        _order_fail_streak = 0
        return False
    _order_rejects += 1
    _order_fail_streak += 1
    if _order_fail_streak >= _HALT_FAIL_THRESHOLD:
        return trip_halt(f"üst üste {_order_fail_streak} oto-emir hatası")
    return False


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


# Fiyat önbelleği: izleme döngüsü (8s) fiyatları tazeler; /positions buradan
# anında okur (her seferinde Binance'e ~700ms ağ gidiş-dönüşü yapmadan).
_price_cache: dict[str, tuple[float, float]] = {}   # symbol -> (price, ts)
_PRICE_TTL = 10.0   # saniye — bu yaştan taze önbellek geçerli


def get_prices(symbols: list[str]) -> dict[str, float]:
    """Birden çok sembolün fiyatını TEK Binance çağrısında çek (seri N çağrı yerine).

    Boş liste → {}. Tekli özel durumu da tek istek. Hata/eksikte o sembol atlanır.
    Pozisyon sayısı arttıkça /positions gecikmesini O(N)→O(1) çağrıya indirir.
    Çekilen fiyatlar önbelleğe yazılır (cached_prices kullanır).
    """
    uniq = sorted({s for s in symbols if s})
    if not uniq:
        return {}
    # Binance: ?symbols=["BTCUSDT","ETHUSDT"] (JSON dizi, boşluksuz)
    sym_param = "[" + ",".join(f'"{s}"' for s in uniq) + "]"
    data = get_json(f"{BINANCE_API}/ticker/price", params={"symbols": sym_param}, timeout=10)
    out: dict[str, float] = {}
    if isinstance(data, list):
        now = time.time()
        for row in data:
            try:
                sym = row["symbol"]
                price = float(row["price"])
            except (KeyError, TypeError, ValueError):
                continue
            out[sym] = price
            _price_cache[sym] = (price, now)
    return out


def cached_prices(symbols: list[str], max_age: float = _PRICE_TTL) -> dict[str, float]:
    """Fiyatları önce önbellekten oku (taze ise); eksik/bayatları TEK çağrıda çek.

    İzleme döngüsü her 8s get_prices ile önbelleği tazelediği için /positions
    çoğu zaman ağ çağrısı yapmadan anında döner.
    """
    now = time.time()
    fresh: dict[str, float] = {}
    missing: list[str] = []
    for s in {x for x in symbols if x}:
        c = _price_cache.get(s)
        if c is not None and now - c[1] <= max_age:
            fresh[s] = c[0]
        else:
            missing.append(s)
    if missing:
        fresh.update(get_prices(missing))   # çeker + önbelleğe yazar
    return fresh


def get_funding_rate(symbol: str) -> float | None:
    """Binance futures anlık funding oranı (% cinsinden, 8 saatlik). Hata/yoksa None.

    Pozitif → longlar shortlara öder (long maliyeti). Negatif → tersi.
    """
    data = get_json(f"{BINANCE_FAPI}/premiumIndex", params={"symbol": symbol}, timeout=10)
    try:
        return float(data["lastFundingRate"]) * 100 if data else None
    except (KeyError, TypeError, ValueError):
        return None


def _premium_index(symbol: str) -> dict[str, float] | None:
    """Binance futures premiumIndex: funding% + perpetual premium% (mark vs index). Auth'suz.

    premium = (markPrice − indexPrice)/indexPrice × 100. Pozitif → futures spot'un üstünde
    (long baskısı/iyimserlik). Tek çağrıda hem funding hem premium. Hata/yoksa None.
    """
    data = get_json(f"{BINANCE_FAPI}/premiumIndex", params={"symbol": symbol}, timeout=10)
    if not data:
        return None
    try:
        mark = float(data["markPrice"])
        index = float(data["indexPrice"])
        funding = float(data["lastFundingRate"]) * 100
    except (KeyError, TypeError, ValueError):
        return None
    premium = (mark - index) / index * 100 if index > 0 else 0.0
    return {"funding_pct": round(funding, 4), "premium_pct": round(premium, 4)}


# Funding/premium "aşırılık" eşikleri (squeeze sinyali için) — futures public veriden
_FUNDING_EXTREME = 0.03   # |funding%| bu üstüyse pozisyonlar kalabalık/aşırı (8s oranı)
_PREMIUM_EXTREME = 0.10   # |premium%| bu üstüyse perpetual spot'tan belirgin sapmış


def liquidation_pressure(symbol: str, side: str,
                         book: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Squeeze/likidasyon-baskısı sinyali (futures public veriden, auth'suz). Saf değerlendirme.

    Aşırı-kalabalık bir yön zorla kapanmaya (squeeze) yatkındır → ters yönde patlama riski.
      - funding NEGATİF & aşırı → shortlar kalabalık → SHORT-SQUEEZE (yukarı patlama, long lehine)
      - funding POZİTİF & aşırı → longlar kalabalık → LONG-SQUEEZE (aşağı, short lehine)
      - premium aynı yönde teyit eder; orderbook skew ters baskıyı gösterirse güç artar.
    `book`: orderbook_imbalance çıktısı (çağıran verir, ekstra ağ olmasın); None → atla.
    Döner: {funding_pct, premium_pct, squeeze, supports_side, score 0-1} ya da None (veri yok).
    `score`: girilen `side` için bu kurulumun ne kadar destekleyici olduğu (squeeze işimize yarıyor mu).
    """
    pi = _premium_index(symbol)
    if pi is None:
        return None
    funding, premium = pi["funding_pct"], pi["premium_pct"]
    squeeze: str | None = None
    if funding <= -_FUNDING_EXTREME:
        squeeze = "short"      # shortlar kalabalık → short-squeeze → YUKARI (long lehine)
    elif funding >= _FUNDING_EXTREME:
        squeeze = "long"       # longlar kalabalık → long-squeeze → AŞAĞI (short lehine)
    # squeeze yönü "kim sıkışacak"; patlama TERS yönde → long-squeeze short'u, short-squeeze long'u destekler
    supports_side: str | None = None
    if squeeze == "short":
        supports_side = "long"
    elif squeeze == "long":
        supports_side = "short"
    # Skor: aşırılık derecesi + premium teyidi + orderbook ters-baskı teyidi
    score = 0.0
    if supports_side:
        score = min(1.0, abs(funding) / (_FUNDING_EXTREME * 3))   # aşırılık derecesi (cap 3×)
        if abs(premium) >= _PREMIUM_EXTREME and (premium < 0) == (supports_side == "long"):
            score = min(1.0, score + 0.2)   # premium squeeze yönünü teyit
        if book and book.get("skew") is not None:
            skew = book["skew"]
            if (supports_side == "long" and skew > 0.2) or (supports_side == "short" and skew < -0.2):
                score = min(1.0, score + 0.2)   # orderbook patlama yönünü teyit
    return {"funding_pct": funding, "premium_pct": premium, "squeeze": squeeze,
            "supports_side": supports_side, "score": round(score, 2),
            "aligned": supports_side == side}


def orderbook_imbalance(symbol: str, depth: int = 20) -> dict[str, Any] | None:
    """Emir defteri dengesizliği: üst `depth` seviyede alıcı/satıcı baskınlığı.

    skew ∈ [-1,1]: +1 alıcı baskın (yukarı baskı), -1 satıcı baskın. Veri yoksa None.
    """
    book = get_json(f"{BINANCE_API}/depth", params={"symbol": symbol, "limit": str(depth)}, timeout=10)
    if not book:
        return None
    try:
        bid_usd = sum(float(p) * float(q) for p, q in book.get("bids", []))
        ask_usd = sum(float(p) * float(q) for p, q in book.get("asks", []))
    except (TypeError, ValueError):
        return None
    tot = bid_usd + ask_usd
    if tot <= 0:
        return None
    return {"skew": round((bid_usd - ask_usd) / tot, 3),
            "bid_usd": round(bid_usd), "ask_usd": round(ask_usd)}


def _funding_cost_pct(symbol: str, side: str) -> float | None:
    """Bu yön için funding MALİYETİ (% — pozitif=ödüyorsun). Veri yoksa None."""
    fr = get_funding_rate(symbol)
    if fr is None:
        return None
    return fr if side == "long" else -fr


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


def precedent_stats(*, news_source: str | None = None, symbol: str | None = None,
                    side: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Emsal: benzer kapanmış işlemlerin gerçek sonucu (kaynak/sembol/yön filtreli).

    Beyne 'bu tür kurulum geçmişte ne yaptı' verir. {n, win_rate, avg_pnl, recent_pnls}.
    """
    with _lock:
        rows = [c for c in _closed if c.get("pnl") is not None
                and (news_source is None or c.get("news_source") == news_source)
                and (symbol is None or c.get("symbol") == symbol)
                and (side is None or c.get("side") == side)]
    rows = rows[-limit:]
    n = len(rows)
    if not n:
        return {"n": 0, "win_rate": None, "avg_pnl": None, "recent_pnls": []}
    wins = sum(1 for c in rows if c["pnl"] > 0)
    return {"n": n, "win_rate": round(wins / n, 2),
            "avg_pnl": round(sum(c["pnl"] for c in rows) / n, 2),
            "recent_pnls": [round(c["pnl"], 2) for c in rows]}


_BRAIN_BANDS = (("0-0.5", 0.0, 0.5), ("0.5-0.7", 0.5, 0.7),
                ("0.7-0.85", 0.7, 0.85), ("0.85-1", 0.85, 1.01))


def brain_scorecard() -> dict[str, Any]:
    """Beyin kalibrasyonu: conviction dilimi → gerçek win-rate/P&L (girilen+kapanan işlemler).

    `calibrated`: yüksek konviksiyon dilimi daha yüksek ort. P&L üretiyor mu (monoton artış).
    Beyin gerçekten edge katıyor mu ölçer (sadece girilen işlemler; veto'lar görülmez).
    """
    with _lock:
        rows = [c for c in _closed if c.get("pnl") is not None and isinstance(c.get("brain"), dict)
                and c["brain"].get("conviction") is not None]
    bands: list[dict[str, Any]] = []
    for name, lo, hi in _BRAIN_BANDS:
        b = [c for c in rows if lo <= float(c["brain"]["conviction"]) < hi]
        if not b:
            bands.append({"band": name, "n": 0, "win_rate": None, "avg_pnl": None})
            continue
        wins = sum(1 for c in b if c["pnl"] > 0)
        bands.append({"band": name, "n": len(b), "win_rate": round(wins / len(b), 2),
                      "avg_pnl": round(sum(c["pnl"] for c in b) / len(b), 2)})
    filled = [x for x in bands if x["n"] > 0]
    calibrated: bool | None = None
    if len(filled) >= 2:
        calibrated = all(filled[i]["avg_pnl"] <= filled[i + 1]["avg_pnl"]
                         for i in range(len(filled) - 1))
    # Kalibrasyon bilimi: conviction'ı kazanma-olasılığı tahmini gibi ölç (gerçek=kârlı mı 1/0)
    pairs = [(float(c["brain"]["conviction"]), 1 if c["pnl"] > 0 else 0) for c in rows]
    sci = _calibration_science(pairs)
    return {"samples": len(rows), "bands": bands, "calibrated": calibrated,
            "escalated_n": sum(1 for c in rows if c["brain"].get("escalated")),
            "escalation": _escalation_accuracy(rows),
            "rubric": _rubric_correlation(rows),
            **sci}


def _agg_pnl(b: list[dict[str, Any]]) -> dict[str, Any]:
    """Kapanan işlem alt-kümesinden n/win_rate/avg_pnl (saf)."""
    if not b:
        return {"n": 0, "win_rate": None, "avg_pnl": None}
    wins = sum(1 for c in b if c["pnl"] > 0)
    return {"n": len(b), "win_rate": round(wins / len(b), 2),
            "avg_pnl": round(sum(c["pnl"] for c in b) / len(b), 2)}


def brain_attribution(min_layer_samples: int = 5) -> dict[str, Any]:
    """Beyin KATMAN atıfı: hangi katman (eskalasyon/oylama/rekalibrasyon/rubrik)
    gerçek kapanmış işlemlerde edge katıyor — tek konsolide rapor.

    Dağınık sinyalleri (brain_scorecard/escalation/rubric) tek "hangi katman para
    kazandırıyor" görünümünde toplar; her katman için verdikt (edge+/edge-/yetersiz).
    Karmaşıklığı veriyle budamak için — bir katman edge katmıyorsa kapatmayı düşün.
    Saf; canlı-anlık girdiler ablate edilemez (bkz /ablation mekanik gateler içindir).
    """
    with _lock:
        rows = [c for c in _closed if c.get("pnl") is not None and isinstance(c.get("brain"), dict)]

    def _verdict(sub: dict[str, Any], ref: dict[str, Any]) -> str:
        if sub["n"] < min_layer_samples or ref.get("avg_pnl") is None or sub["avg_pnl"] is None:
            return "yetersiz-veri"
        return "edge+" if sub["avg_pnl"] > ref["avg_pnl"] else "edge-"

    overall = _agg_pnl(rows)

    # 1) Eskalasyon: Sonnet ikinci-bakış edge katıyor mu (taban = eskale olmayan)
    esc = _escalation_accuracy(rows)
    esc["verdict"] = _verdict(esc["escalated"], esc["base"])

    # 2) Oylama: oybirliği (agreement≈1) vs bölünmüş oy sonuçları
    voted = [c for c in rows if isinstance(c["brain"].get("vote"), dict)]
    unanimous = [c for c in voted if float(c["brain"]["vote"].get("agreement", 0)) >= 0.999]
    split = [c for c in voted if float(c["brain"]["vote"].get("agreement", 0)) < 0.999]
    vote: dict[str, Any] = {"n": len(voted), "unanimous": _agg_pnl(unanimous),
                            "split": _agg_pnl(split)}
    vote["verdict"] = _verdict(vote["unanimous"], vote["split"])

    # 3) Rekalibrasyon: ham conviction düzeltilen işlemler (conviction_raw saklı)
    recal = [c for c in rows if c["brain"].get("conviction_raw") is not None]
    shifts = [float(c["brain"]["conviction"]) - float(c["brain"]["conviction_raw"]) for c in recal]
    recalibration = {**_agg_pnl(recal),
                     "avg_shift": round(sum(shifts) / len(shifts), 3) if shifts else None}

    # 4) Rubrik: hangi alt-skor boyutu P&L ile korele (sinyal taşıyor)
    rubric = _rubric_correlation(rows)
    noisy = [k for k, v in rubric.items() if v is None or abs(v) < 0.1]

    return {
        "samples": len(rows), "overall": overall,
        "layers": {
            "escalation": esc,
            "voting": vote,
            "recalibration": recalibration,
            "rubric": {"correlations": rubric, "noisy_dimensions": noisy},
        },
        "note": "edge+ = katman taban/karşılaştırmadan daha iyi ort. P&L üretti. "
                "Yetersiz-veri katmanlar daha çok kapanmış işlem bekliyor.",
    }


def _calibration_science(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    """Conviction tahmininin kalibrasyon bilimi: Brier + ECE + reliability + base-rate.

    `pairs`: [(conviction 0-1, outcome 0/1), ...]. Conviction'ı kazanma-olasılığı
    tahmini gibi değerlendirir.
      brier = mean((conviction − outcome)²)  (0=mükemmel, 0.25=şans, düşük iyi)
      ece   = Σ |bin'in ort.conviction − bin'in ort.isabet| × ağırlık  (kalibrasyon hatası)
      reliability = [{bin, predicted, actual, n}, ...]  (diyagram noktaları)
      overconfident: ort.conviction > ort.isabet (beyin kendine fazla güveniyor)
    """
    n = len(pairs)
    if n == 0:
        return {"brier": None, "ece": None, "reliability": [], "base_rate": None,
                "mean_conviction": None, "overconfident": None}
    brier = sum((p - o) ** 2 for p, o in pairs) / n
    base_rate = sum(o for _, o in pairs) / n
    mean_conv = sum(p for p, _ in pairs) / n
    # 5 eşit-genişlik bin (0-0.2, ..., 0.8-1.0) — reliability diyagram
    rel: list[dict[str, Any]] = []
    ece = 0.0
    for i in range(5):
        lo, hi = i * 0.2, (i + 1) * 0.2 + (0.01 if i == 4 else 0.0)
        b = [(p, o) for p, o in pairs if lo <= p < hi]
        if not b:
            rel.append({"bin": f"{i*0.2:.1f}-{(i+1)*0.2:.1f}", "predicted": None,
                        "actual": None, "n": 0})
            continue
        bn = len(b)
        pred = sum(p for p, _ in b) / bn
        act = sum(o for _, o in b) / bn
        ece += (bn / n) * abs(pred - act)
        rel.append({"bin": f"{i*0.2:.1f}-{(i+1)*0.2:.1f}", "predicted": round(pred, 3),
                    "actual": round(act, 3), "n": bn})
    return {"brier": round(brier, 4), "ece": round(ece, 4), "reliability": rel,
            "base_rate": round(base_rate, 3), "mean_conviction": round(mean_conv, 3),
            "overconfident": mean_conv > base_rate}


def _escalation_accuracy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Eskalasyon (Haiku→Sonnet) gerçekten isabet katıyor mu: eskale vs eskale-olmayan.

    Eskale edilen işlemler kararsız bant adaylarıydı — Sonnet'in ikinci bakışı bunları
    kurtardı mı (yüksek win-rate/avg_pnl) yoksa para mı yaktı? İstatistik canlı yoldan.
    """
    esc = [c for c in rows if c["brain"].get("escalated")]
    base = [c for c in rows if not c["brain"].get("escalated")]

    def _agg(b: list[dict[str, Any]]) -> dict[str, Any]:
        if not b:
            return {"n": 0, "win_rate": None, "avg_pnl": None}
        wins = sum(1 for c in b if c["pnl"] > 0)
        return {"n": len(b), "win_rate": round(wins / len(b), 2),
                "avg_pnl": round(sum(c["pnl"] for c in b) / len(b), 2)}

    return {"escalated": _agg(esc), "base": _agg(base)}


_RUBRIC_KEYS = ("chase_risk", "fade_risk", "liquidity", "source_quality", "correlation_risk")


def _rubric_correlation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Rubrik alt-skorları (chase/fade/liquidity/...) gerçek sonuçla korele mi.

    Her rubrik skoru ile P&L arasında Pearson korelasyonu. Beklenti: chase_risk/
    fade_risk/correlation_risk NEGATİF (yüksek=kötü), liquidity/source_quality POZİTİF.
    İşaret beklentiyle ters/sıfırsa o rubrik boyutu sinyal taşımıyor → beyin gürültü
    üretiyor. Saf; <3 örnek veya sabit skor → None.
    """
    out: dict[str, Any] = {}
    for key in _RUBRIC_KEYS:
        xs: list[float] = []
        ys: list[float] = []
        for c in rows:
            sc = c["brain"].get("scores")
            if isinstance(sc, dict) and sc.get(key) is not None:
                xs.append(float(sc[key]))
                ys.append(float(c["pnl"]))
        out[key] = _pearson(xs, ys)
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson korelasyon katsayısı (saf). <3 nokta veya sıfır varyans → None."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / math.sqrt(sxx * syy), 3)


def _isotonic(pairs: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """Monoton (azalmayan) conviction→win-rate eğrisi — Pool Adjacent Violators (PAV).

    `pairs`: [(conviction, outcome 0/1), ...]. Conviction'a göre sıralar, ardından
    komşu ihlalleri (sonraki nokta öncekinden düşük) ağırlıklı ortalamayla birleştirir
    → azalmayan basamak fonksiyonu. Döner: [(conviction_eşiği, kalibre_olasılık), ...]
    artan conviction sırasında. Aşırı-güveni düzeltir: conviction 0.9 ama gerçek 0.4
    ise eğri 0.9'u ~0.4'e indirir. Saf fonksiyon.
    """
    if not pairs:
        return []
    # Önce AYNI conviction değerini tek noktaya topla (eşit-conviction kazanç+kayıp
    # tek grup olmalı; aksi halde PAV ekleme sırasına bağlı yanlış böler).
    agg: dict[float, list[float]] = {}
    for c, o in pairs:
        a = agg.setdefault(round(c, 4), [0.0, 0.0])  # [toplam_outcome, n]
        a[0] += float(o)
        a[1] += 1.0
    # Soldan sağa yığınla; her blok [toplam_outcome, ağırlık(=n), toplam_conviction(×ağırlık)].
    # Temsili conviction = blok ORTALAMA conviction. Yeni blok eklenince önceki blok
    # ortalaması daha büyükse (monotonluk ihlali) birleştir, zinciri geriye çöz (PAV).
    stack: list[list[float]] = []
    for c in sorted(agg):
        tot_o, w = agg[c]
        stack.append([tot_o, w, c * w])
        while len(stack) >= 2 and stack[-2][0] / stack[-2][1] > stack[-1][0] / stack[-1][1]:
            last = stack.pop()
            stack[-1][0] += last[0]
            stack[-1][1] += last[1]
            stack[-1][2] += last[2]
    return [(round(b[2] / b[1], 4), round(b[0] / b[1], 4)) for b in stack]


def _fit_calibration(pairs: list[tuple[float, int]], min_n: int) -> dict[str, Any]:
    """Beyin conviction kalibrasyon haritası (geçmiş gir→sonuç'tan). Saf fonksiyon.

    Yeterli örnek (≥min_n) varsa isotonic eğri fit eder; yoksa kimlik (ham geçerli).
    Döner: {ready, curve, n}. curve: artan conviction → kalibre olasılık basamakları.
    """
    n = len(pairs)
    if n < max(2, min_n):
        return {"ready": False, "curve": [], "n": n}
    curve = _isotonic(pairs)
    # Tek blok bile geçerli kalibrasyon (tüm conviction'ları gerçek orana çeker —
    # aşırı-güveni düzeltir). Yalnız boş eğri hazır değil.
    return {"ready": len(curve) >= 1, "curve": curve, "n": n}


def _apply_calibration(conv: float, curve: list[tuple[float, float]]) -> float:
    """Ham conviction'ı kalibrasyon eğrisinden geçir (basamak ara değer). Saf.

    Eğri [(eşik, olasılık), ...] artan. conv'a en yakın alt-eşiğin kalibre değerini
    döndürür (basamak fonksiyonu). Eğri boşsa conv aynen döner.
    """
    if not curve:
        return conv
    out = curve[0][1]
    for thr, prob in curve:
        if conv >= thr:
            out = prob
        else:
            break
    return round(max(0.0, min(1.0, out)), 4)


def recalibrate_conviction(conv: float) -> dict[str, Any]:
    """Ham beyin conviction'ını geçmiş isabetle düzelt (opt-in). Döner: {value, adjusted, raw}.

    brain_recalibrate kapalıysa ya da yeterli beyinli-kapanmış işlem yoksa ham geçerli.
    Aşırı-güveni kalibrasyon eğrisiyle bastırır (conviction 0.9 ama o bantta gerçek
    win-rate 0.4 ise → ~0.4). Boyut/veto bu düzeltilmiş değeri kullanır.
    """
    if not S.brain_recalibrate:
        return {"value": conv, "adjusted": False, "raw": conv}
    with _lock:
        pairs = [(float(c["brain"]["conviction"]), 1 if c["pnl"] > 0 else 0)
                 for c in _closed
                 if c.get("pnl") is not None and isinstance(c.get("brain"), dict)
                 and c["brain"].get("conviction") is not None]
    fit = _fit_calibration(pairs, S.brain_recalibrate_min)
    if not fit["ready"]:
        return {"value": conv, "adjusted": False, "raw": conv}
    cal = _apply_calibration(conv, fit["curve"])
    return {"value": cal, "adjusted": True, "raw": conv}


_BRAIN_SELF_IMPROVE_MIN = 5   # bir conviction dilimini yargılamak için min örnek


def _brain_band_stats(conviction: float) -> dict[str, Any] | None:
    """Verili conviction'ın düştüğü kalibrasyon dilimi (kendini-iyileştirme için)."""
    sc = brain_scorecard()
    for (name, lo, hi), band in zip(_BRAIN_BANDS, sc["bands"]):
        if lo <= conviction < hi:
            return band
    return None


def _check_risk(symbol: str, usdt: float) -> None:
    """Risk limitlerini ihlal eden işlemde RuntimeError fırlatır."""
    _reset_daily_if_needed()
    if S.daily_loss_limit_usdt > 0 and _daily["realized"] <= -abs(S.daily_loss_limit_usdt):
        raise RuntimeError(f"Günlük zarar limiti aşıldı ({_daily['realized']:.2f} USDT) — bugün işlem durduruldu")
    # Drawdown kill-switch: sermaye tepe-noktadan çok düştüyse yeni işlemi durdur
    if S.max_drawdown_pct > 0:
        dd = _drawdown_state(_closed, S.account_equity_usdt)
        if dd["drawdown_pct"] >= S.max_drawdown_pct:
            raise RuntimeError(
                f"Drawdown kill-switch: sermaye tepeden %{dd['drawdown_pct']:.1f} düştü "
                f"(limit %{S.max_drawdown_pct:.1f}) — işlem durduruldu")
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


class OrderError(RuntimeError):
    """Borsa emrinin gönderilememesi/dolmaması (operasyonel hata — devre kesici sayar).
    Doğrulama reddinden (slippage/minNotional/risk → düz RuntimeError) ayrılır."""


def _verify_fill(ex: Any, order: dict[str, Any], csym: str) -> dict[str, Any]:
    """Gönderilen emrin gerçekten DOLDUĞUNU doğrula (ters-hayalet önleme).

    Dolum bilgisi yoksa fetch_order ile teyit eder. Kesin dolmama/iptal/red → OrderError.
    Belirsizse (sorgu da başarısız) dokunmaz — mutabakat yakalar.
    """
    _TERMINAL = ("canceled", "rejected", "expired")
    filled = float(order.get("filled") or 0)
    status = order.get("status")
    # Dolum görünmüyor ama terminal de değil → fetch_order ile kesin durumu öğren
    if filled <= 0 and status not in _TERMINAL and order.get("id"):
        try:
            o2 = ex.fetch_order(order["id"], csym)
            filled = float(o2.get("filled") or 0)
            status = o2.get("status") or status
            order = o2
        except Exception as e:
            log.warning("Emir durumu doğrulanamadı (%s) — dolmuş varsayılıyor: %s", order.get("id"), e)
            return order   # belirsiz → dolmuş varsay (mutabakat yakalar)
    if filled > 0:
        return order
    # Dolmadı: borsada hâlâ DURUYORSA iptal et (limit emir dinlenip sonra dolup ters-hayalet
    # yaratmasın). Zaten terminal ise iptal gereksiz.
    if order.get("id") and status not in _TERMINAL:
        try:
            ex.cancel_order(order["id"], csym)
        except Exception as e:
            log.warning("Dolmayan emir iptal edilemedi (%s): %s", order.get("id"), e)
    raise OrderError(f"emir dolmadı/reddedildi (status={status}, filled={filled})")


def _round_amount(ex: Any, csym: str, amount: float, price: float) -> float:
    """Emir miktarını borsanın lot-size/precision'ına yuvarla + minNotional/min-miktar doğrula.

    Binance her parite için stepSize/minNotional filtreler — bunlara uymayan emir REDDEDİLİR.
    Filtreyi geçemeyecek emir için net RuntimeError (boyutu artır). Market bilgisi yoksa ham döner.
    """
    try:
        if not getattr(ex, "markets", None):
            ex.load_markets()
        m = ex.market(csym)
    except Exception as e:
        log.warning("Market bilgisi alınamadı (%s) — ham miktar: %s", csym, e)
        return amount
    limits = m.get("limits") or {}
    min_cost = ((limits.get("cost") or {}).get("min"))
    if min_cost and amount * price < float(min_cost):
        raise RuntimeError(f"Emir minNotional altında (${amount * price:.2f} < ${float(min_cost):.2f}) — boyutu artır")
    try:
        adj = float(ex.amount_to_precision(csym, amount))
    except Exception:
        adj = amount
    min_amt = ((limits.get("amount") or {}).get("min"))
    if min_amt and adj < float(min_amt):
        raise RuntimeError(f"Emir min miktar altında ({adj} < {min_amt}) — boyutu artır")
    return adj or amount


def _place_protective_orders(ex: Any, csym: str, pos: dict[str, Any]) -> None:
    """Canlıda girişten sonra borsaya DURAN koruyucu SL (ve TP) emri koy.

    Bot çökse/internet gitse bile pozisyon borsada korunur. ccxt birleşik
    `stopLossPrice`/`takeProfitPrice` parametreleriyle (STOP_MARKET) — spot+futures.
    ID'ler pozisyonda saklanır (kapanış/güncellemede iptal için). Hata yutmaz.
    """
    ex_side = "sell" if pos["side"] == "long" else "buy"
    amount = pos["amount"]
    base = {"reduceOnly": True} if pos["market"] == "futures" else {}
    if pos.get("sl_price"):
        o = ex.create_order(csym, "market", ex_side, amount, None,
                            {**base, "stopLossPrice": pos["sl_price"]})
        pos["exchange_sl_id"] = o.get("id") if isinstance(o, dict) else None
    if pos.get("tp_price"):
        o = ex.create_order(csym, "market", ex_side, amount, None,
                            {**base, "takeProfitPrice": pos["tp_price"]})
        pos["exchange_tp_id"] = o.get("id") if isinstance(o, dict) else None


def _cancel_protective_orders(ex: Any, csym: str, pos: dict[str, Any]) -> None:
    """Pozisyonun duran koruyucu emirlerini iptal et (zaten dolmuşsa hata yutulur)."""
    for key in ("exchange_sl_id", "exchange_tp_id"):
        oid = pos.get(key)
        if not oid:
            continue
        try:
            ex.cancel_order(oid, csym)
        except Exception as e:
            log.warning("Koruyucu emir iptal edilemedi (%s %s): %s", key, oid, e)
        pos.pop(key, None)


def place_trade(symbol: str, side: str, usdt: float | None = None,
                source: str = "manual", reason: str = "",
                news_source: str = "", impact: int | None = None,
                atr_pct: float | None = None, sl_mult: float = 1.0,
                time_stop_min: int | None = None,
                rel_volume: float | None = None) -> dict[str, Any]:
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
        # Orderbook payı tavanı: kitabın görünür derinliğinin en fazla %X'i ol
        # (büyük emir piyasayı kaydırır + çıkışta tuzağa düşürür → boyutu kıs).
        if S.max_book_frac > 0 and est.get("avail"):
            cap = est["avail"] * S.max_book_frac
            if usdt > cap:
                log.info("Boyut orderbook payına kısıldı: $%.0f → $%.0f (kitap $%.0f, pay %%%.0f)",
                         usdt, cap, est["avail"], S.max_book_frac * 100)
                usdt = round(cap, 2)

    price = (est["avg"] if est and est.get("avg") else None) or get_price(symbol)
    if not price:
        raise RuntimeError(f"{symbol} fiyatı alınamadı")
    amount = round(usdt / price, 6)
    mode = "paper" if S.paper_trading else "live"

    if not S.paper_trading:
        ex = _get_exchange()
        csym = _ccxt_symbol(symbol)
        ex_side = "buy" if is_long else "sell"
        amount = _round_amount(ex, csym, amount, price)   # lot-size/precision/minNotional
        if S.market == "futures" and S.leverage > 1:
            try:
                ex.set_leverage(S.leverage, csym)
            except Exception as e:
                log.warning("Kaldıraç ayarlanamadı (%s): %s", csym, e)
        try:
            if S.order_type == "limit":
                try:
                    price = float(ex.price_to_precision(csym, price))
                except Exception:
                    pass
                order = _create_order_idempotent(ex, csym, "limit", ex_side, amount, price=price)
            else:
                order = _create_order_idempotent(ex, csym, "market", ex_side, amount)
            order = _verify_fill(ex, order, csym)   # gerçekten doldu mu (ters-hayalet önleme)
        except OrderError:
            raise
        except Exception as e:
            raise OrderError(f"emir gönderilemedi: {e}") from e
        if order.get("average"):
            price = float(order["average"])
        if order.get("filled"):
            amount = float(order["filled"]) or amount

    # SL/TP yüzdeleri: sabit (varsayılan) veya volatilite-bazlı (ATR)
    sl_pct = _effective_stop_pct(atr_pct)
    tp_pct = S.take_profit_pct
    if S.use_atr_exits and atr_pct and atr_pct > 0:
        tp_pct = max(1.0, min(30.0, S.atr_tp_mult * atr_pct))
    # Giriş beyni çıkış önerisi: SL sıkılığı (tight/normal/wide → sl_mult)
    if sl_mult != 1.0 and sl_pct > 0:
        sl_pct = max(0.5, min(15.0, sl_pct * sl_mult))
    # Tasfiye-farkında SL (futures): SL tasfiye fiyatının ötesindeyse önce tasfiye olunur,
    # SL hiç çalışmaz. Kaldıraç-ima tasfiye mesafesinin (~100/kaldıraç%) güvenli içine kıstır.
    if S.market == "futures" and S.leverage > 1 and sl_pct > 0:
        safe = 0.8 * (100.0 / S.leverage)
        if sl_pct > safe:
            log.warning("SL %%%.1f tasfiyeye (~%%%.1f, %dx) çok yakın — %%%.1f'e kıstırıldı (güvenlik)",
                        sl_pct, 100.0 / S.leverage, S.leverage, safe)
            sl_pct = round(safe, 2)

    # SL/TP fiyatları
    sl_price = tp_price = None
    if S.use_sl_tp:
        if is_long:
            if sl_pct > 0:
                sl_price = round(price * (1 - sl_pct / 100), 8)
            if tp_pct > 0:
                tp_price = round(price * (1 + tp_pct / 100), 8)
        else:
            if sl_pct > 0:
                sl_price = round(price * (1 + sl_pct / 100), 8)
            if tp_pct > 0:
                tp_price = round(price * (1 - tp_pct / 100), 8)

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
        "peak": price, "trough": price,   # MFE/MAE izleme (segment SL/TP öğrenme)
        "opened_at": _now(),
        "source": source,
        "news_source": news_source,
        "impact": impact,
        "rel_volume": rel_volume,   # öğrenme: hacim (RVOL) dilimine göre beklenti
        "reason": reason,
        # ATR%: SL/TP veya trailing ATR-uyarlamalıysa sakla (çıkış motoru okur)
        "atr_pct": round(atr_pct, 3) if ((S.use_atr_exits or S.use_atr_trailing) and atr_pct) else None,
        "time_stop_min": int(time_stop_min) if time_stop_min else None,
    }
    with _lock:
        _positions.append(pos)
        _last_trade[symbol] = time.monotonic()
        _save_state()
    log.info("%s AÇ | %s %s | %.2f USDT @ %.6f | SL=%s TP=%s | %s",
             mode.upper(), pos["side"], symbol, usdt, price, sl_price, tp_price, source)
    # Canlıda borsaya DURAN koruyucu stop koy (bot çökse de korunsun). Hata girişi bozmaz
    # ama LOUD uyarı + flag bırakır (korumasız canlı pozisyon tehlikeli).
    if mode == "live" and S.exchange_native_stops and (sl_price or tp_price):
        try:
            _place_protective_orders(_get_exchange(), _ccxt_symbol(symbol), pos)
            with _lock:
                _save_state()
        except Exception as e:
            log.error("⚠️ KORUYUCU STOP KONULAMADI (%s) — pozisyon yalnız bot-lokal korumalı: %s",
                      symbol, e)
            pos["protect_error"] = str(e)
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
    # Canlıda borsadaki duran koruyucu emri yeni SL/TP'ye göre yenile (eskiyi iptal, yenisini koy)
    if pos.get("mode") == "live" and S.exchange_native_stops:
        try:
            ex = _get_exchange()
            csym = _ccxt_symbol(pos["symbol"])
            _cancel_protective_orders(ex, csym, pos)
            _place_protective_orders(ex, csym, pos)
            with _lock:
                _save_state()
        except Exception as e:
            log.error("Koruyucu emir güncellenemedi (%s): %s", pos["symbol"], e)
            pos["protect_error"] = str(e)
    return dict(pos)


def close_position(pid: str, reason: str = "manuel", exchange_close: bool = True) -> dict[str, Any]:
    """Pozisyonu kapat. `exchange_close=False` → borsaya kapanış emri GÖNDERME (yalnız yerel
    defter; borsa zaten düz olduğunda mutabakat-iyileştirmesi için — duran emirler yine iptal)."""
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
            _cancel_protective_orders(ex, csym, pos)   # duran SL/TP'yi iptal et (çift kapanış önle)
            if exchange_close:
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
    # MFE/MAE (segment SL/TP öğrenme): haber yönünde en iyi/en kötü % hareket
    entry = pos.get("entry_price") or 0.0
    if entry > 0:
        peak = pos.get("peak") or entry
        trough = pos.get("trough") or entry
        if pos["side"] == "long":
            pos["mfe_pct"] = round((peak - entry) / entry * 100, 3)
            pos["mae_pct"] = round((entry - trough) / entry * 100, 3)
        else:
            pos["mfe_pct"] = round((entry - trough) / entry * 100, 3)
            pos["mae_pct"] = round((peak - entry) / entry * 100, 3)
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


def reconcile_and_heal(autoclose: bool = False) -> dict[str, Any]:
    """Açılış/periyodik mutabakat: borsada GÖRÜNMEYEN yerel pozisyonları (hayalet) tespit eder.

    Hayalet = bot 'açık' sanıyor ama borsa düz (genelde bot kapalıyken borsa stop'u tetiklenmiş
    veya elle kapanmış). `autoclose=True` ise hayaleti yerel defterde kapatır (borsaya emir
    GÖNDERMEZ — zaten düz). Drift'i her zaman raporlar; çağıran yüksek sesle uyarmalı.
    """
    rep = reconcile_positions()
    if not rep.get("checked"):
        return {"checked": False, "reason": rep.get("reason"), "phantoms": [], "healed": []}
    phantoms = rep["orphans"]
    healed: list[dict[str, Any]] = []
    if autoclose:
        for ph in phantoms:
            try:
                healed.append(close_position(ph["id"], reason="mutabakat: borsada yok",
                                             exchange_close=False))
            except Exception as e:
                log.warning("Hayalet pozisyon kapatılamadı (%s): %s", ph["symbol"], e)
    return {"checked": True, "phantoms": phantoms, "healed": healed,
            "matched": rep.get("matched", [])}


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


def open_symbols() -> list[str]:
    """Açık pozisyonların sembolleri (ağsız — portföy korelasyonu için hızlı erişim)."""
    with _lock:
        return [p["symbol"] for p in _positions if p.get("symbol")]


def get_positions() -> tuple[list[dict[str, Any]], float]:
    with _lock:
        snap = list(_positions)
    # Fiyatları önbellekten oku (izleme döngüsü 8s'de tazeler) — panel anında dönsün
    prices = cached_prices([p["symbol"] for p in snap])
    out: list[dict[str, Any]] = []
    total = 0.0
    for p in snap:
        cur = prices.get(p["symbol"])
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


def _parse_tp_levels(spec: str) -> list[tuple[float, float]]:
    """Çok-kademeli scale-out spec'ini ayrıştır: "3:0.33,6:0.33,10:0.34" → [(3,0.33),...].

    Saf fonksiyon. Geçersiz parçaları atlar; pct'ye göre artan sıralar (en düşük eşik
    önce tetiklenir). Boş/bozuk → boş liste (tek-kademe partial_tp_pct'e düşülür).
    """
    out: list[tuple[float, float]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        a, _, b = part.partition(":")
        try:
            pct, frac = float(a), float(b)
        except ValueError:
            continue
        if pct > 0 and 0 < frac <= 1:
            out.append((pct, frac))
    return sorted(out, key=lambda x: x[0])


def _tp_levels() -> list[tuple[float, float]]:
    """Etkin scale-out kademeleri: çok-kademe spec doluysa o, değilse tek-kademe (geriye uyum)."""
    levels = _parse_tp_levels(S.partial_tp_levels)
    if levels:
        return levels
    if S.partial_tp_pct > 0 and S.partial_tp_frac > 0:
        return [(S.partial_tp_pct, S.partial_tp_frac)]
    return []


def _effective_trailing_pct(p: dict[str, Any]) -> float:
    """Bu pozisyonun etkin trailing yüzdesi: ATR-uyarlamalı açıksa atr_trailing_mult×ATR%
    (clamp [0.3,10]), yoksa pozisyonun sabit trailing_pct'i.

    Oynak coinde (ATR yüksek) trailing geniş → trend tutulur; sakin coinde dar → erken
    kilitlenir. ATR yoksa sabit yüzdeye düşülür (eksik veri ceza değil).
    """
    if S.use_atr_trailing:
        atr = p.get("atr_pct")
        if atr and atr > 0:
            return max(0.3, min(10.0, S.atr_trailing_mult * atr))
    return p.get("trailing_pct", 0) or 0


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
    # Tüm fiyatları TEK çağrıda çek (seri N HTTP yerine) — izleme döngüsü hızlı kalsın
    prices = get_prices([p["symbol"] for p in snap])
    now = datetime.now(timezone.utc)
    for p in snap:
        cur = prices.get(p["symbol"])
        if cur is None:
            continue
        is_long = p["side"] == "long"
        entry = p["entry_price"]
        gain = ((cur - entry) / entry * 100) * (1 if is_long else -1)  # haber yönünde % kazanç
        changed = False

        # 0) MFE/MAE izleme (öğrenme: segment SL/TP). Fiyatın EN YÜKSEK (peak) ve
        #    EN DÜŞÜK (trough) uçlarını sakla → kapanışta mfe%/mae% hesaplanır.
        peak = p.get("peak") or entry
        trough = p.get("trough") or entry
        if cur > peak:
            p["peak"] = cur
        elif cur < trough:
            p["trough"] = cur

        # 1) Trailing stop: kâr yönünde ilerledikçe stop'u çek. Yüzde sabit veya
        #    ATR-uyarlamalı (oynak coinde geniş, sakinde dar) — _effective_trailing_pct.
        tr = _effective_trailing_pct(p)
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

        # 3) Kısmi TP (scale-out): çok-kademeli — her eşik ayrı tetiklenir (bir kez).
        #    partial_levels_done = tetiklenmiş eşiklerin listesi (restart'a dayanıklı).
        levels = _tp_levels()
        if levels:
            done = p.get("partial_levels_done") or []
            for pct, frac in levels:
                if gain >= pct and pct not in done:
                    rec = _partial_close(p, frac, f"partial-tp-{pct:g}%", cur)
                    done = [*done, pct]
                    p["partial_levels_done"] = done
                    if rec:
                        closed.append(rec)
                    if p["amount"] <= 0:   # tüm pozisyon scale-out ile kapandı
                        break

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
        eff_ts = p.get("time_stop_min") or S.time_stop_min   # pozisyon-bazlı (beyin) > global
        if hit is None and eff_ts and eff_ts > 0:
            opened = _parse_dt(p.get("opened_at"))
            if opened and (now - opened).total_seconds() >= eff_ts * 60:
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


def _open_side_count(side: str) -> int:
    """Şu an aynı yönde (long/short) açık pozisyon sayısı (korelasyon kapısı)."""
    with _lock:
        return sum(1 for p in _positions if p["side"] == side)


def _size_multiplier(impact: int) -> float:
    """Conviction çarpanı: yüksek güç = büyük pozisyon. 8'de 1.0x, [0.5x, 1.5x] arası."""
    return max(0.5, min(1.5, 1.0 + (impact - 8) * 0.25))


def _liquidity_factor(volume_usd: float | None) -> float:
    """Likidite-katmanlı boyut çarpanı: ince coinde küçül (giremediğin yerden çıkamazsın).

    Profesyonel kural — pozisyonu coinin 24s hacmine göre ölçekle. Derin coinde
    tam boyut, inceldikçe kısıl. Hacim bilinmiyorsa ihtiyatlı (0.5x).
      ≥$50M → 1.0x | $10–50M → 0.8x | $5–10M → 0.6x | $1–5M → 0.4x | <$1M → 0.25x
    """
    if volume_usd is None or volume_usd <= 0:
        return 0.5
    if volume_usd >= 50_000_000:
        return 1.0
    if volume_usd >= 10_000_000:
        return 0.8
    if volume_usd >= 5_000_000:
        return 0.6
    if volume_usd >= 1_000_000:
        return 0.4
    return 0.25


def _required_rvol(impact: int) -> float:
    """Bu haberin geçmesi için gereken RVOL eşiği — impact'e göre ölçekli (opt-in).

    Gerçek büyük haber piyasayı oransal hareketlendirir: impact 9-10 haberde 1.0x RVOL
    (normal hacim) şüphelidir (fake/fiyatlanmış). Taban `min_rel_volume`; her impact
    puanı 8'in üstünde eşiği +%15 yükseltir, altında gevşetir. rvol_scale_by_impact
    kapalıysa sabit min_rel_volume. Korkuluk: eşik en fazla taban×2.
    """
    base = S.min_rel_volume
    if not S.rvol_scale_by_impact or base <= 0:
        return base
    scaled = base * (1.0 + 0.15 * (impact - 8))
    return round(max(base * 0.5, min(base * 2.0, scaled)), 2)


# Kelly çarpanı [alt, üst] kıstırması — tam-Kelly bile asla 1.5x'i aşmaz (risk tavanı)
_KELLY_MIN_MULT = 0.25
_KELLY_MAX_MULT = 1.5


def _kelly_fraction(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """Kapanan GERÇEK işlemlerden Kelly fraksiyonu f* = W − (1−W)/R (saf fonksiyon).

    W = kazanma oranı, R = payoff (ort.kazanç / |ort.kayıp|). f* pozitif edge'i,
    sermayenin ne kadarının bahse değer olduğunu söyler. Burada f*'ı doğrudan
    sermaye-oranı olarak DEĞİL, taban boyutun çarpanı olarak kullanırız (trade_usdt
    zaten risk birimi). Döner: {ready, f_star, win_rate, payoff, n}.

    `ready` yalnız yeterli örnek VE anlamlı edge varsa True — gürültüden Kelly
    çıkarmak felakettir (tek şanslı seri → aşırı-bahis). Wilson alt sınırı 0.5'i
    aşmıyorsa (kazanma oranı güvenilir değil) ready=False.
    """
    scored = [c for c in closed if c.get("pnl") is not None]
    n = len(scored)
    if n < S.kelly_min_trades:
        return {"ready": False, "f_star": 0.0, "win_rate": None, "payoff": None, "n": n}
    wins = [c["pnl"] for c in scored if c["pnl"] > 0]
    losses = [c["pnl"] for c in scored if c["pnl"] < 0]
    if not wins or not losses:
        return {"ready": False, "f_star": 0.0, "win_rate": None, "payoff": None, "n": n}
    w = len(wins) / n
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    if avg_loss <= 0:
        return {"ready": False, "f_star": 0.0, "win_rate": None, "payoff": None, "n": n}
    payoff = avg_win / avg_loss
    f_star = w - (1 - w) / payoff
    # Gürültü korkuluğu: kazanma oranının Wilson alt sınırı (%95) anlamlı değilse Kelly'e güvenme
    ready = _wilson_lo(len(wins), n) > 0.0 and f_star > 0
    return {"ready": ready, "f_star": round(f_star, 4),
            "win_rate": round(w, 3), "payoff": round(payoff, 2), "n": n}


def _kelly_multiplier() -> float:
    """Fraksiyonel-Kelly boyut çarpanı (kapanan işlemlerden). Korkuluklu: [0.25, 1.5].

    Edge belirsizse (yetersiz/gürültülü örnek) NÖTR (1.0x — boyutu bozmaz). Negatif
    edge'de minimuma kıstırır (boyutu kıs — ama girişi engellemez, o veto işi değil).
    """
    with _lock:
        closed = list(_closed)
    k = _kelly_fraction(closed)
    if not k["ready"]:
        return 1.0
    # f* (pozitif) × kullanıcının fraksiyonu (çeyrek-Kelly) → taban çarpanı.
    # 1.0 nötr nokta etrafında: tam edge (f*≈1) tavanı, sıfır edge taban civarı.
    mult = 1.0 + k["f_star"] * max(0.0, min(1.0, S.kelly_fraction))
    return round(max(_KELLY_MIN_MULT, min(_KELLY_MAX_MULT, mult)), 3)


def _effective_stop_pct(atr_pct: float | None) -> float:
    """Bu işlemin gerçek SL yüzdesi: ATR çıkışı açıksa atr_sl_mult×ATR (clamp), yoksa sabit.

    place_trade ile aynı SL mantığı (tek doğruluk kaynağı) — risk-eşitleme aynı
    pencereden baksın. Beyin sl_tightness çarpanı burada YOK (o, beyin yargısından
    sonra place_trade'de uygulanır; risk-eşitleme mekanik tabanı hedefler).
    """
    if S.use_atr_exits and atr_pct and atr_pct > 0:
        return max(0.5, min(15.0, S.atr_sl_mult * atr_pct))
    return S.stop_loss_pct


def _risk_parity_factor(usdt: float, stop_pct: float | None) -> float:
    """Vol-hedef: SL mesafesi geniş işlemde boyutu kıs ki SL'deki USDT-riski sabit kalsın.

    Risk-at-SL = usdt × (stop_pct/100). Hedef risk sabitse (target_risk_usdt veya
    trade_usdt'nin baz-stop riski), bu işlemin boyutunu hedef/gerçek-risk oranıyla
    ölçekle. Geniş ATR-SL'li işlem küçülür, dar SL'li büyür → her işlem aynı USDT'yi
    riske atar. Korkuluk: çarpan [0.25, 1.5] (tek işlemde aşırı şişme/sönme önlenir).
    """
    if not stop_pct or stop_pct <= 0 or usdt <= 0:
        return 1.0
    actual_risk = usdt * (stop_pct / 100.0)
    if actual_risk <= 0:
        return 1.0
    target = S.target_risk_usdt if S.target_risk_usdt > 0 else S.trade_usdt * (S.stop_loss_pct / 100.0)
    if target <= 0:
        return 1.0
    return round(max(_KELLY_MIN_MULT, min(_KELLY_MAX_MULT, target / actual_risk)), 3)


def _returns(closes: list[float]) -> list[float]:
    """Kapanış serisinden basit getiri serisi (ardışık % değişim). Saf."""
    out: list[float] = []
    for a, b in zip(closes, closes[1:]):
        if a > 0:
            out.append((b - a) / a)
    return out


def _corr(xs: list[float], ys: list[float]) -> float | None:
    """İki getiri serisinin Pearson korelasyonu (eşit boya kırpılır). <3 nokta/sıfır var → None."""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    return _pearson(xs[-n:], ys[-n:])


def _portfolio_heat(new_sym: str, new_side: str,
                    series: dict[str, list[float]]) -> dict[str, Any]:
    """Yeni pozisyonun açık pozisyonlarla korelasyon-yükü ("tek bahis" riski). Saf.

    `series`: {symbol: getiri_serisi} (yeni + açık coinler). Açık pozisyonlardan AYNI
    YÖNDE olup yeni adayla |korelasyon| ≥ corr_threshold olanlar "aynı bahis" sayılır.
    `heat` = 1 (yeni) + Σ korelasyon-ağırlık (ters yön korelasyonu yükü düşürür — hedge).
    `factor` = boyut çarpanı: heat tavanı (max_portfolio_heat) aşılırsa kıs ([0.25,1.0]).
    Veri yoksa nötr (heat=1, factor=1.0) — eksik veri ceza değil.
    """
    with _lock:
        opens = [(p["symbol"], p["side"]) for p in _positions if p["symbol"] != new_sym]
    new_ret = series.get(new_sym, [])
    correlated: list[dict[str, Any]] = []
    heat = 1.0
    for sym, side in opens:
        c = _corr(new_ret, series.get(sym, []))
        if c is None:
            continue
        # Aynı yön + pozitif korelasyon → yük artar; ters yön + pozitif korelasyon → hedge (azalır)
        signed = c if side == new_side else -c
        if abs(c) >= S.corr_threshold:
            correlated.append({"symbol": sym, "corr": round(c, 2), "side": side})
        heat += max(-0.5, min(1.0, signed))   # tek pozisyonun katkısı [-0.5, 1.0]
    heat = max(0.0, round(heat, 2))
    if heat <= S.max_portfolio_heat:
        factor = 1.0
    else:
        # Tavanı aşan ısı oranında kıs (en fazla 0.25x)
        factor = max(0.25, round(S.max_portfolio_heat / heat, 3))
    return {"heat": heat, "factor": factor, "correlated": correlated, "n_open": len(opens)}


def auto_decision(item: Any, *, feed_stale: bool = False,
                  news_age_sec: float | None = None,
                  latency_slow: bool = False,
                  price_series: dict[str, list[float]] | None = None) -> dict[str, Any]:
    """Bir haberin oto-işlem açıp açmayacağına dair YAN ETKİSİZ karar.

    Global `auto_trade` anahtarını dikkate almaz (kalibrasyon/önizleme için her
    sinyali değerlendirir). Dönen: {would_trade, reason, side, usdt, news_source}.

    `price_series`: {symbol: getiri_serisi} — portföy korelasyon-yükü için (yeni aday +
    açık coinler). Çağıran (ağlı) doldurur; yoksa portföy-risk nötr (saf/ağsız kalır).

    `feed_stale`: haber akışı (WS) kopuk mu — güvenlik durdurması için çağıran geçirir.
    `news_age_sec`: haberin yaşı (saniye) — latency kapısı için çağıran hesaplar.
    `latency_slow`: boru hattı gecikme SLA'sı aşıldı mı — çağıran (news_bot) geçirir.
    """
    no = lambda r: {"would_trade": False, "reason": r, "side": None, "usdt": None, "news_source": ""}  # noqa: E731
    # Operasyonel devre kesici: anomali sonrası yeni oto-işlem durdurulmuş
    if _halt["active"]:
        return no(f"operasyonel durdurma: {_halt['reason']}")
    # Güvenlik kapısı: akış kopukken kör girme (gerçek-zamanlı teyit güvenilmez)
    if S.halt_trade_on_stale and feed_stale:
        return no("haber akışı kopuk — güvenlik durdurması")
    # Güvenlik kapısı: boru hattı yavaş (SLA aşıldı) — hareketin gerisinde gireriz
    if S.halt_trade_on_latency and latency_slow:
        return no("boru hattı gecikme SLA aşıldı — güvenlik durdurması")
    # Güvenlik kapısı: haber çok eskiyse hareket büyük olasılıkla bitmiştir
    if S.max_news_age_sec > 0 and news_age_sec is not None and news_age_sec > S.max_news_age_sec:
        return no(f"haber çok eski ({news_age_sec:.0f}s > {S.max_news_age_sec}s)")
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
    # Korelasyon kapısı: aynı yönde çok pozisyon = tek bahis (BTC-korele küme riski)
    if S.max_same_direction > 0 and _open_side_count(side) >= S.max_same_direction:
        return no(f"aynı yönde pozisyon limiti ({S.max_same_direction})")
    if S.skip_already_priced_pct > 0:
        m = getattr(item, "price_24h_pct", None)
        if m is not None and ((side == "long" and m >= S.skip_already_priced_pct)
                              or (side == "short" and m <= -S.skip_already_priced_pct)):
            return no(f"zaten fiyatlanmış (24s %{m:+.1f})")
    # RVOL kapısı: hacim hareketi onaylamıyorsa haber muhtemelen fake → girme.
    # (Veri yoksa engelleme — eksik veri ≠ düşük hacim.) Eşik impact-ölçekli olabilir:
    # yüksek-güç haber daha çok hacim bekler (büyük haber piyasayı oransal hareketlendirir).
    if S.min_rel_volume > 0:
        rv = getattr(item, "rel_volume", None)
        req = _required_rvol(int(item.impact))
        if rv is not None and rv < req:
            return no(f"hacim zayıf (RVOL {rv:.1f}x < {req:.1f}x)")
    # Öğrenilen-veto: bu haber geçmişte ANLAMLI zarar eden bir koşullu segmente düşüyorsa girme
    if S.use_learned_vetoes:
        hit = _learned_veto_hit(item)
        if hit is not None:
            return no(f"öğrenilen-veto [{hit['kind']}: {'×'.join(map(str, hit['key']))}] "
                      f"ort. {hit['avg_pnl']} ({hit['n']} örnek)")
    news_source = getattr(item, "source", "") or ""
    if S.suppress_losing_sources and news_source:
        st = source_stats(news_source)
        if st["count"] >= S.min_source_samples and st["avg_pnl"] < 0:
            return no(f"kaynak negatif beklenti ({news_source} avg={st['avg_pnl']})")
    # Funding kapısı (futures): yön funding'e ters & taşıma maliyeti yüksekse girme.
    # Yalnız uygun adaylarda 1 ağ çağrısı; spot'ta ya da kapalıyken hiç çağrılmaz.
    if S.market == "futures" and S.max_funding_rate_pct > 0:
        cost = _funding_cost_pct(symbol, side)
        if cost is not None and cost > S.max_funding_rate_pct:
            return no(f"funding maliyeti yüksek (%{cost:+.3f} > %{S.max_funding_rate_pct})")
    # Boyut: conviction (güce göre) × Kelly (kazanma matematiği) × likidite katmanı
    # (hacme göre) × kayıp serisi freni × risk-eşitleme (SL mesafesine göre).
    # Taban: yüzde-bazlı risk açıksa sermayenin %'sini SL'de riske at (sabit lot yerine),
    # değilse sabit trade_usdt. Yüzde-bazlı taban zaten SL-normalize olduğundan risk-eşitleme
    # (risk_parity) onunla birlikte uygulanmaz (çift-sayım önleme).
    eff_stop = _effective_stop_pct(getattr(item, "atr_pct", None))
    if S.risk_per_trade_pct > 0:
        usdt = _risk_per_trade_base(eff_stop)
    else:
        usdt = S.trade_usdt
    if S.size_by_impact:
        usdt *= _size_multiplier(int(item.impact))
    if S.size_by_kelly:
        usdt *= _kelly_multiplier()
    if S.size_by_volume:
        usdt *= _liquidity_factor(getattr(item, "volume_usd", None))
    if S.reduce_after_losses > 0 and _losing_streak() >= S.reduce_after_losses:
        usdt *= 0.5
    # Risk-eşitleme (vol-hedef): bu işlemin SL mesafesine göre boyutu eşitle. SL%
    # canlı yolda ATR çıkışı açıksa ATR'den, değilse sabit stop_loss_pct'ten gelir.
    if S.risk_parity and S.risk_per_trade_pct <= 0:
        usdt *= _risk_parity_factor(usdt, eff_stop)
    # Portföy-seviye: yeni pozisyon mevcut açık pozisyonlarla koreleyse "tek bahis" → kıs.
    # price_series çağırandan gelir (ağlı); yoksa nötr.
    heat_info: dict[str, Any] | None = None
    if S.portfolio_risk and price_series:
        heat_info = _portfolio_heat(symbol, side, price_series)
        usdt *= heat_info["factor"]
    out = {"would_trade": True, "reason": "tier1-refleks" if tier1 else "uygun",
           "side": side, "usdt": round(usdt, 2), "news_source": news_source}
    if heat_info is not None:
        out["portfolio_heat"] = heat_info
    return out


# ── Shadow-mode / A-B: aday ayarı canlı sinyallerle SANAL test (gerçek emir yok) ──
_shadow_overrides: dict[str, Any] = {}   # {ayar_adı: aday_değer} — gölge senaryosu
_SHADOW_KEYS = ("auto_min_impact", "tier1_skip_confirm_impact", "auto_require_confirm",
                "min_rel_volume", "rvol_scale_by_impact", "skip_already_priced_pct",
                "size_by_impact", "size_by_kelly", "kelly_fraction",
                "risk_parity", "max_same_direction", "max_funding_rate_pct",
                "suppress_losing_sources", "use_learned_vetoes")


def set_shadow_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """Gölge (aday) ayar override'larını ayarla. Yalnız güvenli karar-eşiği alanları;
    para-büyüklüğü tabanı (trade_usdt) / risk tavanları override edilemez (gölge sadece
    KARAR farkını test eder, sanal). Boş dict → gölge kapalı. Döner: etkin override'lar."""
    global _shadow_overrides
    clean = {k: v for k, v in (overrides or {}).items() if k in _SHADOW_KEYS and v is not None}
    _shadow_overrides = clean
    return dict(_shadow_overrides)


def get_shadow_overrides() -> dict[str, Any]:
    return dict(_shadow_overrides)


def shadow_decision(item: Any, *, feed_stale: bool = False,
                    news_age_sec: float | None = None,
                    latency_slow: bool = False,
                    price_series: dict[str, list[float]] | None = None
                    ) -> dict[str, Any] | None:
    """Aday ayarla (gölge) auto_decision'ı SANAL çalıştır — gerçek emir YOK, S kalıcı değil.

    Canlı karar (mevcut S) ile aday karar yan yana üretilir; FARK varsa kaydedilebilir.
    S'in gölge alanları geçici set edilir, auto_decision çağrılır, AYNEN geri yüklenir
    (lock altında — eşzamanlı gerçek karar bozulmasın). Gölge yoksa None.

    Döner: {live, shadow, diverged} — live/shadow auto_decision çıktıları, diverged: ikisi
    farklı would_trade veya farklı boyut mu (aday ayar başka karar verirdi mi).
    """
    if not _shadow_overrides:
        return None
    live = auto_decision(item, feed_stale=feed_stale, news_age_sec=news_age_sec,
                         latency_slow=latency_slow, price_series=price_series)
    # S'i geçici override et — auto_decision iç kilitleri (_open_side_count vb.) aldığından
    # BURADA _lock TUTMA (yeniden-giriş = deadlock). Gölge, process_items'ten sıralı çağrılır.
    saved = {k: getattr(S, k) for k in _shadow_overrides}
    try:
        for k, v in _shadow_overrides.items():
            setattr(S, k, v)
        shadow = auto_decision(item, feed_stale=feed_stale, news_age_sec=news_age_sec,
                               latency_slow=latency_slow, price_series=price_series)
    finally:
        for k, v in saved.items():
            setattr(S, k, v)
    diverged = (live["would_trade"] != shadow["would_trade"]
                or (live["would_trade"] and live["usdt"] != shadow["usdt"]))
    return {"live": live, "shadow": shadow, "diverged": diverged}


# Terfi önerisi için minimum kanıt: en az bu kadar SONUÇLU divergence + net edge eşiği
_SHADOW_PROMOTE_MIN = 10
_SHADOW_PROMOTE_EDGE_PCT = 0.5   # aday, canlıdan ort. bu kadar % net daha iyi olmalı


def shadow_promotion_advice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Gölge kararların SANAL sonuçlarından aday-ayar terfi ÖNERİSİ (saf, OTO-UYGULAMAZ).

    `rows`: her biri en az {diverged, live_trade, shadow_trade, outcome_pct} taşıyan gölge
    kayıtları. `outcome_pct`: o sinyalin sinyal-sonrası gerçek net %% (çağıran backtest'le
    doldurur). Yalnız DIVERGENCE'larda (aday≠canlı karar) sonucu sayarız — fark orada.
      - aday GİRER & canlı girmez: aday o sinyalin sonucunu KAZANIR/KAYBEDER (live 0 alır)
      - canlı GİRER & aday girmez: tersi
    Aday ort. net edge ≥ eşik VE yeterli örnek → 'terfi öner' (insan onayı şart, KONTROL kullanıcıda).
    Döner: {ready, n, shadow_avg, live_avg, edge_pct, recommend}.
    """
    scored = [r for r in rows if r.get("diverged") and r.get("outcome_pct") is not None]
    n = len(scored)
    if n < _SHADOW_PROMOTE_MIN:
        return {"ready": False, "n": n, "min": _SHADOW_PROMOTE_MIN,
                "shadow_avg": None, "live_avg": None, "edge_pct": None, "recommend": False}
    # Her divergence'ta her ayarın o sinyalden KAZANDIĞI net %: girdiyse outcome, girmediyse 0
    shadow_pnls = [float(r["outcome_pct"]) if r.get("shadow_trade") else 0.0 for r in scored]
    live_pnls = [float(r["outcome_pct"]) if r.get("live_trade") else 0.0 for r in scored]
    shadow_avg = sum(shadow_pnls) / n
    live_avg = sum(live_pnls) / n
    edge = shadow_avg - live_avg
    return {"ready": True, "n": n, "shadow_avg": round(shadow_avg, 3),
            "live_avg": round(live_avg, 3), "edge_pct": round(edge, 3),
            "recommend": edge >= _SHADOW_PROMOTE_EDGE_PCT}


def _consult_brain(brain: Any, item: Any, decision: dict[str, Any]) -> dict[str, Any] | None:
    """Giriş beynini güvenli çağır. Hata olursa None (mekanik karar geçerli kalır)."""
    try:
        return brain(item, decision)
    except Exception as e:
        log.warning("Giriş beyni hatası, mekanik karar geçerli: %s", e)
        return None


def maybe_auto_trade(item: Any, *, feed_stale: bool = False,
                     news_age_sec: float | None = None,
                     latency_slow: bool = False,
                     brain: Any = None,
                     price_series: dict[str, list[float]] | None = None) -> dict[str, Any] | None:
    if not S.auto_trade:
        return None
    d = auto_decision(item, feed_stale=feed_stale, news_age_sec=news_age_sec,
                      latency_slow=latency_slow, price_series=price_series)
    if not d["would_trade"]:
        return None
    usdt = d["usdt"]
    verdict: dict[str, Any] | None = None
    sl_mult = 1.0
    hold_min: int | None = None
    # Giriş beyni: mekanik kapıları geçen Tier-2 (refleks olmayan) adayda son yargı.
    # enter=False → veto; conviction → boyut; sl_tightness/hold_minutes → çıkış. Refleks atlanır.
    if brain is not None and S.use_entry_brain and d["reason"] != "tier1-refleks":
        verdict = _consult_brain(brain, item, d)
        if verdict is not None:
            if not verdict.get("enter", True):
                log.info("Giriş beyni VETO | %s | %s", item.symbol, verdict.get("reason", ""))
                return None
            conv = verdict.get("conviction")
            if conv is not None:
                # Recalibration: ham conviction'ı geçmiş isabetle düzelt (aşırı-güveni bastır).
                # Self-improve dilimi ve boyut bu düzeltilmiş değeri kullanır.
                rec = recalibrate_conviction(float(conv))
                if rec["adjusted"]:
                    conv = rec["value"]
                    verdict["conviction_raw"] = rec["raw"]
                    verdict["conviction"] = conv   # şeffaflık: pozisyonda düzeltilmiş saklanır
                # Kendini-iyileştirme: bu conviction dilimi geçmişte negatifse oto-veto; zayıfsa boyutu kıs
                if S.brain_self_improve:
                    band = _brain_band_stats(float(conv))
                    if band and band["n"] >= _BRAIN_SELF_IMPROVE_MIN and band["avg_pnl"] is not None:
                        if band["avg_pnl"] < 0:
                            log.info("Kendini-iyileştirme VETO | %s | konv-dilimi %s negatif (ort %s)",
                                     item.symbol, band["band"], band["avg_pnl"])
                            return None
                        if band["win_rate"] is not None and band["win_rate"] < 0.5:
                            usdt = round(usdt * 0.75, 2)   # zayıf dilim → boyutu kıs
                usdt = round(usdt * max(0.5, min(1.5, float(conv) + 0.5)), 2)  # 0.5→1.0x,1.0→1.5x
            sl_mult = {"tight": 0.6, "wide": 1.5}.get(verdict.get("sl_tightness", "normal"), 1.0)
            hm = verdict.get("hold_minutes")
            hold_min = int(hm) if hm and int(hm) > 0 else None
    try:
        pos = place_trade(item.symbol, d["side"], usdt=usdt, source="auto",
                          news_source=d["news_source"], impact=int(item.impact),
                          reason=getattr(item, "reason", ""),
                          atr_pct=getattr(item, "atr_pct", None),
                          sl_mult=sl_mult, time_stop_min=hold_min,
                          rel_volume=getattr(item, "rel_volume", None))
    except OrderError as e:
        log.warning("Oto-işlem emri başarısız (%s): %s", item.symbol, e)
        _note_order_result(False)   # üst üste hata → devre kesici
        return None
    except Exception as e:
        log.warning("Otomatik işlem açılamadı (%s): %s", item.symbol, e)
        return None   # doğrulama reddi (slippage/minNotional/risk) — anomali değil
    _note_order_result(True)
    if isinstance(pos, dict) and pos.get("protect_error"):   # canlı pozisyon KORUMASIZ → durdur
        trip_halt(f"{item.symbol} koruyucu stop konulamadı (korumasız pozisyon)")
    if verdict is not None and isinstance(pos, dict):
        pos["brain"] = verdict   # şeffaflık: konviksiyon + rubrik + gerekçe pozisyonda saklanır
    return pos


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


def _drawdown_state(closed: list[dict[str, Any]], account_base: float) -> dict[str, Any]:
    """Anlık tepe-dip drawdown durumu (kill-switch için). Saf.

    equity_t = account_base + kümülatif realized; peak = en yüksek equity; drawdown =
    peak'ten anlık düşüş. `drawdown_pct` = düşüş / peak × 100 (>=0). Boş/zarar yoksa 0.
    """
    base = max(1e-9, account_base)
    cum = 0.0
    peak = base
    equity = base
    for c in closed:
        if c.get("pnl") is None:
            continue
        cum += c["pnl"]
        equity = base + cum
        peak = max(peak, equity)
    dd_usdt = max(0.0, peak - equity)
    return {"equity": round(equity, 2), "peak": round(peak, 2),
            "drawdown_usdt": round(dd_usdt, 2),
            "drawdown_pct": round(dd_usdt / peak * 100, 2)}


def _account_equity() -> float:
    """Anlık sermaye = account_equity_usdt tabanı + kümülatif realized (P&L ile birleşik)."""
    realized = sum(c["pnl"] for c in _closed if c.get("pnl") is not None)
    return max(0.0, S.account_equity_usdt + realized)


def _risk_per_trade_base(stop_pct: float | None) -> float:
    """Yüzde-bazlı taban boyut: SL tetiklenince sermayenin `risk_per_trade_pct`'i kaybedilir.

    notional = (equity × pct%) / (eff_stop%) → SL mesafesi geniş işlemde küçük, dar işlemde
    büyük pozisyon (sabit USDT-risk). eff_stop tabanı %0.5'e kıstırılır (div-by-tiny önleme);
    sonuç sağduyu tavanı 3×sermaye ile sınırlanır (kalan exposure/kaldıraç kapıları ayrıca uygular).
    """
    eff = max(0.5, stop_pct if stop_pct and stop_pct > 0 else (S.stop_loss_pct or 3.0))
    eq = _account_equity()
    base = eq * S.risk_per_trade_pct / eff   # eq×(pct/100)/(eff/100)
    return round(max(0.0, min(base, eq * 3.0)), 2)


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
    with _lock:
        kelly = _kelly_fraction(list(_closed))
        dd = _drawdown_state(_closed, S.account_equity_usdt)
        equity_now = _account_equity()
    dd_halt = bool(S.max_drawdown_pct > 0 and dd["drawdown_pct"] >= S.max_drawdown_pct)
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
        # drawdown kill-switch: sermaye tepeden çok düştüyse yeni işlem açılmaz
        "drawdown": {**dd, "max_drawdown_pct": S.max_drawdown_pct,
                     "account_equity_usdt": S.account_equity_usdt, "halted": dd_halt},
        # Yüzde-bazlı boyutlama (açıksa): anlık sermaye + işlem başı risk %'si
        "sizing": {"risk_per_trade_pct": S.risk_per_trade_pct, "equity": round(equity_now, 2),
                   "mode": "risk_pct" if S.risk_per_trade_pct > 0 else "fixed_usdt"},
        "paper_trading": S.paper_trading,
        "auto_trade": S.auto_trade,
        # Kelly boyut bağlamı (şeffaflık): edge + uygulanan çarpan
        "kelly": {**kelly, "multiplier": _kelly_multiplier() if S.size_by_kelly else 1.0,
                  "enabled": S.size_by_kelly},
        # Rejim adaptasyon durumu: eşik geçici sıkılaştırıldı mı
        "regime": get_regime_state(),
    }


def preflight() -> list[dict[str, Any]]:
    """Canlıya geçiş operasyonel ön-uçuş kontrolleri (saf — ağsız, S + env okur).

    `/readiness` track-record edge'ini sorgular; bu fonksiyon AYRI bir ekseni:
    sistem gerçek parayı riske atacak şekilde **güvenli yapılandırılmış mı**.
    Her kontrol: name/status (ok|warn|critical|info)/detail. Canlıya geçiş için
    'critical' eksikler bloke edicidir; paper modunda canlı-özel kontroller yine
    'canlıya geçince gerekecek' diye gösterilir.

    Trade güvenliği için `news_bot._preflight` bunu besleme-sağlığı (WS) + uzak
    bildirim + token kontrolleriyle birleştirir ve nihai verdikt üretir.
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    live = not S.paper_trading
    add("İşlem modu", "info",
        "CANLI — gerçek emir" if live else "PAPER — simülasyon (gerçek emir yok)")

    # Canlı API anahtarları (canlıda kritik; paper'da uyarı/bilgi)
    if has_live_keys():
        add("Borsa API anahtarları", "ok", "BINANCE_API_KEY/SECRET tanımlı")
    else:
        add("Borsa API anahtarları", "critical" if live else "info",
            "yok — canlı emir gönderilemez (.env BINANCE_API_KEY/SECRET)")

    # Stop-loss güvencesi: SL/TP veya ATR çıkışı açık olmalı
    if S.use_sl_tp and (S.stop_loss_pct > 0 or S.use_atr_exits):
        add("Zarar durdurma (SL)", "ok",
            f"SL=%{S.stop_loss_pct:g}" + (" + ATR" if S.use_atr_exits else ""))
    else:
        add("Zarar durdurma (SL)", "critical", "SL kapalı — korumasız pozisyon riski")

    # Borsa-native koruyucu stop (bot çökse de korur) — canlıda kritik
    if S.exchange_native_stops:
        add("Borsa koruyucu stop", "ok", "canlıda borsaya DURAN SL/TP konur (çökmeye dayanıklı)")
    else:
        add("Borsa koruyucu stop", "critical" if live else "warn",
            "kapalı — bot çökerse/internet giderse pozisyon korumasız")

    # Anomali devre kesici
    add("Anomali devre kesici", "ok" if S.auto_halt_on_anomaly else "warn",
        "açık" if S.auto_halt_on_anomaly else "kapalı — emir-hata serisinde durmaz")

    # Risk limitleri (sınırsız zarar/maruziyet = kritik)
    add("Günlük zarar limiti", "ok" if S.daily_loss_limit_usdt > 0 else "critical",
        f"{S.daily_loss_limit_usdt:g} USDT" if S.daily_loss_limit_usdt > 0
        else "0 = SINIRSIZ günlük zarar (kill-switch yok)")
    add("Toplam maruziyet tavanı", "ok" if S.max_total_exposure_usdt > 0 else "warn",
        f"{S.max_total_exposure_usdt:g} USDT" if S.max_total_exposure_usdt > 0 else "0 = sınırsız")
    add("Coin maruziyet tavanı", "ok" if S.max_per_coin_usdt > 0 else "warn",
        f"{S.max_per_coin_usdt:g} USDT" if S.max_per_coin_usdt > 0 else "0 = sınırsız")
    add("Drawdown kill-switch", "ok" if S.max_drawdown_pct > 0 else "warn",
        f"%{S.max_drawdown_pct:g} (sermaye tabanı {S.account_equity_usdt:g} USDT)"
        if S.max_drawdown_pct > 0 else "kapalı — tepe-dip düşüşte durdurma yok")
    add("Pozisyon boyutlama", "info",
        f"yüzde-bazlı: işlem başı sermayenin %{S.risk_per_trade_pct:g} riski (sermaye {_account_equity():g} USDT)"
        if S.risk_per_trade_pct > 0 else f"sabit: {S.trade_usdt:g} USDT/işlem")

    # Kaldıraç aklı (futures)
    if S.market == "futures" and S.leverage > 10:
        add("Kaldıraç", "warn", f"{S.leverage}x — yüksek; tasfiye mesafesi dar")
    elif S.market == "futures":
        add("Kaldıraç", "ok", f"{S.leverage}x")

    # Açık devre kesici → şu an işlem açılmaz
    halt = get_halt()
    if halt["active"]:
        add("Devre kesici durumu", "critical", f"AKTİF — oto-işlem durdurulmuş: {halt['reason']}")
    else:
        add("Devre kesici durumu", "ok", "temiz")

    return checks


def complexity_audit(closed_n: int) -> dict[str, Any]:
    """Karmaşıklık/overfitting denetimi: aktif opt-in katmanları kanıtla kıyasla (saf).

    `/ablation` mekanik gateleri, `/brain-attribution` beyin katmanlarını GERÇEK sonuçla
    ölçer; bu fonksiyon ondan ÖNCE gelir — "elimdeki veri bu katmanı açmaya yeter mi?".
    Her aktif (ON) katman sınıflanır: `structural` (geçmiş veri gerektirmez, çalışır) /
    `data-ready` (veri-aç katman + yeterli kapanmış işlem var) / `premature` (veri-aç
    ama örnek yetersiz → no-op veya gürültüden öğrenme). Claude maliyet çarpanı hesaplanır.

    `closed_n`: kapanmış gerçek işlem sayısı (kanıt tabanı). 'premature' katman = erken
    karmaşıklık → veri birikene dek kapat. Yalın çekirdek (eşik+teyit+SL/TP) hep güvenli.
    """
    # (flag, etiket, gereken_min_işlem|None=yapısal, kategori-ipucu)
    registry: list[tuple[bool, str, int | None]] = [
        (S.size_by_kelly, "Kelly boyutlama", S.kelly_min_trades),
        (S.brain_recalibrate, "Conviction rekalibrasyon", S.brain_recalibrate_min),
        (S.suppress_losing_sources, "Kaynak susturma", S.min_source_samples),
        (S.use_learned_vetoes, "Öğrenilmiş vetolar", 20),
        (S.brain_self_improve, "Beyin kendini-iyileştirme", 15),
        (S.regime_adapt, "Rejim adaptasyonu", 10),
        (S.auto_tune, "Oto-kalibrasyon (auto_tune)", 20),
        (S.brain_vote_count > 1, f"Çoklu-oylama (×{S.brain_vote_count})", 30),
        # yapısal (geçmiş veri gerektirmez — çalışır, ama hâlâ karmaşıklık)
        (S.size_by_impact, "Güç-bazlı boyutlama", None),
        (S.size_by_volume, "Hacim-bazlı boyutlama", None),
        (S.rvol_scale_by_impact, "İmpact-ölçekli RVOL", None),
        (S.risk_parity, "Risk-eşitleme", None),
        (S.portfolio_risk, "Portföy-ısı boyutlama", None),
        (S.use_entry_brain, "Giriş beyni (canlı-girdi, offline doğrulanamaz)", None),
        (S.brain_escalate, "Beyin eskalasyonu", None),
    ]
    active: list[dict[str, Any]] = []
    premature: list[str] = []
    for on, label, need in registry:
        if not on:
            continue
        if need is None:
            cat = "structural"
        elif closed_n >= need:
            cat = "data-ready"
        else:
            cat = "premature"
            premature.append(f"{label} (≥{need} işlem gerekli, {closed_n} var)")
        active.append({"layer": label, "category": cat, "needs_trades": need})

    # Claude maliyet çarpanı (gate'leri geçen aday başına giriş-beyni çağrısı)
    per_entry: float = (max(1, S.brain_vote_count) if S.use_entry_brain else 0)
    if S.use_entry_brain and S.brain_escalate:
        per_entry += 0.3   # kararsız bantta ara sıra ikinci (güçlü) model çağrısı
    claude = {"entry_brain": S.use_entry_brain, "vote_count": S.brain_vote_count,
              "escalate": S.brain_escalate, "calls_per_qualifying_entry": round(per_entry, 1)}

    n_active = len(active)
    if premature:
        verdict = (f"ERKEN KARMAŞIKLIK — {len(premature)} katman veri yetersizken aktif "
                   "(no-op/gürültü riski)")
    elif n_active >= 6 and closed_n < 30:
        verdict = "İZLE — çok sayıda katman aktif, kanıt tabanı henüz ince"
    elif n_active == 0:
        verdict = "YALIN — saf mekanik çekirdek (eşik+teyit+SL/TP)"
    else:
        verdict = "DİSİPLİNLİ — aktif katmanlar yapısal veya veriyle destekli"

    advice = [f"Kapat: {p}" for p in premature]
    if per_entry >= 3:
        advice.append(f"Claude maliyeti yüksek: aday başına ~{per_entry} çağrı "
                      "(oylama/eskalasyon) — edge kanıtlanana dek azalt.")
    return {
        "closed_trades": closed_n, "n_active_layers": n_active,
        "active_layers": active, "premature": premature,
        "claude_cost": claude, "verdict": verdict, "advice": advice,
        "note": "Yalın taban (lean preset) için güvenli başlangıç; katmanları edge "
                "kanıtlandıkça (bkz /ablation, /brain-attribution) geri ekle.",
    }


def connectivity_probe() -> dict[str, Any]:
    """Canlı borsa bağlantı probu (AĞ — auth + saat kayması + bakiye).

    Gerçek emir GÖNDERMEDEN borsanın erişilebilir ve anahtarların geçerli olduğunu
    doğrular: (1) sunucu saat kayması (imza reddi riski), (2) kimlik doğrulama,
    (3) serbest USDT bakiyesi. Paper modda/anahtar yoksa atlar. Kritik = canlıya geçme.
    """
    if not has_live_keys():
        return {"ok": False, "skipped": True, "reason": "canlı anahtar yok (.env)", "checks": []}
    checks: list[dict[str, Any]] = []
    ok = True

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    try:
        ex = _get_exchange()
    except Exception as e:
        return {"ok": False, "skipped": False,
                "checks": [{"check": "Borsa bağlantısı", "status": "critical",
                            "detail": f"kurulamadı: {e}"}]}

    # 1) Saat kayması: imzalı isteklerde borsa zamanıyla fark çok büyükse emir reddedilir
    try:
        server_ms = int(ex.fetch_time())
        skew = abs(server_ms - int(time.time() * 1000))
        if skew <= 1000:
            add("Saat kayması", "ok", f"{skew} ms")
        elif skew <= 5000:
            add("Saat kayması", "warn", f"{skew} ms — imza reddi riski (NTP senkronla)")
        else:
            add("Saat kayması", "critical", f"{skew} ms — emir imzaları reddedilir (NTP senkronla)")
            ok = False
    except Exception as e:
        add("Saat kayması", "warn", f"okunamadı: {e}")

    # 2) Kimlik doğrulama + 3) bakiye (tek özel-uç çağrısı)
    try:
        bal = ex.fetch_balance() or {}
        free = float((bal.get("free") or {}).get("USDT", 0) or 0)
        add("Kimlik doğrulama", "ok", "anahtarlar geçerli (özel uç erişildi)")
        add("USDT bakiye", "ok" if free > 0 else "warn", f"{free:.2f} USDT serbest")
    except Exception as e:
        add("Kimlik doğrulama", "critical", f"başarısız: {e}")
        ok = False

    # 4) API anahtarı izinleri (GÜVENLİK): çekim KAPALI olmalı + IP kısıtı önerilir.
    # Anahtar çalınsa bile çekim kapalıysa para çekilemez — kritik güvenlik kontrolü.
    try:
        fn = getattr(ex, "sapiGetAccountApiRestrictions", None)
        r = fn() if callable(fn) else None
        if isinstance(r, dict):
            if r.get("enableWithdrawals"):
                add("Çekim izni (API)", "critical",
                    "API anahtarında ÇEKİM AÇIK — KAPAT (anahtar çalınırsa paran çekilir)")
                ok = False
            else:
                add("Çekim izni (API)", "ok", "kapalı — güvenli (anahtar yalnız işlem/okuma)")
            add("IP kısıtlaması (API)", "ok" if r.get("ipRestrict") else "warn",
                "aktif" if r.get("ipRestrict") else "yok — anahtarı yalnız sunucu IP'sine kısıtla")
        else:
            add("API izinleri", "warn",
                "okunamadı — MANUEL doğrula: çekim KAPALI + IP whitelist açık olmalı")
    except Exception as e:
        add("API izinleri", "warn",
            f"okunamadı ({e}) — MANUEL doğrula: çekim KAPALI + IP whitelist")

    return {"ok": ok, "skipped": False, "checks": checks}


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


# ── Öğrenen beyin (öneri modu — otomatik UYGULAMAZ) ────────────────────────
# Kapanan işlemlerden hangi güç-dilimi / kaynak-tier / kaynak gerçekten kâr etti
# çıkarır ve eşik ayarı önerir. ASLA ayarı kendiliğinden değiştirmez — yalnızca
# panelde gösterilir; kararı kullanıcı verir (izlenebilirlik).
MIN_LEARN_SAMPLES = 10   # öneri üretmek için gereken min kapanmış işlem
_MIN_BUCKET_SAMPLES = 4  # bir dilimi/kaynağı yargılamak için min örnek

# RVOL (göreceli hacim) dilimleri — hangi hacim seviyesi kâr ediyor öğrenmek için
_RVOL_BANDS = (("<1.0", 0.0, 1.0), ("1.0-1.5", 1.0, 1.5), ("1.5-3", 1.5, 3.0), (">=3", 3.0, 1e9))


def _rvol_band(rv: float | None) -> str | None:
    """Bir RVOL değerinin düştüğü dilim etiketi (öğrenme bucket'ı için)."""
    if rv is None:
        return None
    for name, lo, hi in _RVOL_BANDS:
        if lo <= rv < hi:
            return name
    return None


def _hold_minutes(c: dict[str, Any]) -> float | None:
    """Bir kapanan işlemin tutma süresi (dakika): closed_at - opened_at."""
    a, b = _parse_dt(c.get("opened_at")), _parse_dt(c.get("closed_at"))
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds() / 60.0)


def _opened_hour(c: dict[str, Any]) -> int | None:
    """Bir işlemin açıldığı saat (UTC, 0-23) — saat-dilimi öğrenmesi için."""
    dt = _parse_dt(c.get("opened_at"))
    return dt.hour if dt is not None else None


def _impact_band(impact: Any) -> str | None:
    """Güç dilimi (koşullu edge için kaba grup): ≥9 = net/yüksek, ≤8 = sınırda."""
    if impact is None:
        return None
    return "≥9" if int(impact) >= 9 else "≤8"


# Koşullu (çok-değişkenli) edge boyutları: tek-boyutun kaçırdığı etkileşimler.
# Her biri (etiket, anahtar-fonksiyon). Anahtar bir tuple; herhangi bir alanı
# None ise o işlem o boyutta sayılmaz.
_COND_DIMS: tuple[tuple[str, Any], ...] = (
    ("kaynak×rvol", lambda c: (c.get("news_source"), _rvol_band(c.get("rel_volume")))),
    ("güç×rvol", lambda c: (_impact_band(c.get("impact")), _rvol_band(c.get("rel_volume")))),
    ("kaynak×yön", lambda c: (c.get("news_source"), c.get("side"))),
)


def _conditional_edges(trades: list[dict[str, Any]], value_key: str = "pnl",
                       *, top: int = 8) -> list[dict[str, Any]]:
    """Çok-değişkenli ANLAMLI edge'ler: 'X kaynağı sadece RVOL<1'de kaybettiriyor' gibi.

    Tek-boyutlu marjinal ortalamaların gizlediği koşullu kuralları bulur (Simpson).
    Yalnız istatistiksel anlamlı (GA 0'ı dışlayan) segmentleri, |beklenti|'ye göre döner.
    """
    edges: list[dict[str, Any]] = []
    for label, kf in _COND_DIMS:
        groups: dict[tuple, list[float]] = {}
        for c in trades:
            k = kf(c)
            if k is None or any(x is None for x in k):
                continue
            groups.setdefault(k, []).append(float(c[value_key]))
        for k, vals in groups.items():
            if len(vals) < _MIN_BUCKET_SAMPLES:
                continue
            ci = _expectancy_ci(vals)
            if not ci["significant"]:
                continue
            edges.append({
                "dim": label, "kind": label, "key": list(k),
                "condition": " & ".join(str(x) for x in k),
                "n": ci["n"], "avg_pnl": ci["mean"],
                "ci_lo": ci["ci_lo"], "ci_hi": ci["ci_hi"],
                "positive": ci["ci_lo"] > 0,
            })
    edges.sort(key=lambda e: abs(e["avg_pnl"]), reverse=True)
    return edges[:top]


# Öğrenilen-veto: anlamlı-negatif koşullu segmentler. Monitor hook'u tazeler
# (refresh_learned_vetoes); auto_decision use_learned_vetoes açıkken kontrol eder.
_learned_vetoes: list[dict[str, Any]] = []


def _item_segment(item: Any, kind: str) -> tuple | None:
    """Bir haber item'ının verili koşul-boyutundaki segment anahtarı (veto eşleştirme)."""
    rv = _rvol_band(getattr(item, "rel_volume", None))
    src = getattr(item, "source", None)
    side = "long" if getattr(item, "direction", "") == "bullish" else (
        "short" if getattr(item, "direction", "") == "bearish" else None)
    if kind == "kaynak×rvol":
        return (src, rv)
    if kind == "güç×rvol":
        return (_impact_band(getattr(item, "impact", None)), rv)
    if kind == "kaynak×yön":
        return (src, side)
    return None


def refresh_learned_vetoes() -> int:
    """Kapanan işlemlerden anlamlı-negatif koşullu segmentleri öğren (veto listesi).

    Monitor hook'undan çağrılır; use_learned_vetoes açıkken auto_decision bunları eler.
    Dönen: aktif veto sayısı.
    """
    global _learned_vetoes
    with _lock:
        closed = [c for c in _closed if c.get("pnl") is not None]
    edges = _conditional_edges(closed, "pnl")
    _learned_vetoes = [{"kind": e["kind"], "key": e["key"], "avg_pnl": e["avg_pnl"], "n": e["n"]}
                       for e in edges if not e["positive"]]
    return len(_learned_vetoes)


def _learned_veto_hit(item: Any) -> dict[str, Any] | None:
    """item öğrenilmiş anlamlı-negatif bir segmente düşüyorsa o vetoyu döner, yoksa None."""
    for v in _learned_vetoes:
        seg = _item_segment(item, v["kind"])
        if seg is not None and list(seg) == v["key"]:
            return v
    return None


# İstatistiksel güven: ham ortalama yerine güven aralığı / Wilson alt sınırı kullan
# ki beyin AZ örnekli gürültüyü "edge" sanmasın (kapalı döngüde kritik).
_Z = 1.96   # %95 güven (normal yaklaşım)


def _expectancy_ci(values: list[float]) -> dict[str, Any]:
    """Bir değer kümesinin ortalaması + %95 güven aralığı (normal yaklaşım).

    `significant`: aralık 0'ı dışlıyor mu (anlamlı pozitif/negatif). n<2 → anlamsız.
    """
    n = len(values)
    if n == 0:
        return {"mean": 0.0, "ci_lo": 0.0, "ci_hi": 0.0, "n": 0, "significant": False}
    mean = sum(values) / n
    if n < 2:
        return {"mean": round(mean, 3), "ci_lo": round(mean, 3), "ci_hi": round(mean, 3),
                "n": n, "significant": False}
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    se = math.sqrt(var) / math.sqrt(n)
    lo, hi = mean - _Z * se, mean + _Z * se
    return {"mean": round(mean, 3), "ci_lo": round(lo, 3), "ci_hi": round(hi, 3),
            "n": n, "significant": lo > 0 or hi < 0}


def _wilson_lo(wins: int, n: int) -> float:
    """Kazanma oranı için Wilson skor aralığının ALT sınırı (%95). Az örnekte ihtiyatlı."""
    if n == 0:
        return 0.0
    p = wins / n
    z2 = _Z * _Z
    denom = 1 + z2 / n
    centre = p + z2 / (2 * n)
    margin = _Z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    return round(max(0.0, (centre - margin) / denom), 3)


def _regime_check(trades: list[dict[str, Any]], value_key: str = "pnl") -> dict[str, Any]:
    """Rejim-kayması: SON yarı ile ESKİ yarının beklentisi belirgin farklı mı.

    İşlemleri zamana göre sırala, ikiye böl, iki yarının %95 GA'sını karşılaştır.
    `shifted`: aralıklar çakışmıyor (dağılım değişti) → eski veri güncel piyasayı
    yansıtmıyor olabilir; öğrenmede son veriye daha çok güven. `improving`: son>eski.
    """
    timed: list[tuple[datetime, dict[str, Any]]] = []
    for c in trades:
        t = _parse_dt(c.get("closed_at")) or _parse_dt(c.get("opened_at"))
        if t is not None:
            timed.append((t, c))
    if len(timed) < 2 * _MIN_BUCKET_SAMPLES:
        return {"ready": False, "shifted": False}
    timed.sort(key=lambda x: x[0])
    mid = len(timed) // 2
    older = _expectancy_ci([float(c[value_key]) for _, c in timed[:mid]])
    recent = _expectancy_ci([float(c[value_key]) for _, c in timed[mid:]])
    # GA'lar çakışmıyorsa dağılım değişmiş (rejim kayması)
    shifted = recent["ci_hi"] < older["ci_lo"] or recent["ci_lo"] > older["ci_hi"]
    return {"ready": True, "shifted": shifted, "improving": recent["mean"] > older["mean"],
            "recent_avg": recent["mean"], "older_avg": older["mean"],
            "recent_n": recent["n"], "older_n": older["n"]}


def _bucket_stats(trades: list[dict[str, Any]], key_fn: Any,
                  value_key: str = "pnl") -> dict[str, dict[str, Any]]:
    """trades'i key_fn'e göre grupla: her dilim için sayım + beklenti + İSTATİSTİKSEL GÜVEN.

    Döner: {key: {count, pnl, avg_pnl, win_rate, ci_lo, ci_hi, significant, wilson_lo}}.
    `significant`: ort. beklentinin %95 GA'sı 0'ı dışlıyor mu (gürültü değil, gerçek edge).
    value_key: kâr alanı — canlı işlemde 'pnl' (USDT), backtest'te 'net_pct' (%)."""
    vals: dict[str, list[float]] = {}
    wins: dict[str, int] = {}
    for c in trades:
        k = key_fn(c)
        if k is None:
            continue
        sk = str(k)
        v = float(c[value_key])
        vals.setdefault(sk, []).append(v)
        wins[sk] = wins.get(sk, 0) + (1 if v > 0 else 0)
    out: dict[str, dict[str, Any]] = {}
    for sk, vlist in vals.items():
        n = len(vlist)
        ci = _expectancy_ci(vlist)
        out[sk] = {
            "count": n, "pnl": round(sum(vlist), 3), "wins": wins[sk],
            "avg_pnl": ci["mean"], "win_rate": round(wins[sk] / n * 100, 1),
            "ci_lo": ci["ci_lo"], "ci_hi": ci["ci_hi"],
            "significant": ci["significant"], "wilson_lo": _wilson_lo(wins[sk], n),
        }
    return out


def _suggest_from_trades(trades: list[dict[str, Any]], *, value_key: str, source_key: str,
                         tier_of: Any, unit: str) -> dict[str, Any]:
    """İşlem/sonuç listesinden eşik önerileri üret (saf — canlı VE backtest için ortak).

    value_key: kâr alanı; source_key: haber kaynağı alanı; unit: mesaj birimi (' USDT'/'%').
    YAN ETKİSİZ — yalnızca öneri döndürür, ayar değiştirmez.
    """
    n = len(trades)
    by_impact = _bucket_stats(trades, lambda c: int(c["impact"]) if c.get("impact") else None, value_key)
    by_source = _bucket_stats(trades, lambda c: c.get(source_key) or None, value_key)
    by_tier = (_bucket_stats(trades, lambda c: tier_of(c.get(source_key) or "") if c.get(source_key) else None, value_key)
               if tier_of else {})
    # Çok-boyutlu öğrenme: yön / coin / saat-dilimi / RVOL (hacim) dilimi
    by_direction = _bucket_stats(trades, lambda c: c.get("side") or None, value_key)
    by_coin = _bucket_stats(trades, lambda c: c.get("symbol") or None, value_key)
    by_hour = _bucket_stats(trades, _opened_hour, value_key)
    by_rvol = _bucket_stats(trades, lambda c: _rvol_band(c.get("rel_volume")), value_key)
    dims = {"by_impact": by_impact, "by_tier": by_tier, "by_source": by_source,
            "by_direction": by_direction, "by_coin": by_coin, "by_hour": by_hour, "by_rvol": by_rvol}

    suggestions: list[dict[str, Any]] = []
    if n < MIN_LEARN_SAMPLES:
        return {"ready": False, "samples": n, "min_samples": MIN_LEARN_SAMPLES,
                "suggestions": [], **dims}

    # 1) auto_min_impact: beklentiyi en yükseğe çıkaran eşik — ANLAMLI pozitif olmalı
    #    (o eşik ve üstü beklentinin %95 GA alt sınırı > 0; gürültüye göre eşik oynatma).
    impacts = sorted(int(k) for k in by_impact)
    best_t, best_avg = None, None
    for t in impacts:
        rows = [float(c[value_key]) for c in trades if c.get("impact") and int(c["impact"]) >= t]
        if len(rows) < _MIN_BUCKET_SAMPLES:
            continue
        ci = _expectancy_ci(rows)
        if ci["ci_lo"] <= 0:        # anlamlı pozitif değil → güvenme
            continue
        if best_avg is None or ci["mean"] > best_avg:
            best_t, best_avg = t, ci["mean"]
    if best_t is not None and best_avg is not None and best_t != S.auto_min_impact:
        verb = "yükselt" if best_t > S.auto_min_impact else "düşür"
        suggestions.append({
            "type": "auto_min_impact", "current": S.auto_min_impact, "suggested": best_t,
            "message": f"Oto min. gücü {S.auto_min_impact}→{best_t} {verb}: güç ≥{best_t} "
                       f"ort. {best_avg}{unit} (istatistiksel anlamlı pozitif).",
        })

    # 2) kaynak-tier: ANLAMLI negatif beklentili (GA üst sınırı < 0) tier'i kıs
    for tier, d in by_tier.items():
        if d["count"] >= MIN_LEARN_SAMPLES and d["significant"] and d["ci_hi"] < 0:
            suggestions.append({
                "type": "suppress_tier", "tier": tier, "avg_pnl": d["avg_pnl"], "count": d["count"],
                "message": f"'{tier}' kaynak sınıfı ANLAMLI negatif (ort. {d['avg_pnl']}{unit}, "
                           f"GA≤{d['ci_hi']}, {d['count']} örnek) — bu sınıfı kıs/eşiği artır.",
            })

    # 3) tek kaynak: ANLAMLI negatif beklentili kaynağı sustur (gürültü değil, gerçek)
    for src, d in by_source.items():
        if d["count"] >= S.min_source_samples and d["significant"] and d["ci_hi"] < 0:
            suggestions.append({
                "type": "suppress_source", "source": src, "avg_pnl": d["avg_pnl"], "count": d["count"],
                "message": f"Kaynak '{src}' ANLAMLI negatif (ort. {d['avg_pnl']}{unit}, "
                           f"GA≤{d['ci_hi']}, {d['count']} örnek) — 'kaybeden kaynağı sustur' eler.",
            })

    # 4) min_rel_volume: düşük-RVOL dilim(ler)i negatif, üst dilim pozitifse hacim eşiği öner
    #    (hacimsiz haber = fake → girme). En düşük POZİTİF beklentili dilimin tabanını öner.
    rv_ok = [(lo, by_rvol[name]) for name, lo, _ in _RVOL_BANDS
             if name in by_rvol and by_rvol[name]["count"] >= _MIN_BUCKET_SAMPLES]
    if len(rv_ok) >= 2:
        # ANLAMLI: düşük dilim GA üst<0 (gerçek negatif), üst dilim GA alt>0 (gerçek pozitif)
        neg_low = any(lo < 1.5 and d["significant"] and d["ci_hi"] < 0 for lo, d in rv_ok)
        pos_bands = [lo for lo, d in rv_ok if d["significant"] and d["ci_lo"] > 0]
        if neg_low and pos_bands:
            cutoff = max(1.0, min(pos_bands))   # ilk pozitif dilimin tabanı (≥1.0)
            if abs(cutoff - S.min_rel_volume) >= 0.25:
                suggestions.append({
                    "type": "min_rel_volume", "current": S.min_rel_volume, "suggested": cutoff,
                    "message": f"Min RVOL {S.min_rel_volume}→{cutoff}: düşük hacimli dilim negatif, "
                               f"≥{cutoff}x pozitif beklenti — hacimsiz/fake haberleri eler.",
                })

    # 5) time_stop: kaybedenler kazananlardan belirgin uzun tutuluyorsa süre-stop öner
    #    (edge sönünce kes). Kazananların ort. tutma süresi × 1.5 makul tavan.
    win_h = [h for c in trades if (h := _hold_minutes(c)) is not None and float(c[value_key]) > 0]
    loss_h = [h for c in trades if (h := _hold_minutes(c)) is not None and float(c[value_key]) <= 0]
    if len(win_h) >= _MIN_BUCKET_SAMPLES and len(loss_h) >= _MIN_BUCKET_SAMPLES:
        avg_win, avg_loss = sum(win_h) / len(win_h), sum(loss_h) / len(loss_h)
        if avg_loss > avg_win * 1.5:
            sug = round(avg_win * 1.5)
            if sug > 0 and (S.time_stop_min == 0 or abs(sug - S.time_stop_min) >= 10):
                suggestions.append({
                    "type": "time_stop", "current": S.time_stop_min, "suggested": sug,
                    "message": f"Süre-stop {S.time_stop_min}→{sug}dk: kaybedenler ort. {avg_loss:.0f}dk "
                               f"tutuluyor, kazananlar {avg_win:.0f}dk — geç çıkış zarar büyütüyor.",
                })

    # 6b) segment SL/TP: gerçekleşen MFE/MAE'den optimal stop/hedef öğren
    #     SL ≈ kazananların MAE'sinin p75'i (kazananları erken stop'lama),
    #     TP ≈ tüm işlemlerin MFE medyanı (gerçekçi yakalanabilir hedef).
    win_mae = sorted(c["mae_pct"] for c in trades
                     if c.get("mae_pct") is not None and float(c[value_key]) > 0)
    all_mfe = sorted(c["mfe_pct"] for c in trades if c.get("mfe_pct") is not None)
    if len(win_mae) >= MIN_LEARN_SAMPLES and len(all_mfe) >= MIN_LEARN_SAMPLES:
        sl_sug = round(min(15.0, max(0.5, win_mae[int(len(win_mae) * 0.75)] * 1.1)), 1)
        tp_sug = round(min(30.0, max(1.0, all_mfe[len(all_mfe) // 2])), 1)
        if sl_sug > 0 and abs(sl_sug - S.stop_loss_pct) >= 0.5:
            suggestions.append({
                "type": "stop_loss_pct", "current": S.stop_loss_pct, "suggested": sl_sug,
                "message": f"SL {S.stop_loss_pct}→{sl_sug}%: kazananların %75'i {sl_sug}% "
                           f"içinde dipledi — daha sıkı SL kazananları erken stop'luyor.",
            })
        if tp_sug > 0 and abs(tp_sug - S.take_profit_pct) >= 0.5:
            suggestions.append({
                "type": "take_profit_pct", "current": S.take_profit_pct, "suggested": tp_sug,
                "message": f"TP {S.take_profit_pct}→{tp_sug}%: işlemlerin medyan en-iyi hareketi "
                           f"{tp_sug}% — hedef gerçekçi yakalanabilir seviyeye.",
            })

    # 6) rejim-kayması: son yarı eski yarıdan ANLAMLI farklıysa uyar (eski veriye güvenme)
    regime = _regime_check(trades, value_key)
    if regime.get("shifted"):
        yon = "iyileşme" if regime["improving"] else "BOZULMA"
        suggestions.append({
            "type": "regime_shift", "improving": regime["improving"],
            "recent_avg": regime["recent_avg"], "older_avg": regime["older_avg"],
            "message": f"Rejim kayması ({yon}): son dönem ort. {regime['recent_avg']}{unit} "
                       f"vs eski {regime['older_avg']}{unit} — eski veriye dayalı kararları "
                       f"{'gözden geçir' if not regime['improving'] else 'güncelle'}.",
        })

    # 7) koşullu edge'ler: tek-boyutun kaçırdığı etkileşimler (kaynak×rvol vb.)
    cond = _conditional_edges(trades, value_key)
    for e in cond:
        if not e["positive"]:   # anlamlı NEGATİF koşul → kaçın
            suggestions.append({
                "type": "conditional_avoid", "kind": e["kind"], "condition": e["condition"],
                "avg_pnl": e["avg_pnl"], "count": e["n"],
                "message": f"Koşullu kaçın [{e['dim']}: {e['condition']}]: ANLAMLI negatif "
                           f"(ort. {e['avg_pnl']}{unit}, {e['n']} örnek) — bu kombinasyonda girme.",
            })

    return {"ready": True, "samples": n, "min_samples": MIN_LEARN_SAMPLES,
            "suggestions": suggestions, "regime": regime,
            "conditional_edges": cond, **dims}


def suggest_tuning(tier_of: Any = None) -> dict[str, Any]:
    """Kapanan GERÇEK işlemlerden ayar önerileri üret (YAN ETKİSİZ — uygulamaz).

    tier_of: news_source -> tier eşleyen opsiyonel callable (news_bot._source_tier).
    """
    with _lock:
        closed = [c for c in _closed if c.get("pnl") is not None]
    return _suggest_from_trades(closed, value_key="pnl", source_key="news_source",
                                tier_of=tier_of, unit=" USDT")


def auto_apply_tuning(tier_of: Any = None) -> dict[str, Any]:
    """Kapalı döngü: auto_tune AÇIKSA öğrenen beyin önerilerini korkuluklarla oto-uygula.

    auto_tune kapalıyken hiçbir şey yapmaz (no-op). Açıkken suggest_tuning + apply_tuning
    zincirini koşar; yalnız güvenli ayarları (eşik/kaynak/RVOL/süre-stop) değiştirir,
    para-büyüklüğü/risk tavanlarına dokunmaz. Uygulanan değişiklikleri döner.
    """
    if not S.auto_tune:
        return {"applied": False, "reason": "auto_tune kapalı", "changes": []}
    return apply_tuning(suggest_tuning(tier_of=tier_of))


# Rejim bozulmasında uygulanacak geçici eşik sıkılaştırması (korkuluk: tek seferde +1, tavan +2)
_REGIME_BUMP_STEP = 1
_REGIME_BUMP_MAX = 2


def regime_adapt_step() -> dict[str, Any]:
    """Rejim BOZULMASINDA eşiği geçici sıkılaştır; toparlanınca GERİ AL (opt-in).

    Kapanan gerçek işlemlerden `_regime_check`: son yarı eski yarıdan ANLAMLI kötüyse
    (shifted & improving=False) piyasa bozulmuş → `auto_min_impact`'i geçici +1 yükselt
    (daha seçici ol, tavan +2). Rejim toparlanınca (shifted=False ya da improving) orijinal
    eşiğe DÖN. Kalıcı değil — durum bilgisi `_regime_state`'te; yalnız EŞİĞE dokunur,
    para-büyüklüğü/risk tavanına ASLA. Döner: {acted, state, change?}.
    """
    if not S.regime_adapt:
        return {"acted": False, "reason": "regime_adapt kapalı"}
    with _lock:
        closed = [c for c in _closed if c.get("pnl") is not None]
    regime = _regime_check(closed)
    if not regime.get("ready"):
        return {"acted": False, "reason": "yeterli örnek yok", "regime": regime}
    deteriorating = bool(regime["shifted"] and not regime["improving"])
    st = _regime_state
    # BOZULMA: eşiği sıkılaştır (henüz tavanda değilse)
    if deteriorating:
        if not st["active"]:
            st["restore"] = S.auto_min_impact   # ilk sıkılaştırmada orijinali sakla
            st["bump"] = 0
        if st["bump"] < _REGIME_BUMP_MAX:
            old = S.auto_min_impact
            new = min(10, old + _REGIME_BUMP_STEP)
            if new != old:
                S.auto_min_impact = new
                st["active"] = True
                st["bump"] += _REGIME_BUMP_STEP
                st["since"] = _now()
                with _lock:
                    _save_state()
                log.info("Rejim adaptasyonu: BOZULMA → eşik %s→%s (geçici)", old, new)
                return {"acted": True, "state": "tighten", "regime": regime,
                        "change": {"field": "auto_min_impact", "from": old, "to": new}}
        return {"acted": False, "state": "tightened-max", "regime": regime}
    # TOPARLANMA (artık bozulma yok): geçici sıkılaştırmayı geri al
    if st["active"] and st["restore"] is not None:
        old = S.auto_min_impact
        S.auto_min_impact = int(st["restore"])
        restored = S.auto_min_impact
        st.update({"active": False, "restore": None, "bump": 0, "since": ""})
        with _lock:
            _save_state()
        log.info("Rejim adaptasyonu: toparlanma → eşik %s→%s (geri alındı)", old, restored)
        return {"acted": True, "state": "restore", "regime": regime,
                "change": {"field": "auto_min_impact", "from": old, "to": restored}}
    return {"acted": False, "state": "stable", "regime": regime}


def get_regime_state() -> dict[str, Any]:
    """Rejim adaptasyon durumu (panel/uç için): aktif mi, ne kadar sıkılaştırıldı."""
    return {"enabled": S.regime_adapt, "active": _regime_state["active"],
            "bump": _regime_state["bump"], "restore": _regime_state["restore"],
            "since": _regime_state["since"]}


def apply_tuning(suggestion: dict[str, Any], *, min_impact_floor: int = 7) -> dict[str, Any]:
    """Öğrenen beynin önerilerini KORKULUKLARLA uygula (oto-kalibrasyon).

    Yalnızca güvenli ayarları otomatik değiştirir:
    - `auto_min_impact`: önerilen eşik (tabana [min_impact_floor] ve 10'a kıstırılır)
    - kaynak susturma: negatif-beklenti önerisi varsa `suppress_losing_sources` aç

    Risk tavanları/boyut/kaldıraç gibi para-büyüklüğü ayarlarına DOKUNMAZ. Yeterli
    örnek yoksa (`ready=False`) hiçbir şey değiştirmez. Uygulanan değişiklikleri döner.
    """
    if not suggestion.get("ready"):
        return {"applied": False, "reason": "yeterli örnek yok",
                "samples": suggestion.get("samples", 0), "changes": []}
    changes: list[dict[str, Any]] = []
    for s in suggestion.get("suggestions", []):
        if s["type"] == "auto_min_impact":
            new = max(min_impact_floor, min(10, int(s["suggested"])))
            if new != S.auto_min_impact:
                changes.append({"field": "auto_min_impact", "from": S.auto_min_impact, "to": new})
                S.auto_min_impact = new
        elif s["type"] in ("suppress_source", "suppress_tier") and not S.suppress_losing_sources:
            changes.append({"field": "suppress_losing_sources", "from": False, "to": True})
            S.suppress_losing_sources = True
        elif s["type"] == "min_rel_volume":
            new_rv = max(0.0, min(5.0, round(float(s["suggested"]), 2)))   # korkuluk: [0,5]
            if abs(new_rv - S.min_rel_volume) >= 0.25:
                changes.append({"field": "min_rel_volume", "from": S.min_rel_volume, "to": new_rv})
                S.min_rel_volume = new_rv
        elif s["type"] == "time_stop":
            new_ts = max(0, min(720, int(s["suggested"])))   # korkuluk: [0,720]dk (12s)
            if new_ts > 0 and (S.time_stop_min == 0 or abs(new_ts - S.time_stop_min) >= 10):
                changes.append({"field": "time_stop_min", "from": S.time_stop_min, "to": new_ts})
                S.time_stop_min = new_ts
        elif s["type"] == "stop_loss_pct":
            new_sl = max(0.5, min(15.0, round(float(s["suggested"]), 1)))   # korkuluk [0.5,15]
            if abs(new_sl - S.stop_loss_pct) >= 0.5:
                changes.append({"field": "stop_loss_pct", "from": S.stop_loss_pct, "to": new_sl})
                S.stop_loss_pct = new_sl
        elif s["type"] == "take_profit_pct":
            new_tp = max(1.0, min(30.0, round(float(s["suggested"]), 1)))   # korkuluk [1,30]
            if abs(new_tp - S.take_profit_pct) >= 0.5:
                changes.append({"field": "take_profit_pct", "from": S.take_profit_pct, "to": new_tp})
                S.take_profit_pct = new_tp
    if changes:
        with _lock:
            _save_state()
        log.info("Oto-kalibrasyon uygulandı: %s", changes)
    return {"applied": bool(changes), "samples": suggestion.get("samples", 0), "changes": changes}


_ABLATION_APPLY_KEYS = ("auto_min_impact", "auto_require_confirm",
                        "min_rel_volume", "skip_already_priced_pct")


def apply_ablation_recommendation(rec: dict[str, Any]) -> dict[str, Any]:
    """Ablation aramasının `recommended_settings`'ini KORKULUKLARLA uygula.

    `/ablation/search` "hangi gate kombinasyonu edge katıyor" bulur; bu fonksiyon o
    öneriyi (yalnız güvenli KARAR-EŞİĞİ alanları, `_ABLATION_APPLY_KEYS`) kıstırarak
    uygular — risk/boyut/kaldıraç gibi para-büyüklüğü ayarlarına ASLA dokunmaz. Açık
    kullanıcı eylemiyle çağrılır (oto-uygulanmaz). Uygulanan değişiklikleri döner.

    Korkuluklar: auto_min_impact→[7,10] · auto_require_confirm→yalnız True (teyit
    zorunluluğunu GEVŞETMEZ) · min_rel_volume→[0,5] · skip_already_priced_pct→[0,50].
    """
    changes: list[dict[str, Any]] = []

    def _set(field: str, new: Any) -> None:
        old = getattr(S, field)
        if old != new:
            changes.append({"field": field, "from": old, "to": new})
            setattr(S, field, new)

    if "auto_min_impact" in rec:
        _set("auto_min_impact", max(7, min(10, int(rec["auto_min_impact"]))))
    if rec.get("auto_require_confirm") is True and not S.auto_require_confirm:
        _set("auto_require_confirm", True)
    if "min_rel_volume" in rec:
        new_rv = max(0.0, min(5.0, round(float(rec["min_rel_volume"]), 2)))
        if abs(new_rv - S.min_rel_volume) >= 0.05:
            _set("min_rel_volume", new_rv)
    if "skip_already_priced_pct" in rec:
        new_sp = max(0.0, min(50.0, round(float(rec["skip_already_priced_pct"]), 1)))
        if abs(new_sp - S.skip_already_priced_pct) >= 0.1:
            _set("skip_already_priced_pct", new_sp)
    if changes:
        with _lock:
            _save_state()
        log.info("Ablation önerisi uygulandı: %s", changes)
    return {"applied": bool(changes), "changes": changes}


def suggest_from_backtest(results: list[dict[str, Any]], tier_of: Any = None) -> dict[str, Any]:
    """İşlemsiz ÖN-BİLGİ: backtest sonuçlarından (arşiv simülasyonu) aynı önerileri üret.

    Gerçek para riske atmadan kalibrasyon → sistem ilk işlemden itibaren akıllı. Backtest
    sonucu net %% (`net_pct`) ve haber kaynağı (`source`) taşır.
    """
    trades = [r for r in results if r.get("net_pct") is not None]
    out = _suggest_from_trades(trades, value_key="net_pct", source_key="source",
                               tier_of=tier_of, unit="%")
    out["pretrade"] = True
    return out


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
    """Ayarları uygula (transaksiyonel: reddedilirse kısmi commit YOK).

    Canlı oto-işlemi ETKİNLEŞTİREN bir değişiklik + ön-uçuşta (`preflight`) kritik
    eksik varsa GUARD-RAIL devreye girer ve değişiklik bloklanır (kör canlıya geçiş
    önleme). Zaten canlı+oto iken diğer alanları düzenlemek bloklanmaz (kilitlenme yok).
    """
    global _exchange
    snapshot = {k: getattr(S, k) for k in _PERSIST_KEYS}
    market_changed = "market" in patch and patch["market"] != S.market
    try:
        for k in _PERSIST_KEYS:
            if k in patch and patch[k] is not None and k not in ("paper_trading", "auto_trade"):
                setattr(S, k, patch[k])
        if "paper_trading" in patch and patch["paper_trading"] is not None:
            S.paper_trading = bool(patch["paper_trading"])
        if "auto_trade" in patch and patch["auto_trade"] is not None:
            if patch["auto_trade"] and not S.paper_trading and not has_live_keys():
                raise RuntimeError("Canlı otomatik işlem için .env'de Binance anahtarları gerekli")
            S.auto_trade = bool(patch["auto_trade"])
        # Guard-rail: canlı oto-işlemi ETKİNLEŞTİREN değişiklik + ön-uçuş kritik → blokla
        enabling_live = S.auto_trade and not S.paper_trading and (
            bool(patch.get("auto_trade")) or patch.get("paper_trading") is False)
        if enabling_live:
            crit = [c for c in preflight() if c["status"] == "critical"]
            if crit:
                raise RuntimeError(
                    "Canlıya geçiş engellendi — ön-uçuş kritik eksik(ler): "
                    + "; ".join(f"{c['check']} ({c['detail']})" for c in crit)
                    + ". /preflight ile düzelt veya halt_trade_on_latency gibi kapıyı gözden geçir.")
    except Exception:
        for k, v in snapshot.items():   # kısmi commit'i geri al
            setattr(S, k, v)
        raise
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
        "size_by_impact": True,                     # conviction sizing (güce göre)
        "size_by_volume": True,                     # likidite katmanı (ince coinde küçül)
        "min_rel_volume": 1.5,                      # RVOL<1.5x = hacimsiz/fake → girme
        "max_book_frac": 0.10,                      # pozisyon orderbook'un en fazla %10'u
        "tier1_skip_confirm_impact": 9,             # güç≥9 net haberde refleks giriş
    },
    "safe": {
        "stop_loss_pct": 3.0, "take_profit_pct": 6.0,
        "breakeven_pct": 0.0,
        "partial_tp_pct": 0.0, "partial_tp_frac": 0.5,
        "trailing_stop_pct": 0.0,
        "time_stop_min": 0,
        "size_by_impact": False,
        "size_by_volume": False,
        "min_rel_volume": 0.0,
        "max_book_frac": 0.0,
        "tier1_skip_confirm_impact": 0,
    },
    # MİNİMAL UYGULANABİLİR STRATEJİ: saf mekanik çekirdek — puanla → impact eşiği →
    # teyit → SL/TP. Tüm spekülatif/öğrenen/boyutlandırma katmanları KAPALI. "Önce
    # edge'i kanıtla, sonra ekle" disiplini için açık taban (news preset'ine A/B temeli).
    # Güvenlik altyapısı (native-stop/halt-gate'ler) preset DIŞIDIR — hep açık kalır.
    "lean": {
        "stop_loss_pct": 3.0, "take_profit_pct": 6.0, "auto_require_confirm": True,
        # çıkış: yalnız sabit SL/TP (akıllı çıkış ek katmanları kapalı)
        "breakeven_pct": 0.0, "partial_tp_pct": 0.0, "trailing_stop_pct": 0.0,
        "time_stop_min": 0, "partial_tp_levels": "",
        "use_atr_exits": False, "use_atr_trailing": False,
        # boyutlama: düz trade_usdt (güç/hacim/Kelly/risk-parity/portföy katmanları kapalı)
        "size_by_impact": False, "size_by_volume": False, "size_by_kelly": False,
        "risk_parity": False, "portfolio_risk": False, "rvol_scale_by_impact": False,
        # giriş beyni yığını kapalı (Claude maliyeti + doğrulanamaz karmaşıklık)
        "use_entry_brain": False, "brain_escalate": False, "brain_vote_count": 1,
        "brain_recalibrate": False, "brain_self_improve": False,
        # öğrenen/uyarlayan katmanlar kapalı (veri birikene dek gürültü)
        "suppress_losing_sources": False, "use_learned_vetoes": False,
        "regime_adapt": False, "auto_tune": False,
        # kapılar: refleks/RVOL/book-frac kapalı (saf eşik+teyit)
        "tier1_skip_confirm_impact": 0, "min_rel_volume": 0.0, "max_book_frac": 0.0,
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
