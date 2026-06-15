# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Backend (Python)

```bash
# Bağımlılıkları yükle
pip install -r requirements.txt

# ★ AKTİF PROJE: Kripto haber-trade radarı
# Haber motoru (RSS + Binance duyuruları tarar, güçlü haberde masaüstü bildirimi)
python news_bot.py
# Sadece konsol modu (API yok)
python news_bot.py --cli
# Motor + panel'i birlikte: çift tıkla haber-radar.bat
# Claude ile akıllı puanlama için: ANTHROPIC_API_KEY ortam değişkenini ayarla (yoksa kural-tabanlı)

# FastAPI sunucusunu başlat (varsayılan: http://127.0.0.1:8000)
python bot.py

# Sadece konsol log modu (API yok)
python bot.py --cli

# Özel host/port
python bot.py --host 0.0.0.0 --port 8080

# Arbitraj botu — sadece bildirim modu (varsayılan, .env GEREKMEZ)
# Fırsat bulununca Windows masaüstü bildirimi atar, trade'i kullanıcı elle yapar.
python arb_bot.py
# veya çift tıkla: arb-bildirim.bat

# Arbitraj botu — otomatik emir modu (.env gerekli)
python arb_bot.py --execute

# Eski CORS proxy (dashboard.html için)
uvicorn api:app --reload --port 8001
```

### Dashboard (React + TypeScript + Tailwind)

```bash
cd dashboard
npm install
npm run dev      # http://localhost:5173
npm run build
npm run preview
```

`frontend/` klasörü şu an default Vite scaffold'dur, aktif kullanımda değil.

## Mimari

**Aktif proje = kripto haber-trade radarı** (`news_bot.py` + `dashboard/`). `bot.py`/`arb_bot.py`/`api.py` eski Polymarket işidir; korunuyor ama aktif değil.

### `news_bot.py` — FastAPI Haber Motoru (AKTİF)

`bot.py` iskeletini (arka plan thread + in-memory cache + CORS + Pydantic) yeniden kullanır. **İki haber yolu:** (a) `_tree_ws_loop` — TreeNews WebSocket (`wss://news.treeofalpha.com/ws`, `parse_tree_message`) ile GERÇEK ZAMANLI haber (borsa duyuruları + Twitter + siteler); (b) `_background_loop` — RSS + Binance polling yedek. İkisi de ortak `process_items(session, candidates, allow_notify)` fonksiyonuna besler (dedupe → puanla → teyit → sakla → bildir/oto-işlem). WS'te backfill koruması (`TREE_BACKFILL_GUARD_SEC`) ilk saniyelerdeki geçmiş mesajları bildirmez.

`_background_loop` her `SCAN_INTERVAL_SEC` (20s):
1. **Kaynakları çek** (`fetch_all`): RSS feed'leri (`RSS_FEEDS`, feedparser) + Binance yeni listeleme duyuruları (catalogId=48). Biri patlarsa diğerleri devam eder.
2. **Tekrar engelle**: `_seen_ids` ile yeni haberleri ayıkla. İlk tarama bildirimsiz **tohumlama** (`_primed`) — spam önler.
3. **Puanla** (iki faz — gecikme düşürme): önce **her zaman `score_item`** (kural-tabanlı, anında: `COIN_PATTERNS`, `IMPACT_KEYWORDS`, Binance ticker çıkarımı) → haber hemen saklanır ve `USE_CLAUDE` açıkken kural-güçlüleri **anında bildirilir** (erken heads-up). Sonra `USE_CLAUDE` varsa `score_with_claude` (Claude `claude-haiku-4-5`, `messages.parse` + Pydantic `_ScoreBatch`, tek istekte) ile **rafine** edilir; Claude başarısızsa kural skoru geçerli kalır. Teyit/arşiv/oto-işlem **nihai skorda** çalışır (para yolu dikkatli); erken bildirilen haber tekrar bildirilmez. Her haber: `coins`, `impact` (1-10), `direction` (bullish/bearish/neutral), `reason`.
4. **Bildir** (`notify`): `impact >= ALERT_THRESHOLD` (7) olanlara winotify masaüstü bildirimi ("Habere git" butonuyla) **+ uzak bildirim** (`notify_remote` → `notify.py`'deki `Notifier`, Telegram/Discord). Uzak kanal env tanımlıysa (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/`DISCORD_WEBHOOK_URL`) otomatik etkin, yoksa sessiz; winotify kurulu olmasa da çalışır (bilgisayardan uzaktayken telefona sinyal). Oto-işlem **açılış** (`process_items`) ve **kapanış** (`_monitor_loop`, `monitor_positions` artık kapanan pozisyonları döndürür) olayları da uzak kanaldan bildirilir.

**Fiyat teyidi** (`confirm_with_price`): güçlü haberler için Binance public API'den 24s + **çoklu zaman dilimi** (15dk + ~1s) fiyat hareketi + hacim çekilir (`_fetch_symbol_stats`, tek `CONFIRM_INTERVAL`×`CONFIRM_LIMIT` klines isteği — varsayılan 15m×4; daha erken/gürültülü teyit için env ile `1m`×`15` yapılabilir). `confirmed` = kısa pencere haber yönünde **ve** tüm pencere ters yönde değil (fade önleme) **ve** likidite ≥ `MIN_VOLUME_USD`. "Zaten fiyatlanmış" (24s'te > `ALREADY_PRICED_PCT`) uyarısı verir; 15dk-1s çelişkisinde teyit etmez.

