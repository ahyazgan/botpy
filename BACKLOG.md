# BACKLOG.md — Work Queue (botpy: kripto haber-trade radarı)

> CLAUDE.md §7: sıradaki işaretsiz öğe otomatik çekilir. Tamamlananlar SHA ile işaretlenir.
> Tema: kullanışlılık / kullanıcı deneyimi (işlevsel çekirdek tamam).

-----

## Now (üstten alta, ara vermeden)

- [x] Tarayıcı bildirimi + ses uyarısı (güçlü sinyal gelince)  (3286a7a)
  Done when: panel açıkken yeni güç ≥ eşik sinyalde Notification API bildirimi + kısa bip; aç/kapat toggle (localStorage); tekrar bildirim yok; tsc+build yeşil; commit.
- [x] Açık pozisyonda canlı SL/TP düzenleme  (da9ffd6)
  Done when: `PATCH /positions/{id}` sl/tp günceller (trader.update_position); panelde pozisyon satırında düzenleme; testler; tsc+build yeşil; commit.
- [ ] Backtest "en iyi paramları uygula"
  Done when: grid/walk sonucundaki en iyi SL/TP'yi tek tıkla `/settings`'e yazan buton; tsc+build yeşil; commit.

-----

## Next (Now bitince)

- [ ] Sinyal detay paneli (şeffaflık: gerekçe/coin/teyit kırılımı)
  Done when: haber kartına tıkla → detay (puanlayıcı/gerekçe/24s-15dk-1s/teyit); tsc+build yeşil; commit.
- [ ] Bağlantı/onboarding durumu (Telegram/Claude/Binance bağlı mı)
  Done when: footer/health'te env-kaynak rozetleri (scorer=claude/rule, remote açık, canlı anahtar); commit.

-----

## Later (düşük öncelik)

- [ ] Gerçek zamanlı SSE push (15s polling yerine)
- [ ] Mobil duyarlılık denetimi + drawer
- [ ] Koyu/açık tema

-----

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
