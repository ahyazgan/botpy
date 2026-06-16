# BACKLOG.md — Work Queue (botpy: kripto haber-trade radarı)

> CLAUDE.md §7: sıradaki işaretsiz öğe otomatik çekilir. Tamamlananlar SHA ile işaretlenir.
> Tema: kullanışlılık / kullanıcı deneyimi (işlevsel çekirdek tamam).

-----

## Now (üstten alta, ara vermeden)

### Epic: Akıllı oto-trade güçlendirme (haber→otomatik işlem)

- [x] Faz 1 — Güvenlik kapıları (auto_decision)  (cc5be8c)
  Done when: feed-stale halt (`halt_trade_on_stale`) + latency kapısı (`max_news_age_sec`) +
  aynı-yön korelasyon limiti (`max_same_direction`); auto_decision/maybe_auto_trade context alır;
  /auto-preview yansıtır; testler; mypy+ruff+pytest yeşil; commit.
- [x] Faz 2 — Oto-kalibrasyon (POST /tuning/apply)  (daa8466)
  Done when: `trader.apply_tuning` öneriyi korkuluklarla uygular (auto_min_impact taban + kaynak
  susturma); endpoint; testler; yeşil; commit.
- [x] Faz 3 — Bearish/short (futures funding kapısı)  (7dffbc2)
  Done when: futures short zaten açık; `max_funding_rate_pct` ile funding'e ters pahalı yönde girme;
  `get_funding_rate`; testler; yeşil; commit.
- [x] Faz 4 — ATR volatilite SL/TP  (9a3840f)
  Done when: confirm'de `atr_pct` hesapla; `use_atr_exits`/`atr_sl_mult`/`atr_tp_mult` ile place_trade
  dinamik SL/TP; testler; yeşil; commit.
- [x] Panel kontrolleri — yeni oto-trade ayarları  (44142a1)
  Done when: güvenlik kapıları/ATR/funding toggle+slider + oto-kalibrasyon "uygula" butonu;
  tsc+vite build yeşil; commit.
- [x] Giriş beyni — girişte Claude kararlı son yargı  (122c6bd)
  Done when: entry_brain_decision (haiku, haber+fiyat+geçmiş+portföy → enter/conviction/veto);
  maybe_auto_trade beyin kancası (Tier-2'de, refleks atlanır, fail-safe); panel toggle;
  testler; ruff+mypy+pytest+build yeşil; commit.
- [x] Giriş beynini derinleştir (emsal + rubrik + çıkış + eskalasyon)  (f7f1a6d)
  Done when: precedent_stats emsal hafızası; çok-boyutlu rubrik alt-skorlar; sl_tightness/hold_minutes
  çıkış (place_trade sl_mult + pozisyon-bazlı time-stop); brain_escalate iki-kademeli (haiku→sonnet);
  pos["brain"] bildirimde; panel eskalasyon toggle; testler; hepsi yeşil; commit.
- [x] Beyin ileri-seviye (kalibrasyon + BTC rejimi + bekle + küme)  (17942fe)
  Done when: brain_scorecard + /brain-scorecard + panel şeridi + ctx geri besleme; _btc_regime piyasa
  rejimi; _cluster_context küme; wait_seconds bekle/izle erteleme (_brain_for_trade + recheck loop);
  testler; ruff+mypy+pytest+build yeşil; commit.
- [x] Beyin uzman-seviye (backtest + kendini-iyileştirme + mikroyapı + tam metin)  (ea4ebeb)
  Done when: /brain-backtest offline replay (beyin vs mekanik edge); brain_self_improve oto-veto+boyut;
  orderbook_imbalance mikroyapı; NewsItem.body tam metin; panel backtest butonu+self-improve toggle;
  testler; ruff+mypy+pytest+build yeşil; commit.
- [x] /auto-preview'a beyin verdikti (canlıdan önce gözlem)
  Done when: /auto-preview?brain=true entry_brain_decision verdiktini döner; panelde 🧠 Beyin önizleme
  butonu + tabloda gir/bekle/veto rozeti; testler; yeşil; commit.

- [x] Tarayıcı bildirimi + ses uyarısı (güçlü sinyal gelince)  (3286a7a)
  Done when: panel açıkken yeni güç ≥ eşik sinyalde Notification API bildirimi + kısa bip; aç/kapat toggle (localStorage); tekrar bildirim yok; tsc+build yeşil; commit.
- [x] Açık pozisyonda canlı SL/TP düzenleme  (da9ffd6)
  Done when: `PATCH /positions/{id}` sl/tp günceller (trader.update_position); panelde pozisyon satırında düzenleme; testler; tsc+build yeşil; commit.
- [x] Backtest "en iyi paramları uygula"  (8aedc16)
  Done when: grid/walk sonucundaki en iyi SL/TP'yi tek tıkla `/settings`'e yazan buton; tsc+build yeşil; commit.

-----

## Next (Now bitince)

- [x] Sinyal detay paneli (şeffaflık: gerekçe/coin/teyit kırılımı)  (fd31844)
  Done when: haber kartına tıkla → detay (puanlayıcı/gerekçe/24s-15dk-1s/teyit); tsc+build yeşil; commit.
- [x] Bağlantı/onboarding durumu (Telegram/Claude/Binance bağlı mı)  (d90ce71)
  Done when: footer/health'te env-kaynak rozetleri (scorer=claude/rule, remote açık, canlı anahtar); commit.

-----

## Later (düşük öncelik)

- [x] Gerçek zamanlı SSE push (15s polling yerine)  (dc23ea9)
- [x] Mobil duyarlılık denetimi + drawer  (11d54e5)
- [x] Koyu/açık tema  (6ffebf3)

-----

- [x] Hız: kural-önce/Claude-sonra puanlama (anında heads-up bildirim)  (PR pending)

## Done (son ~10)

- [x] Canlı pozisyon mutabakatı (/reconcile)  (PR #25)
- [x] Yapılandırılabilir RSS kaynakları (/news-sources)  (PR #25)
- [x] /metrics gözlemlenebilirlik  (PR #24)
- [x] TreeNews WS üstel backoff + parse testi  (PR #24)
- [x] Ağ-yoğun uçlarda eşzamanlılık koruması (409)  (PR #23)
- [x] Kalıcı kapanan-işlem defteri (SQLite)  (PR #23)
- [x] Multi-tf görünürlük + "sadece teyitli" filtre  (PR #23)
- [x] Oto-işlem dry-run önizleme (/auto-preview)  (PR #19)
- [x] Operatör README  (PR #18)
- [x] Günlük özet digest (/summary)  (PR #17)

-----

## Notes / blockers

- (yok)
