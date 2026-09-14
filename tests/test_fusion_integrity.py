"""Füzyon dürüstlüğü: çapraz-teyit gerçek olsun + Tier-1 refleks kapısı korunsun.

Regresyon zemini: füzyon 'aynı olay' derken yalnız coin+yön+pencereye bakıyordu.
RSS_FEEDS'te aynı wire'ı taşıyan onlarca medya var; aynı haberi 3 outlet geçince
impact +2 → 7 olan haber 9'a çıkar → news preset'inde tier1_skip_confirm_impact=9
ile FİYAT TEYİDİ BEKLEMEDEN pozisyon açılır. Yani sistemin en tehlikeli kapısı,
taklit edilmesi en kolay sinyalle (kopyalanan haber) açılabiliyordu.
"""

from __future__ import annotations


import news_bot as nb
import trader


def _item(iid, title, source, direction="bullish", coins=("FOO",), symbol=None, impact=7):
    it = nb.NewsItem(id=iid, source=source, title=title, url="http://x",
                     published=None, fetched_at=nb._now_iso())
    it.coins = list(coins)
    it.direction = direction
    it.impact = impact
    it.symbol = symbol
    return it


# ── Konu parmak izi (saf) ────────────────────────────────────────────────
def test_topic_key_drops_filler_keeps_numbers_and_events():
    k = nb._topic_key("The FOO protocol was hacked for $600M today", ["FOO"])
    assert "hacked" in k and "$600m" in k    # olay + büyüklük ayırt edici
    assert "the" not in k and "was" not in k and "today" not in k
    assert "foo" not in k                     # coin adı atılır (eşleşmede ayrı koşul)


def test_same_event_requires_overlap_and_fails_open():
    hack = nb._topic_key("FOO bridge exploited, $600M drained", ["FOO"])
    listing = nb._topic_key("Binance will list FOO perpetual futures", ["FOO"])
    assert nb._same_event(hack, hack) is True
    assert nb._same_event(hack, listing) is False   # aynı coin, BAŞKA olay
    assert nb._same_event(set(), hack) is True      # konu çıkmadı → eski davranış


def test_fuse_coin_matches_confirmed_and_unconfirmed():
    """BTCUSDT (teyitli) ile BTC (teyitsiz) aynı olayda eşleşmeli."""
    assert nb._fuse_coin(_item("a", "t", "s", symbol="FOOUSDT")) == "FOO"
    assert nb._fuse_coin(_item("b", "t", "s", coins=("FOO",))) == "FOO"


# ── Füzyon davranışı ─────────────────────────────────────────────────────
def test_different_events_on_same_coin_do_not_confirm():
    base = _item("1", "FOO bridge exploited, $600M drained", "treenews")
    other = _item("2", "Binance will list FOO perpetual futures", "rss:coindesk")
    f = nb._fuse_event(base, [other])
    assert f["source_count"] == 1 and f["impact_bonus"] == 0

def test_same_event_across_sources_still_confirms():
    base = _item("1", "FOO bridge exploited, $600M drained", "treenews")
    echo1 = _item("2", "FOO bridge exploited for $600M", "rss:coindesk")
    echo2 = _item("3", "Exploit drains $600M from FOO bridge", "rss:theblock")
    f = nb._fuse_event(base, [echo1, echo2])
    assert f["source_count"] == 3 and f["impact_bonus"] == 2   # meşru çapraz-teyit


def test_contested_event_gets_no_bonus():
    """Kaynaklar aynı olayda anlaşamıyorsa 'çok kaynak = daha emin' geçersizdir."""
    base = _item("1", "FOO bridge exploited, $600M drained", "treenews", direction="bearish")
    agree = _item("2", "FOO bridge exploited for $600M", "rss:coindesk", direction="bearish")
    deny = _item("3", "FOO team denies bridge exploit, $600M safe", "rss:theblock",
                 direction="bullish")
    f = nb._fuse_event(base, [agree, deny])
    assert f["contested"] >= 1
    assert f["impact_bonus"] == 0        # çelişki varken teyit bonusu yok