**Sinyal arşivi** (`_archive_signal` → `storage.add_signal`): güçlü haberler (canlı, tohumlama değil) `news_signals` tablosuna kalıcı yazılır (id ile dedupe, restart'a dayanıklı). `Store` lazy açılır (`get_store`, `BOTPY_DB` yolu); import'ta dosya yaratma yan etkisi yok. `news_backtest.py --db` bu arşivi motor çalışmadan okur. Sınırsız büyümeyi önlemek için `prune_signals` ile budanır (`MAX_ARCHIVE_SIGNALS`=5000; başlangıçta + her `ARCHIVE_PRUNE_EVERY` yeni sinyalde).

**Token koruması** (`require_token`, `API_TOKEN` env): tanımlıysa mutasyon uçları (`POST /trade`, `PATCH /settings`, `PATCH /news-settings`, `DELETE /positions`) `X-API-Token` başlığı ister; yoksa açık (yerel kullanım, geriye dönük uyumlu). Sunucu dışa açılırsa ayarlanmalı.

**Endpoint'ler:** Haber: `GET /news?limit=&min_impact=`, `/alerts`, `/signals?limit=&min_impact=` (kalıcı arşiv + kapsam), `/scorecard?hours=&min_impact=&limit=` (ham sinyal kalitesi — `signal_scorecard`: haber yönünün fiyatla uyumu, kaynak/güç kırılımı; SL/TP simüle etmez), `/backtest?sl=&tp=&fee=&usdt=&hours=&min_impact=&limit=&mode=&train_frac=` (arşiv üzerinde `news_backtest` fonksiyonlarını koşar — Binance klines indirir, senkron/threadpool; `mode`: `simple`/`smart` (akıllı çıkış: mevcut ayarları/preset'i `simulate_smart` ile arşivde simüle eder — breakeven/kısmi TP/trailing/time-stop)/`grid` (`grid_search`, en kârlı SL/TP)/`walk` (walk-forward)), `GET/PATCH /news-settings` (uyarı eşiği + uzak bildirim, store'da kalıcı), `/healthz` (hafif liveness, her zaman 200), `/health` (uptime/scorer/treenews/arşiv sayısı ile zenginleştirilmiş readiness), `/metrics` (Prometheus-uyumlu sayaç/gauge: uptime/alerts/trades/scan-errors/açık-pozisyon/arşiv + TreeNews WS bağlı-mı/son-mesaj-yaşı via `_ws_state` + rate-limit/retry sayaçları via `netutil.get_stats`). İşlem/risk: `GET/PATCH /settings`, `POST /settings/preset/{name}` (çıkış preset'i: `news` = haber-trade düzeni / `safe` = muhafazakâr — `trader.apply_preset`/`PRESETS`), `POST /trade`, `GET /positions`, `PATCH /positions/{id}` (canlı SL/TP), `DELETE /positions/{id}`, `POST /positions/close-all` (acil flatten — `trader.close_all`, detaylı rapor), `GET /performance` (kazanma/P&L/drawdown/profit-factor/payoff/Sharpe), `GET /tuning` (öğrenen beyin — öneri modu: `trader.suggest_tuning` kapanan işlemlerden güç-dilimi/kaynak-tier/kaynak beklentisi çıkarıp `auto_min_impact`/kaynak-kısma önerir; `_source_tier` tier eşlemesiyle; **otomatik uygulamaz**), `GET /risk` (maruziyet/limit/kill-switch), `GET /summary` (günlük işlem özeti), `GET /reconcile` (canlı pozisyonları borsayla karşılaştır — read-only, auto-close yok), `GET/PATCH /news-sources` (yapılandırılabilir RSS kaynakları, kalıcı), `GET /auto-preview` (oto-işlem dry-run önizleme), `GET /trades/closed` (işlem günlüğü — kalıcı `news_closed_trades` defterinden, arşiv boşsa in-memory'e düşer; her kapanışta `_persist_closed`, başlangıçta backfill) + `/trades/closed.csv` (CSV dışa aktarım). Gün dönümünde dünün özeti uzak kanaldan otomatik bildirilir (`_maybe_daily_digest`).

### `trader.py` — Binance İşlem Modülü (AKTİF, profesyonel)

CCXT ile Binance işlem. **paper** (varsayılan, simülasyon) + **canlı** (`.env`'de `BINANCE_API_KEY`/`BINANCE_SECRET`). Ayarlar `Settings` sınıfında, `/settings` (GET+PATCH) ile değişir, **`trade_state.json`'a kaydedilir** (restart'ı atlatır — `load_state` modül import'unda çağrılır).

- **Manuel:** `place_trade`. **Otomatik:** `maybe_auto_trade` → yan etkisiz `auto_decision` (güç ≥ `auto_min_impact` + `confirmed` + cooldown + limitler + spot-short-yok + chase/kaynak filtreleri + boyut) ile karar verir, uygunsa `place_trade`. **Tier-1 refleks giriş** (`tier1_skip_confirm_impact`>0): güç ≥ bu eşik olan "net" haberde (hack/ETF/büyük listeleme) teyit **beklenmez**, hareketin önünde girilir; eşiğin altı (Tier-2) yine `confirmed` bekler. `auto_decision` global `auto_trade` anahtarından bağımsızdır → `/auto-preview` (dry-run) kalibrasyon önizlemesi sunar. **Conviction sizing** (`size_by_impact`): açıksa oto-işlem boyutu güce göre ölçeklenir (`_size_multiplier`: güç 8→1.0x, 10→1.5x, 7→0.75x, [0.5x,1.5x]).
- **Otomatik çıkış:** `monitor_positions` (news_bot `_monitor_loop`, 8s) — SL/TP/trailing + **akıllı çıkış**: `time_stop_min` (edge sönünce kapat), `breakeven_pct` (+%X'te SL'i girişe çek), `partial_tp_pct`/`partial_tp_frac` (`_partial_close` ile scale-out). Kısmi+tam kapanışları döndürür.
- **Sinyal kalitesi / öğrenme:** `skip_already_priced_pct` (24s'te haber yönünde çok oynamışsa girme — chase önleme), `suppress_losing_sources`+`min_source_samples` (negatif beklentili haber kaynağını sustur, `source_stats`), `news_source` pozisyonda saklanır → `get_performance` `by_news_source` attribution.
- **Dinamik/portföy riski:** `reduce_after_losses` (kayıp serisinde boyutu yarıla, `_losing_streak`), `max_open_risk_usdt` (açık pozisyonların SL'de toplam riski, `_open_risk`/`_position_risk`, `_check_risk`'te uygulanır).
- **Risk limitleri:** günlük zarar freni (`daily_loss_limit_usdt`, `_daily` realized takibi), toplam (`max_total_exposure_usdt`) + coin (`max_per_coin_usdt`) maruziyet — `_check_risk` ile `place_trade`'de uygulanır.
- **Emir kalitesi:** `_estimate_fill` (orderbook `/depth`) ile slippage tahmini (`slippage_guard_pct`) + likidite kontrolü (`min_orderbook_usd`); `order_type` market/limit.
- **Performans:** `_closed` işlem günlüğü + `get_performance` (kazanma oranı, P&L, kaynak/coin/sebep kırılımı + `equity` kümülatif P&L eğrisi via `_equity_from` + `max_drawdown` (`_max_drawdown`) + `profit_factor` (`_profit_factor`)). `closed_trades` (işlem günlüğü, /trades/closed + CSV) ve `get_risk` (anlık maruziyet/limit/günlük zarar/kill-switch, /risk). Endpoint'ler: `/performance`, `/risk`, `/trades/closed[.csv]`.

Güvenli varsayılan: `paper_trading=True`, `auto_trade=False`, SL=3% TP=6%.

### `backtest.py` — Sinyal Backtest (CLI)

Sinyalleri iki kaynaktan alır: (a) çalışan motorun `/news` (RAM) ucu — varsayılan; (b) `--db botpy.db` ile **kalıcı SQLite arşivi** (motor çalışmasa da olur). `news_bot` güçlü sinyalleri arşive yazdığı için (`_archive_signal` → `storage.add_signal`, restart'a dayanıklı), günlerce biriken veriyle backtest yapılabilir. Her sinyal için Binance geçmiş 1dk klines indirip SL/TP çıkışını simüle eder (komisyon dahil). `--grid` ile en kârlı SL/TP kombinasyonunu arar (klines sinyal başına bir kez `prefetch` edilir). `--walk` ile **walk-forward doğrulama** (`walk_forward`): sinyalleri zamana göre böl, ilk %`train_frac`'te SL/TP optimize et (`_best_params`, `SL_GRID`×`TP_GRID`), son kısımda (out-of-sample) test et — `walkforward._verdict` ile zayıflama + karar raporlar (overfit'i ölçer). `published` (RFC822/ISO) veya `fetched_at` zamanı kullanılır, çok yeni sinyaller (<30dk) atlanır.

### `bot.py` — FastAPI Market Tarayıcı (eski Polymarket)

Ana sunucu. Bir background thread her `SCAN_INTERVAL_SEC` (30s) saniyede:
1. Gamma API'den tüm aktif marketleri sayfalandırarak çeker (`fetch_active_markets`)
2. Binance'dan BTC/USDT spot fiyatını çeker
3. `MIN_VOLUME_24HR` ($10K) altındaki marketleri filtreler ve sonuçları `_cache` dict'ine yazar

FastAPI endpoint'leri bu in-memory cache'i okur (thread-safe, `threading.Lock`). Paper trade pozisyonları da in-memory `_paper_trades` listesinde tutulur — yeniden başlatınca sıfırlanır.

**Endpoint'ler:**
- `GET /markets` — filtrelenmiş market listesi + metadata
- `GET /btc` — BTC/USDT spot
- `PATCH /settings` — `PAPER_MODE` aç/kapat
- `POST /trade` — paper trade aç
- `GET /trades` — açık pozisyonlar + P&L
- `DELETE /trades/{id}` — pozisyon kapat

**NO token fiyat mantığı:** Gamma API yalnızca YES token fiyatlarını döner. NO fiyatları şu formüllerle hesaplanır:
- `NO_bid = 1 - YES_ask`
- `NO_ask = 1 - YES_bid`

### `arb_bot.py` — Async Arbitraj Botu

Bağımsız bot. İki modu var:

- **Sadece bildirim (varsayılan, `python arb_bot.py`):** Fırsat bulununca `notify_opportunity` ile Windows masaüstü bildirimi (`winotify`) atar; kullanıcı trade'i elle yapar. CLOB API anahtarı / `.env` **gerekmez** — sadece public Gamma + CLOB orderbook kullanılır. Aynı fırsatın tekrar tekrar bildirim atmasını `NOTIFY_COOLDOWN` (300s, market+yön bazında) engeller.
- **Otomatik emir (`python arb_bot.py --execute`):** `execute_arb` ile YES+NO emirlerini `asyncio.gather` ile aynı anda gönderir (FOK). `.env`'deki CLOB kimlik bilgileri gerekir; `build_clob_client` ve `py_clob_client` importları yalnızca bu modda lazy yüklenir.

Çalışma akışı (her iki mod):
1. **Hızlı ön eleme** (`quick_screen`): Gamma API fiyatlarıyla arbitraj adaylarını filtrele (CLOB çağırmadan)
2. **CLOB doğrulaması** (`verify_opportunity`): Adaylar için gerçek orderbook'u paralel çek
3. **Aksiyon**: bildirim (`notify_opportunity`) **veya** otomatik emir (`execute_arb`)

Strateji: `YES_ask + NO_ask < (1 - MIN_PROFIT)` → iki tarafı da al; `YES_bid + NO_bid > (1 + MIN_PROFIT)` → iki tarafı da sat.

### `dashboard/` — React Panel (AKTİF — haber radarı)

`src/App.tsx` `news_bot.py`'ye bağlanır (15s polling, `/news` + `/settings` + `/positions` + `/performance` + `/signals`). Canlı haber akışı: güç rozeti (yöne göre renkli), coin etiketleri, kaynak, zaman, gerekçe; güç ≥ eşik olan haberler vurgulanır. Performans bölümünde **kümülatif P&L eğrisi** (`EquityChart`, bağımlılıksız inline SVG; `/performance`'ın `equity` alanı). **Risk & maruziyet** paneli (`/risk`: maruziyet/pozisyon/günlük-zarar metreleri + coin kırılımı + kill-switch rozeti), **işlem günlüğü** (`/trades/closed` tablo + CSV indirme `/trades/closed.csv`), footer'da **sağlık şeridi** (`/health`: uptime/puanlayıcı/kaynak). Filtreler: arama, min. güç slider'ı, "sadece güçlü uyarılar". Footer'da **arşiv kapsam göstergesi** (`/signals` span'ı: biriken sinyal sayısı + gün/saat aralığı). **Backtest paneli** (`/backtest`, talep üzerine — 15s polling'e dahil değil): SL/TP gir + mod seçici (Basit / Akıllı çıkış / Grid / Walk-forward) + "Çalıştır". Basit: kazanma oranı/TP-SL-timeout/P&L + **edge kırılımı** (`breakdown`: güç-dilimi/yön/kaynağa göre — auto_min_impact/eşik kalibrasyonu için); Akıllı çıkış: mevcut çıkış ayarlarını/preset'i arşivde simüle eder (kısmi/time-stop/breakeven-stop sayaçlarıyla — preset'i canlıdan önce veriyle doğrula); Grid: tüm SL/TP kombinasyonları P&L'e göre sıralı tablo (en kârlı vurgulu); Walk-forward: in/out-sample + karar + zayıflama. Her çalıştırma `storage.add_backtest_run` ile kaydedilir → "Geçmiş çalıştırmalar" karşılaştırma tablosu (`/backtest/runs`). `VITE_API_BASE` (varsayılan `http://127.0.0.1:8000`). Tailwind + koyu zinc tema (eski Polymarket panelinden devralındı).

### `api.py` — CORS Proxy (eski)

`dashboard.html` (standalone HTML dosyası) için yazılmış basit bir proxy. `dashboard/` React uygulamasının kullanımıyla artık gerekli değil.

## Ortam Değişkenleri

`arb_bot.py`'nin **sadece bildirim** modu `.env` gerektirmez. `--execute` (otomatik emir) modu için `.env` dosyası gereklidir (`.env.example`'a bakın):

```
PRIVATE_KEY=        # Polygon cüzdan private key
FUNDER_ADDRESS=     # USDC kaynağı adres
POLY_API_KEY=       # Polymarket CLOB API key
POLY_SECRET=        # Polymarket CLOB secret
POLY_PASSPHRASE=    # Polymarket CLOB passphrase
```

`bot.py` bu değişkenlere ihtiyaç duymaz — sadece public Gamma API kullanır.

`news_bot.py` opsiyonel env'ler: `ANTHROPIC_API_KEY` (Claude puanlama), `BINANCE_API_KEY`/`BINANCE_SECRET` (canlı işlem), `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/`DISCORD_WEBHOOK_URL` (uzak bildirim), `BOTPY_DB` (SQLite yolu), `API_TOKEN` (mutasyon uçlarını korur — dışa açılırsa ayarla).

## CI / Kalite

`.github/workflows/ci.yml` iki iş: **lint-and-test** (ruff + `mypy` [aktif modüller, `pyproject.toml` `[tool.mypy]`] + py_compile + pytest) ve **dashboard** (npm ci + `npm run build` = `tsc --noEmit && vite build`). Yerelde tümü: `ruff check . && mypy && pytest` ve `cd dashboard && npm run build`.

## Önemli Notlar

- `bot.py`'deki `PAPER_MODE=True` (varsayılan) gerçek emir göndermez; tüm işlemler simülasyondur.
- `arb_bot.py` her zaman gerçek işlem açar — `.env` olmadan başlamaz (`KeyError` fırlatır).
- Dashboard CORS izinleri `bot.py`'de `localhost:5173` ve `localhost:3000`'e açıktır.
- Tüm sayfalandırma `PAGE_LIMIT=500` ile offset bazlı yapılır; çok sayıda aktif market varsa birden fazla istek atılır.
