"""Çoklu-haber füzyonu: aynı olayı farklı kaynaklardan çapraz-doğrula → güven/impact artır.

Tek kaynak vs 3 kaynak teyit = farklı güven. Echo (aynı kaynak tekrarı) ve nötr yön
güven katmaz. Impact artışı cap'li (FUSE_MAX_IMPACT_BONUS, tavan 10).
"""

from __future__ import annotations

import news_bot as nb
from news_bot import NewsItem

_NOW = nb.datetime.now(nb.timezone.utc).isoformat()


def _item(sid, source, coin="BTC", direction="bullish", impact=7, symbol="BTCUSDT"):
    return NewsItem(id=sid, source=source, title=f"{coin} haberi", url=f"u/{sid}",
                    published=_NOW, fetched_at=_NOW, coins=[coin],
                    direction=direction, impact=impact, symbol=symbol)


# ── _fuse_event ──────────────────────────────────────────────────────────────
def test_fuse_single_source_no_bonus():
    it = _item("a", "CoinDesk")
    f = nb._fuse_event(it, [it])
    assert f["source_count"] == 1
    assert f["impact_bonus"] == 0


def test_fuse_two_sources_confirm():
    a = _item("a", "CoinDesk")
    b = _item("b", "Bloomberg")
    f = nb._fuse_event(a, [a, b])
    assert f["source_count"] == 2
    assert "Bloomberg" in f["confirming_sources"]
    assert f["impact_bonus"] == 1


def test_fuse_caps_bonus():
    a = _item("a", "CoinDesk")
    others = [_item(str(i), src) for i, src in enumerate(["Bloomberg", "TheBlock", "Reuters", "Decrypt"])]
    f = nb._fuse_event(a, [a, *others])
    assert f["source_count"] == 5
    assert f["impact_bonus"] == nb.FUSE_MAX_IMPACT_BONUS   # cap


def test_fuse_echo_same_source_ignored():
    a = _item("a", "CoinDesk")
    echo = _item("b", "CoinDesk")   # aynı kaynak tekrarı
    f = nb._fuse_event(a, [a, echo])
    assert f["source_count"] == 1   # echo güven katmaz
    assert f["impact_bonus"] == 0


def test_fuse_opposite_direction_not_counted():
    a = _item("a", "CoinDesk", direction="bullish")
    b = _item("b", "Bloomberg", direction="bearish")   # ters yön
    f = nb._fuse_event(a, [a, b])
    assert f["source_count"] == 1


def test_fuse_different_coin_not_counted():
    a = _item("a", "CoinDesk", coin="BTC", symbol="BTCUSDT")
    b = _item("b", "Bloomberg", coin="ETH", symbol="ETHUSDT")
    f = nb._fuse_event(a, [a, b])
    assert f["source_count"] == 1


def test_fuse_neutral_no_fusion():
    a = _item("a", "CoinDesk", direction="neutral")
    b = _item("b", "Bloomberg", direction="neutral")
    f = nb._fuse_event(a, [a, b])
    assert f["source_count"] == 1
    assert f["impact_bonus"] == 0


# ── _apply_fusion: impact artışı + alanlar ──────────────────────────────────
def test_apply_fusion_boosts_impact(monkeypatch):
    a = _item("a", "CoinDesk", impact=7)
    b = _item("b", "Bloomberg", impact=7)
    monkeypatch.setattr(nb, "_news", [a, b])
    nb._apply_fusion([a, b])
    assert a.impact == 8   # +1 (2 kaynak)
    assert a.source_count == 2
    assert "Bloomberg" in a.confirming_sources


def test_apply_fusion_respects_cap_10(monkeypatch):
    a = _item("a", "CoinDesk", impact=10)
    b = _item("b", "Bloomberg", impact=9)
    monkeypatch.setattr(nb, "_news", [a, b])
    nb._apply_fusion([a, b])
    assert a.impact == 10   # zaten tavan, artmaz


def test_apply_fusion_disabled(monkeypatch):
    monkeypatch.setattr(nb, "FUSE_NEWS", False)
    a = _item("a", "CoinDesk", impact=7)
    b = _item("b", "Bloomberg", impact=7)
    monkeypatch.setattr(nb, "_news", [a, b])
    nb._apply_fusion([a, b])
    assert a.impact == 7    # füzyon kapalı → değişmez
    assert a.source_count == 1


def test_newsitem_fusion_defaults():
    it = _item("a", "CoinDesk")
    assert it.source_count == 1
    assert it.confirming_sources == []