def test_apply_fusion_records_raw_impact(monkeypatch):
    monkeypatch.setattr(nb, "_news", [])
    base = _item("1", "FOO bridge exploited, $600M drained", "treenews", impact=7)
    echo1 = _item("2", "FOO bridge exploited for $600M", "rss:coindesk")
    echo2 = _item("3", "Exploit drains $600M from FOO bridge", "rss:theblock")
    monkeypatch.setattr(nb, "_news", [echo1, echo2])
    nb._apply_fusion([base])
    assert base.impact == 9                 # füzyon bonusu uygulandı (eşik/boyut için)
    assert base.impact_pre_fusion == 7      # ham skor korundu


# ── Tier-1 korkuluğu (para yolu) ─────────────────────────────────────────
def test_tier1_reflex_reads_raw_impact_not_fused(monkeypatch):
    monkeypatch.setattr(trader.S, "tier1_skip_confirm_impact", 9)
    monkeypatch.setattr(trader.S, "auto_require_confirm", True)
    monkeypatch.setattr(trader.S, "auto_min_impact", 7)

    fused = _item("1", "FOO bridge exploited", "treenews", impact=9)
    fused.impact_pre_fusion = 7      # ham 7, füzyonla 9 olmuş
    fused.symbol = "FOOUSDT"
    fused.confirmed = False
    d = trader.auto_decision(fused)
    assert d["would_trade"] is False and "teyidi yok" in d["reason"].lower()

    # Gerçekten 9 olan haber refleks yetkisini KORUR
    genuine = _item("2", "FOO bridge exploited", "treenews", impact=9)
    genuine.impact_pre_fusion = 9
    genuine.symbol = "FOOUSDT"
    genuine.confirmed = False
    assert trader.auto_decision(genuine)["would_trade"] is True


def test_raw_impact_falls_back_when_field_absent():
    """Füzyon kapalı / eski kayıt → mevcut impact geçerli (geriye uyum)."""
    it = _item("1", "FOO hacked", "treenews", impact=8)
    assert trader._raw_impact(it) == 8
    it.impact_pre_fusion = 5
    assert trader._raw_impact(it) == 5


def test_fusion_fields_persist_to_archive(tmp_path):
    from storage import Store
    s = Store(str(tmp_path / "fuse.db"))
    try:
        it = _item("1", "FOO bridge exploited", "treenews", impact=9)
        it.impact_pre_fusion = 7
        it.contested = 2
        it.source_count = 3
        s.add_signal(it.to_dict())
        row = s.list_signals()[0]
        assert row["impact_pre_fusion"] == 7 and row["contested"] == 2
    finally:
        s.close()


def test_cooldown_does_not_block_untraded_symbol_after_boot(monkeypatch):
    """Hiç işlem görmemiş sembol cooldown'da SAYILMAMALI.

    time.monotonic() Linux'ta boot'tan beri geçen süre; .get(symbol, 0.0) varsayılanı
    yeni boot edilmiş makinede (Docker 7/24) her sembolü ilk cooldown_sec boyunca
    cooldown'da gösteriyordu → bot sessizce hiç işlem açmıyordu.
    """
    monkeypatch.setattr(trader, "_last_trade", {})
    monkeypatch.setattr(trader, "_positions", [])
    monkeypatch.setattr(trader.S, "cooldown_sec", 1800)
    monkeypatch.setattr(trader.time, "monotonic", lambda: 12.0)   # boot'tan 12 sn sonra
    assert trader._can_auto_trade("FOOUSDT") is True

    # Gerçekten işlem görmüş sembol hâlâ cooldown'a takılır
    monkeypatch.setattr(trader, "_last_trade", {"FOOUSDT": 10.0})
    assert trader._can_auto_trade("FOOUSDT") is False
