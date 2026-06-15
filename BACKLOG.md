# BACKLOG.md — Work Queue (botpy: kripto haber-trade radarı)

> CLAUDE.md §7: sıradaki işaretsiz öğe otomatik çekilir. Tamamlananlar SHA ile işaretlenir.
> NOT: Bu repo `ahyazgan/botpy` (crypto news-trade). Önceki futbol backlog'u yanlış
> projeye aitti; bu dosya botpy'ye uygun gerçek işlerle değiştirildi.

-----

## Now (üstten alta, ara vermeden)

- [x] Canlı sinyal scorecard (ham sinyal kalitesi)  (7a5e968)
  Done when: `/scorecard` arşiv sinyallerinin gerçekleşen yön isabetini (SL/TP'den bağımsız) kaynak/güç bazında döndürür; saf `signal_scorecard` + testler; ruff+mypy+pytest yeşil; commit.
- [x] Scorecard dashboard yüzeyi  (6fe4565)
  Done when: panelde katlanır "Sinyal kalitesi" tablosu (isabet oranı/ort. hareket, kaynak+güç kırılımı); tsc+build yeşil; commit.
- [x] Dış API retry + backoff (Binance/Anthropic)  (f9c3177)
  Done when: ortak `_http_get` retry sarmalayıcısı (üstel backoff, sınırlı deneme) fiyat/kline çağrılarına uygulanır; testler retry davranışını kapsar; commit.

-----

## Next (Now bitince)

- [x] Liveness/readiness ayrımı `/health` (hafif liveness + zengin readiness)  (7126997)
  Done when: `/healthz` (her zaman 200) + `/health` (kaynak/uptime); test; commit.
- [x] Pozisyon bazında güç attribution (impact pozisyonda saklanır → live by_impact)  (a954b66)
  Done when: place_trade impact saklar; get_performance `by_impact`; test; commit.
- [x] Güvenlik başlıkları (CSP/HSTS/X-Frame-Options/X-Content-Type-Options)  (323dae7)
  Done when: FastAPI middleware başlıkları ekler; test; commit.

-----

## Later (düşük öncelik)

- [x] Backtest sonuçlarını kalıcı kaydet + karşılaştırma  (c677689)
- [x] Çoklu zaman dilimli teyit (15dk + 1s uyumu)  (a144af7)

-----

## Done (son ~10)

- [x] Oto-işlem dry-run önizleme (/auto-preview + auto_decision)  (d0b8ca9, PR #19)
- [x] Operatör README (runbook)  (6648b10, PR #18)
- [x] Günlük özet digest (/summary + gün dönümü)  (d5b98be, PR #17)
- [x] Gelişmiş performans metrikleri (payoff/Sharpe)  (74bf03d, PR #16)
- [x] Backtest edge kırılımı (güç/yön/kaynak)  (b068d7e, PR #15)

-----

## Notes / blockers

- Pastelenen futbol backlog'u (La Liga/decisions/return_to_play) bu repoya ait değil — yok sayıldı.
