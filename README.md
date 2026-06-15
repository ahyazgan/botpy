# Kripto Haber-Trade Radarı

Gerçek zamanlı kripto haberlerini yakalayıp **puanlayan**, Binance fiyatıyla **teyit eden**, masaüstü/Telegram'a **bildiren** ve isteğe bağlı olarak **otomatik işlem** açan bir radar. Backtest, walk-forward doğrulama, risk yönetimi ve canlı performans paneliyle gelir.

> ⚠️ **Güvenlik önce:** Varsayılan **paper (simülasyon)** modundadır — gerçek emir göndermez. Canlıya geçmeden risk limitlerini gözden geçirin.

## Hızlı başlangıç

```bash
# 1) Bağımlılıklar
pip install -r requirements.txt

# 2) Haber motoru + API (http://127.0.0.1:8000)
python news_bot.py
#   sadece konsol modu (API'siz): python news_bot.py --cli

# 3) Panel (ayrı terminal)
cd dashboard && npm install && npm run dev   # http://localhost:5173
```

`.env` opsiyoneldir (hiçbiri yoksa kural-tabanlı puanlama + paper modda çalışır). Bkz. `.env.example`.

## Nasıl çalışır

1. **Kaynaklar** — TreeNews WebSocket (gerçek zamanlı) + RSS/Binance duyuruları (yedek polling).
2. **Puanlama** — `ANTHROPIC_API_KEY` varsa Claude ile akıllı puanlama, yoksa kural-tabanlı. Her haber: coin(ler), etki gücü (1-10), yön (yükseliş/düşüş), gerekçe.
3. **Teyit** — güçlü haberler için Binance 24s/15dk fiyat hareketi + likidite kontrolü.
4. **Aksiyon** — güç ≥ eşik → masaüstü (winotify) + uzak (Telegram/Discord) bildirim; otomatik işlem açıksa kurallar sağlanırsa pozisyon açılır.
5. **Çıkış & risk** — SL/TP/trailing + akıllı çıkış (time-stop, breakeven, kısmi TP); günlük zarar freni, maruziyet/risk tavanları, kill-switch.

## Yapılandırma

### Ortam değişkenleri (`.env`)
| Değişken | Etki |
|----------|------|
| `ANTHROPIC_API_KEY` | Claude ile akıllı haber puanlaması (yoksa kural-tabanlı) |
| `BINANCE_API_KEY` / `BINANCE_SECRET` | CANLI işlem (yoksa paper) — para çekme izni KAPALI olmalı |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `DISCORD_WEBHOOK_URL` | Uzak bildirim (telefona sinyal) |
| `BOTPY_DB` | SQLite yolu (varsayılan `botpy.db`) |
| `API_TOKEN` | Tanımlıysa işlem/ayar uçları `X-API-Token` ister — **sunucuyu dışa açarsan ayarla** |
| `CONFIRM_INTERVAL` / `CONFIRM_LIMIT` | Fiyat teyit penceresi (varsayılan `15m`×`4`; daha erken/gürültülü teyit için `1m`×`15`) |
| `WS_STALE_ALERT_SEC` | Ölü-adam anahtarı eşiği: haber akışı bu kadar saniye kopuk/sessizse uzak kanaldan uyar (varsayılan 600) |

### Çalışma zamanı ayarları (panelden, kalıcı)
İşlem ayarları `trade_state.json`'a, haber ayarları SQLite'a yazılır; restart'a dayanıklıdır.
- **İşlem:** paper/canlı, oto-işlem, spot/futures, pozisyon boyutu, conviction sizing (güce göre boyut)
- **Giriş:** Tier-1 refleks (`tier1_skip_confirm_impact`: net/yüksek-güç haberde teyit beklemeden gir — hareketin önünde ol; altındakiler teyit bekler)
- **Çıkış:** SL/TP %, trailing, time-stop dk, breakeven %, kısmi TP %/oran — panelde **"⚡ Haber-trade preset'i"** ile tek tıkla optimal düzen (hızlı breakeven + erken kısmi TP + trailing + 60dk time-stop + tier-1), **"Muhafazakâr"** ile geri dön
- **Risk:** günlük zarar limiti, toplam/coin maruziyet tavanı, max açık risk, kayıp serisi freni
- **Sinyal kalitesi:** uyarı eşiği, "zaten-fiyatlanmış" atla (chase önleme), kaybeden kaynağı sustur

