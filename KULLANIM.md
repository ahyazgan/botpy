# 🚀 Kullanım Rehberi — Kripto Haber-Trade Radarı

Bu rehber, sistemi **sıfırdan kendi bilgisayarında veya sunucunda** çalıştırman için
adım adım yazıldı. Teknik bilgi gerektirmez — komutları sırayla kopyala-yapıştır.

> **Önemli:** Sistem varsayılan olarak **paper (kâğıt) modunda** çalışır — yani
> **gerçek para kullanmaz**, tüm işlemler simülasyondur. Gerçek paraya geçmek
> tamamen senin elinde ve en sonda anlatılıyor. Önce simülasyonla güven kazan.

---

## İçindekiler
1. [En hızlı başlangıç (5 dakika)](#1-en-hızlı-başlangıç-5-dakika)
2. [Paneli açma](#2-paneli-açma)
3. [Sistemi çalışırken görme (Test haberi)](#3-sistemi-çalışırken-görme-test-haberi)
4. [Telefonuna bildirim (Telegram)](#4-telefonuna-bildirim-telegram)
5. [Akıllı haber puanlaması (Claude)](#5-akıllı-haber-puanlaması-claude)
6. [Veri biriktirme — asıl önemli adım](#6-veri-biriktirme--asıl-önemli-adım)
7. [Gerçek paraya geçiş (en sonda, dikkatli)](#7-gerçek-paraya-geçiş-en-sonda-dikkatli)
8. [Sık karşılaşılan sorunlar](#8-sık-karşılaşılan-sorunlar)

---

## 1. En hızlı başlangıç (5 dakika)

**Gereken tek şey: Docker.** ([Docker Desktop indir](https://www.docker.com/products/docker-desktop/) — Windows/Mac için.)

Proje klasöründe bir terminal aç ve şunları çalıştır:

```bash
# 1) Ayar dosyasını oluştur (boş bile çalışır — paper modu + kural-tabanlı puanlama)
cp .env.example .env

# 2) Motoru başlat (ilk sefer imajı kurar, biraz sürebilir)
docker compose up -d --build
```

Bu kadar. Sistem artık `http://localhost:8000` adresinde **7/24** çalışıyor.

**Çalışıyor mu kontrol et:**
```bash
docker compose logs -f engine      # canlı log akışı (çıkmak için Ctrl+C)
```
Tarayıcıda `http://localhost:8000/healthz` aç — `{"status":"ok"}` görürsen hazır.

**Durdurmak için:** `docker compose down` &nbsp;|&nbsp; **Tekrar başlatmak:** `docker compose up -d`

> Verilerin (`botpy.db` arşivi + ayarların) `botpy-data` adlı kalıcı alanda tutulur —
> sistemi durdurup başlatsan da kaybolmaz.

---

## 2. Paneli açma

Motor çalışırken haberleri/işlemleri **görsel panelde** izlemek için:

```bash
cd dashboard
npm install          # ilk sefer (Node.js gerekir: https://nodejs.org)
npm run dev          # → http://localhost:5173
```

Tarayıcıda `http://localhost:5173` aç. Panel motora otomatik bağlanır
(motor `localhost:8000`'de olduğu için ek ayar gerekmez).

Panelde göreceklerin: canlı haber akışı, güç rozetleri, açık pozisyonlar,
performans grafiği, risk durumu, 📋 Durum kartı ("şimdi ne yapmalıyım").

---

## 3. Sistemi çalışırken görme (Test haberi)

Gerçek haber gelmesini beklemeden sistemin nasıl çalıştığını **hemen** görebilirsin.

Panelde **🧪 Test haberi** kartını bul, bir başlık yaz, **Çalıştır**'a bas:

```
Binance lists new token PEPE with massive volume
```

Sistem o haberi gerçekmiş gibi okuyup anında gösterir:
- Hangi coin · ne kadar güçlü (1-10) · yön (yükseliş/düşüş)
- Uyarı verir miydi? İşlem açar mıydı? Hangi yön, ne kadar?

Bu **tamamen güvenli** — gerçek emir açmaz, hiçbir kaydı etkilemez. Sadece
"bu haber gelse sistem ne yapardı?" sorusunu yanıtlar. Farklı başlıklar dene,
sistemin mantığını tanı.

---

## 4. Telefonuna bildirim (Telegram)

Bilgisayar başında değilken bile güçlü haberlerde **telefonuna bildirim** gelsin.

**Adım adım Telegram botu oluştur:**

1. Telegram'da **@BotFather**'a yaz → `/newbot` gönder → bot adı ver.
   Sana bir **token** verir (örn. `123456:ABC-DEF...`). Kopyala.
2. Telegram'da **@userinfobot**'a yaz → sana **chat id**'ni söyler (bir sayı). Kopyala.
3. `.env` dosyasını aç, şu iki satırı doldur:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=987654321
   ```
4. Sistemi yeniden başlat:
   ```bash
   docker compose up -d
   ```

**Test et:** İlk güçlü gerçek haber geldiğinde bildirim telefonuna düşmeli.
Hemen denemek istersen, motor çalışırken şu komutla bir test bildirimi yollayabilirsin:
```bash
curl -X POST http://localhost:8000/simulate \
  -H "Content-Type: application/json" \
  -d '{"title":"Test bildirimi BTC","notify":true}'
```

> Discord kullanıyorsan: `DISCORD_WEBHOOK_URL=` satırına webhook adresini yaz.
> İkisini de boş bırakırsan bildirim sessizce devre dışı kalır (sistem yine çalışır).

---

## 5. Akıllı haber puanlaması (Claude)

Sistem haberleri varsayılan olarak **kural-tabanlı** puanlar (anahtar kelimeler,
coin tanıma) — bu ücretsiz ve anında çalışır. Daha akıllı, nüanslı puanlama
istersen Claude'u devreye alabilirsin:

1. [console.anthropic.com](https://console.anthropic.com) adresinden bir API anahtarı al.
2. `.env` dosyasına ekle:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. `docker compose up -d` ile yeniden başlat.

Artık güçlü haberler Claude ile rafine edilir (başlık↔gövde çelişkisi tespiti,
daha isabetli yön/güç). Maliyeti panelde **💸 /cost** kartından izleyebilirsin
(genelde günlük birkaç sent).

> Anahtar yoksa sistem sorunsuz kural-tabanlı çalışmaya devam eder — zorunlu değil.

---

## 6. Veri biriktirme — asıl önemli adım

**Bu sistemin asıl gücü zamanla ortaya çıkar.** Öğrenen katmanlar (giriş beyni,
ayar önerisi, risk analizi, backtest) ancak **gerçek sinyal birikince** anlamlı
sonuç verir. İlk gün her şey "veri yetersiz" der — bu normaldir.

**Yapman gereken:** Sistemi paper modunda **2-4 hafta kesintisiz çalıştır.**
Gerçek haberler gelsin, sistem puanlasın, sanal işlem açıp kapatsın, arşive yazsın.

Bu süreçte panelde takip et:
- **📋 Durum kartı** (`/report`) — "şu an ne yapmalıyım?" sorusunun tek-bakış cevabı.
  Sana sırayla ne yapman gerektiğini söyler.
- **Hazırlık kokpiti** (`/readiness` + `/golive`) — strateji canlıya değer mi?
- **Backtest paneli** — biriken veriyle "bu ayarlar gerçekten para kazanıyor mu?"

Veri birikince Durum kartı "VERİ YETERSİZ"den "GELİŞİYOR"a, oradan "UMUT VERİCİ"ye
geçer. Acele etme — sistem sana hazır olduğunu kendisi söyleyecek.

> **Kısa yol:** Elinde geçmiş haber verisi (CSV/JSON) varsa
> `python import_history.py haberler.csv` ile içe aktarıp aylarca beklemeden
> backtest yapabilirsin. (Detay: CLAUDE.md → import_history.py)

---

## 7. Gerçek paraya geçiş (en sonda, dikkatli)

> ⚠️ **Buraya ancak adım 6'da Durum kartı "UMUT VERİCİ" dedikten sonra gel.**
> Önce simülasyonda kanıtlanmamış bir stratejiyle gerçek para riske atma.

1. **Binance API anahtarı oluştur** (Binance → Hesap → API Yönetimi):
   - ⚠️ **"Para Çekme (Withdrawal)" iznini KAPALI bırak!** Sadece "Spot/Futures
     İşlem" izni yeterli. Anahtar çalınsa bile paran çekilemez.
   - IP kısıtlaması eklemen de önerilir.
2. `.env` dosyasına ekle:
   ```
   BINANCE_API_KEY=...
   BINANCE_SECRET=...
   ```
3. **Ön-uçuş denetimi yap:** Panelde `/preflight` (veya tarayıcıda
   `http://localhost:8000/preflight?probe=true`) — sistem "kritik eksik" derse
   **geçme**, önce onları düzelt.
4. Hazırsa, **çok küçük miktarla** başla: panelden `trade_usdt` değerini düşük tut
   (örn. 10-20 USDT), `paper_trading`'i kapat, `auto_trade`'i aç.
5. İlk günler yakından izle. 🚫 Kara liste ile güvenmediğin coinleri yasakla,
   risk limitlerini (günlük zarar freni, drawdown kill-switch) ayarla.

Güvenli varsayılanlar zaten açık: SL %3, TP %6, drawdown koruması, devre kesici.

---

## 8. Sık karşılaşılan sorunlar

| Sorun | Çözüm |
|-------|-------|
| `docker compose` komutu bulunamadı | Docker Desktop kurulu ve **çalışıyor** mu? |
| Panel "bağlanamadı" diyor | Motor çalışıyor mu? `docker compose ps` ile kontrol et. Motor `8000`'de olmalı. |
| Haber gelmiyor | İnternet bağlantısı? Loglara bak: `docker compose logs -f engine`. İlk taramada "tohumlama" yapılır (bildirim atmaz) — normal. |
| Telegram bildirimi gelmiyor | Token + chat_id doğru mu? Bota Telegram'da en az bir kez `/start` yazdın mı? |
| "Veri yetersiz" yazıları | Normal — adım 6. Sistem çalıştıkça dolacak. |
| Panel uzaktan açılacak (sunucu) | `.env`'e `API_TOKEN=birsifre` ekle (mutasyon uçlarını korur) + panelde `VITE_API_BASE`'i sunucu adresine ayarla. |

---

**Daha derin teknik detay** için: [`CLAUDE.md`](./CLAUDE.md) — tüm modüller,
endpoint'ler ve mimari orada açıklanıyor.

İyi avlar 🎯
