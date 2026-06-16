"""process_items boru hattı testleri: dedupe → puanla → teyit → sakla → bildir.

Ağsız: scorer ve confirm_with_price monkeypatch'lenir, bildirim no-op yapılır.
"""

from __future__ import annotations

import pytest

import news_bot as nb
from news_bot import NewsItem
from storage import Store

_NOW = "2026-06-14T12:00:00+00:00"


def _item(sid, title="Foo gets listed", source="TreeNews") -> NewsItem:
    return NewsItem(id=sid, source=source, title=title, url="u",
                    published=_NOW, fetched_at=_NOW)


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    # boru hattı global durumunu izole et
    monkeypatch.setattr(nb, "_seen_ids", set())
    monkeypatch.setattr(nb, "_news", [])
    monkeypatch.setattr(nb, "USE_CLAUDE", False)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": False})
    monkeypatch.setattr(nb, "_settings_loaded", True)   # store'dan yükleme yapma

    store = Store(str(tmp_path / "p.db"))
    monkeypatch.setattr(nb, "_store", store)

    # zaman/yaş filtresini test anına sabitle (published _NOW çok eski olabilir)
    monkeypatch.setattr(nb, "_too_old", lambda it: False)

    # ağ/yan etki olmadan: skor id'ye göre, teyit ve bildirim no-op, oto-işlem yok
    scores: dict[str, int] = {}

    def fake_score(it):
        it.impact = scores.get(it.id, 3)
        it.direction = "bullish"
        it.symbol = "FOOUSDT"

    monkeypatch.setattr(nb, "score_item", fake_score)
    monkeypatch.setattr(nb, "confirm_with_price", lambda session, it: None)
    monkeypatch.setattr(nb, "notify", lambda it: None)
    monkeypatch.setattr(nb.trader, "maybe_auto_trade", lambda it, **kw: None)

    yield scores, store
    store.close()


def test_new_and_alert_counts(pipeline):
    scores, _ = pipeline
    scores.update({"a": 8, "b": 4})
    n_new, n_alert = nb.process_items(None, [_item("a"), _item("b")], allow_notify=True)
    assert n_new == 2 and n_alert == 1          # sadece "a" eşik üstü
    assert [n.id for n in nb._news] == ["b", "a"] or [n.id for n in nb._news] == ["a", "b"]
    assert len(nb._news) == 2


def test_dedupe_across_calls(pipeline):
    scores, _ = pipeline
    scores["a"] = 8
    assert nb.process_items(None, [_item("a")], allow_notify=True) == (1, 1)
    # aynı id tekrar → yeni yok
    assert nb.process_items(None, [_item("a")], allow_notify=True) == (0, 0)
    assert len(nb._news) == 1


def test_noise_filtered_but_marked_seen(pipeline):
    scores, _ = pipeline
    scores["a"] = 9
    noise = _item("n1", title="Binance Will Launch FOO Perpetual Contract", source="Binance")
    n_new, n_alert = nb.process_items(None, [noise, _item("a")], allow_notify=True)
    assert n_new == 1 and n_alert == 1          # sadece "a" gerçek haber
    assert "n1" in nb._seen_ids                 # gürültü de görüldü olarak işaretli
    assert [n.id for n in nb._news] == ["a"]    # gürültü saklanmadı


def test_archive_only_when_notify(pipeline):
    scores, store = pipeline
    scores["a"] = 8
    # tohumlama (allow_notify=False) → arşivlenmez
    nb.process_items(None, [_item("a")], allow_notify=False)
    assert store.signal_span()["count"] == 0
    # canlı (allow_notify=True) → arşivlenir
    scores["b"] = 8
    nb.process_items(None, [_item("b")], allow_notify=True)
    assert store.signal_span()["count"] == 1


def test_runtime_threshold_respected(pipeline):
    scores, _ = pipeline
    scores["a"] = 6
    nb._news_settings["alert_threshold"] = 5     # eşiği düşür
    assert nb.process_items(None, [_item("a")], allow_notify=True) == (1, 1)


def test_empty_candidates(pipeline):
    assert nb.process_items(None, [], allow_notify=True) == (0, 0)


# ── İki-faz: kural-önce (erken bildir) → Claude-sonra (nihai skor) ──────────
def test_two_phase_early_notify_and_claude_promote(pipeline, monkeypatch):
    scores, store = pipeline
    monkeypatch.setattr(nb, "USE_CLAUDE", True)
    notified: list[str] = []
    traded: list[str] = []
    monkeypatch.setattr(nb, "notify", lambda it: notified.append(it.id))
    monkeypatch.setattr(nb.trader, "maybe_auto_trade", lambda it, **kw: (traded.append(it.id), None)[1])
    scores.update({"a": 8, "b": 3})   # kural: a güçlü (erken bildir), b zayıf

    def claude(items):
        cs = {"a": 9, "b": 8}          # Claude: a güçlü kalır, b'yi güçlüye yükseltir
        for it in items:
            it.impact = cs[it.id]
            it.scorer = "claude"
    monkeypatch.setattr(nb, "score_with_claude", claude)

    n_new, n_alert = nb.process_items(None, [_item("a"), _item("b")], allow_notify=True)
    assert n_new == 2 and n_alert == 2
    assert sorted(notified) == ["a", "b"]   # a erken, b Claude-sonrası — her biri 1 kez (a çift değil)
    assert sorted(traded) == ["a", "b"]     # oto-işlem nihai skorda her ikisi


def test_two_phase_claude_downgrade_heads_up_only(pipeline, monkeypatch):
    scores, store = pipeline
    monkeypatch.setattr(nb, "USE_CLAUDE", True)
    notified: list[str] = []
    traded: list[str] = []
    monkeypatch.setattr(nb, "notify", lambda it: notified.append(it.id))
    monkeypatch.setattr(nb.trader, "maybe_auto_trade", lambda it, **kw: (traded.append(it.id), None)[1])
    scores.update({"a": 9})            # kural güçlü → erken heads-up

    def claude(items):
        for it in items:
            it.impact = 2              # Claude düşürür
            it.scorer = "claude"
    monkeypatch.setattr(nb, "score_with_claude", claude)

    n_new, n_alert = nb.process_items(None, [_item("a")], allow_notify=True)
    assert n_alert == 0                # nihai skor zayıf
    assert notified == ["a"]           # erken heads-up gitti
    assert traded == []                # ama işlem yok (para yolu nihai skorda)
    assert store.signal_span()["count"] == 0   # arşivlenmedi


def test_two_phase_claude_failure_keeps_rule(pipeline, monkeypatch):
    scores, store = pipeline
    monkeypatch.setattr(nb, "USE_CLAUDE", True)
    scores.update({"a": 8})

    def boom(items):
        raise RuntimeError("claude down")
    monkeypatch.setattr(nb, "score_with_claude", boom)

    n_new, n_alert = nb.process_items(None, [_item("a")], allow_notify=True)
    assert n_alert == 1                # Claude patladı → kural skoru geçerli