## Panel

Canlı haber akışı, açık pozisyonlar, **risk & maruziyet** metreleri (kill-switch rozeti), **performans** (kazanma oranı, kümülatif P&L eğrisi, max drawdown, profit factor, payoff, Sharpe), **işlem günlüğü** + CSV indirme, **backtest** (basit/grid/walk-forward + güç-dilimi kırılımı), **sinyal arşivi** tarayıcısı ve sağlık şeridi.

## Backtest

Arşivlenmiş güçlü sinyaller üzerinde (motor çalışmasa da) geçmiş fiyatla simülasyon:

```bash
python news_backtest.py --db botpy.db                # basit (SL=3 TP=6)
python news_backtest.py --db botpy.db --grid         # en kârlı SL/TP araması
python news_backtest.py --db botpy.db --walk         # walk-forward (overfit testi)
# Panelden "Akıllı çıkış" modu: mevcut ayarları/preset'i (breakeven+kısmi TP+
# trailing+time-stop) arşivde simüle eder — haber-trade preset'ini canlıdan önce doğrula.
```

Panelden de çalıştırılabilir (Backtest bölümü). Güç-dilimi/yön/kaynak kırılımıyla `auto_min_impact`/eşik veriyle ayarlanır.

## Güvenlik modeli

- **Paper varsayılan** — `paper_trading=True`, gerçek emir yok.
- **Kill-switch** — günlük zarar limiti aşılınca yeni işlem durur.
- **Risk tavanları** — toplam/coin maruziyet + açık SL-riski sınırları.
- **Token koruması** — `API_TOKEN` ile işlem uçları korunur (dışa açık dağıtımlarda zorunlu).
- **Likidite/slippage** — orderbook derinliği ve tahmini slippage girişte kontrol edilir.
- **İdempotent emir** — `create_order` sabit `clientOrderId` ile gönderilir; yanıt kaybolsa bile **çift emir oluşmaz**.
- **Acil flatten** — panelde "⛔ Tümünü kapat" / `POST /positions/close-all` ile tüm pozisyonlar tek tıkla kapatılır.
- **Ölü-adam anahtarı** — gerçek-zamanlı haber akışı (WS) `WS_STALE_ALERT_SEC` (vars. 600s) boyunca kopuk/sessiz kalırsa Telegram/Discord'dan **otomatik uyarı**, düzelince toparlama bildirimi (sessiz sinyal-kaybını önler).

## Canlı işleme geçmeden — kontrol listesi

Sırayla:

1. **Paper'da doğrula.** Motoru bir süre paper modda çalıştır; `/signals` arşivi birikince `news_backtest --walk` (veya panelde Walk-forward) **pozitif/tutarlı** karar verene kadar auto-trade'i açma. Edge kanıtlanmadan gerçek para riske atma.
2. **İlk canlı işlem minik.** `BINANCE_API_KEY`/`SECRET` (para çekme KAPALI + IP allowlist) ekledikten sonra `trade_usdt`'yi en düşükte tut, tek işlemle borsa entegrasyonunu (emir/teyit/SL-TP/kapanış) doğrula.
3. **Risk limitlerini ayarla.** `daily_loss_limit_usdt` (kill-switch), `max_total_exposure_usdt`, `max_per_coin_usdt`, `max_open_risk_usdt`, `reduce_after_losses` — hepsini hesabına göre gir.
4. **Erişimi kapat.** Sunucu dışa açıksa `API_TOKEN` ayarla; aksi halde mutasyon uçları korumasız.
5. **Tek örnek çalıştır.** Aynı anda iki motor = çift işlem (süreçler-arası kilit yok). Tek instance kuralına uy.
6. **İzle.** `/health` (WS bağlı mı, son mesaj yaşı) ve `/metrics` (rate-limit/retry) ile gerçek-zamanlı kaynağın ve Binance entegrasyonunun sağlığını gözle; sorun olursa **acil flatten** hazırda.

## Geliştirme

```bash
ruff check . && mypy && pytest        # Python: lint + tip + test
cd dashboard && npm run build         # frontend: tsc + vite build
```

CI (`.github/workflows/ci.yml`) ikisini de koşar. Mimari detaylar için `CLAUDE.md`.

## Diğer botlar (eski, pasif)

`bot.py` / `arb_bot.py` / `api.py` eski Polymarket işidir; korunuyor ama aktif değil. Detay: `CLAUDE.md`.
