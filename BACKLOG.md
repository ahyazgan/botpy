# BACKLOG.md — Work Queue (botpy: kripto haber-trade radarı)

> CLAUDE.md §7: sıradaki işaretsiz öğe otomatik çekilir. Tamamlananlar SHA ile işaretlenir.

-----

## Now (üstten alta, ara vermeden)

- [x] Multi-tf görünürlüğü (news feed.de 1s hareketi + teyit detayı)  (cf68266)
  Done when: panel haber kartında `price_60m_pct` ve 15dk/1s uyum durumu görünür; "sadece teyitli" filtresi; tsc+build yeşil; commit.
- [x] Kalıcı kapanan-işlem defteri (SQLite, restart.a dayanıklı)  (339d22e)
  Done when: kapanan işlemler `storage.closed_news_trades` tablosuna yazılır (trade_state.json 500 sınırından bağımsız); `/trades/closed` arşivden de okuyabilir; testler; commit.
- [x] Ağ-yoğun uçlarda eşzamanlılık koruması (/backtest, /scorecard)  (8737f2a)
  Done when: aynı anda çalışan ağır istek tek seferde bir koşar (in-flight lock); ikinci istek 409/uyarı döner; test; commit.

-----

## Next (Now bitince)

- [x] TreeNews WS reconnect/backoff sağlamlaştırma + parse testi  (e643516)
  Done when: WS kopuşunda üstel backoff ile yeniden bağlanır; `parse_tree_message` birim testleri (borsa/twitter/site biçimleri); commit.
- [x] /metrics gözlemlenebilirlik (sayaçlar: taranan/uyarı/işlem/hata)  (e6fdeed)
  Done when: `GET /metrics` basit sayaç/gauge metni döner; test; commit.

-----

## Later (düşük öncelik)

- [ ] Canlı pozisyonları başlangıçta borsa ile mutabakat (Binance)
- [x] Yapılandırılabilir RSS kaynakları (ayarlardan)  (09ff6c6)

-----

## Done (son ~10)

- [x] Çoklu zaman dilimli teyit (15dk + 1s uyumu)  (ca7271f, PR #22)
- [x] Backtest sonuç kalıcılığı + karşılaştırma (/backtest/runs)  (bc817ba, PR #22)
- [x] Güvenlik başlıkları (CSP/HSTS/X-Frame/nosniff)  (7ae770b, PR #21)
- [x] Pozisyon bazında güç attribution (by_impact)  (f2ad4cc, PR #21)
- [x] Liveness probe /healthz  (fa556d1, PR #21)
- [x] Dış API retry + backoff (netutil.get_json)  (5e37a0b, PR #20)
- [x] Sinyal scorecard + dashboard (/scorecard)  (eb7e7db/f2d4c09, PR #20)
- [x] Oto-işlem dry-run önizleme (/auto-preview)  (d0b8ca9, PR #19)
- [x] Operatör README (runbook)  (6648b10, PR #18)
- [x] Günlük özet digest (/summary)  (d5b98be, PR #17)

-----

## Notes / blockers

- (yok)
