"""Başlık↔gövde çelişki tespiti (clickbait): puanlamada mismatch → impact tavanı.

Claude puanlayıcı gövdeyi görür; başlık iddialı ama gövde belirsiz/söylenti ise
mismatch=true → impact _MISMATCH_IMPACT_CAP'e kıstırılır (şişirilmiş başlığa kanma).
"""

from __future__ import annotations

from types import SimpleNamespace

import news_bot as nb
from news_bot import NewsItem

_NOW_DT = nb.datetime(2026, 6, 14, 12, 0, 0, tzinfo=nb.timezone.utc)


def _item(title, body="", sid="x") -> NewsItem:
    return NewsItem(id=sid, source="TreeNews", title=title, url="u",
                    published="2026-06-14T12:00:00+00:00",
                    fetched_at="2026-06-14T12:00:00+00:00", body=body)


# ── _score_line: gövde prompt'a giriyor mu ──────────────────────────────────
def test_score_line_includes_body():
    it = _item("BTC ETF onaylandı", body="SEC kararı kesinleşti")
    line = nb._score_line(0, it, _NOW_DT, [it])
    assert "» gövde:" in line
    assert "SEC kararı" in line


def test_score_line_no_body():
    it = _item("BTC haberi", body="")
    line = nb._score_line(0, it, _NOW_DT, [it])
    assert "» gövde:" not in line


# ── _score_chunk: mismatch → impact tavanı ──────────────────────────────────
class _FakeResp:
    def __init__(self, results):
        self.parsed_output = SimpleNamespace(results=results)


def _fake_client(scores):
    """scores: index→_ItemScore eşlemesi döndüren sahte messages.parse."""
    results = [nb._ItemScore(**s) for s in scores]

    class _Msgs:
        def parse(self, **kwargs):
            return _FakeResp(results)

    return SimpleNamespace(messages=_Msgs())


def test_mismatch_caps_impact():
    it = _item("DEV HACK milyarlar çalındı!!!", body="küçük bir cüzdan, iddia, doğrulanmadı")
    client = _fake_client([{"index": 0, "coins": ["BTC"], "impact": 9,
                            "direction": "bearish", "reason": "hack", "mismatch": True}])
    nb._score_chunk(client, [it])
    assert it.mismatch is True
    assert it.impact == nb._MISMATCH_IMPACT_CAP   # 9 → 6 (clickbait tavanı)
    assert "çelişki" in it.reason


def test_no_mismatch_keeps_impact():
    it = _item("BTC ETF onaylandı", body="SEC kararı kesinleşti, işlem başladı")
    client = _fake_client([{"index": 0, "coins": ["BTC"], "impact": 9,
                            "direction": "bullish", "reason": "etf", "mismatch": False}])
    nb._score_chunk(client, [it])
    assert it.mismatch is False
    assert it.impact == 9   # gerçek haber → tam impact


def test_mismatch_below_cap_unchanged():
    # mismatch ama impact zaten ≤ tavan → değişmez
    it = _item("Belki bir ortaklık", body="söylenti")
    client = _fake_client([{"index": 0, "coins": [], "impact": 4,
                            "direction": "neutral", "reason": "söylenti", "mismatch": True}])
    nb._score_chunk(client, [it])
    assert it.impact == 4


def test_itemscore_mismatch_defaults_false():
    # geriye dönük: mismatch alanı gelmezse False (eski model çıktısı)
    s = nb._ItemScore(index=0, coins=[], impact=5, direction="neutral", reason="x")
    assert s.mismatch is False
