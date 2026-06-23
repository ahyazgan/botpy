"""Kaynak sağlık durum-makinesi + fetch_all devre-dışı/toparlanma entegrasyonu."""

from __future__ import annotations

import pytest

import news_bot as nb
from sourcehealth import SourceHealth


# ── SourceHealth durum-makinesi ──────────────────────────────────────────
def test_healthy_by_default():
    sh = SourceHealth(fail_threshold=3)
    assert sh.is_disabled("rss") is False


def test_disables_after_threshold():
    sh = SourceHealth(fail_threshold=3, cooldown_sec=100)
    assert sh.record_failure("rss", now=0) is None        # 1
    assert sh.record_failure("rss", now=1) is None        # 2
    assert sh.record_failure("rss", now=2) == "disabled"  # 3 → geçiş
    assert sh.is_disabled("rss", now=3) is True


def test_disabled_transition_fires_once():
    sh = SourceHealth(fail_threshold=2, cooldown_sec=100)
    sh.record_failure("rss", now=0)
    assert sh.record_failure("rss", now=1) == "disabled"
    # Devre dışıyken yeni hata: cooldown re-arm ama YENİ "disabled" geçişi yok
    assert sh.record_failure("rss", now=2) is None


def test_cooldown_expiry_allows_retry():
    sh = SourceHealth(fail_threshold=1, cooldown_sec=100)
    sh.record_failure("rss", now=0)               # devre dışı, retry @100
    assert sh.is_disabled("rss", now=50) is True
    assert sh.is_disabled("rss", now=101) is False  # cooldown doldu → yeniden dene


def test_recovery_transition():
    sh = SourceHealth(fail_threshold=1, cooldown_sec=100)
    sh.record_failure("rss", now=0)
    assert sh.record_success("rss", now=101) == "recovered"
    assert sh.is_disabled("rss", now=102) is False
    # Toparlandıktan sonra tekrar başarı: geçiş yok
    assert sh.record_success("rss", now=103) is None


def test_success_resets_consecutive():
    sh = SourceHealth(fail_threshold=3)
    sh.record_failure("rss", now=0)
    sh.record_failure("rss", now=1)
    sh.record_success("rss", now=2)               # sayaç sıfırlandı
    assert sh.record_failure("rss", now=3) is None  # tekrar 1'den başlar
    assert sh.is_disabled("rss", now=4) is False


def test_snapshot_shape():
    sh = SourceHealth(fail_threshold=1, cooldown_sec=100)
    sh.record_failure("rss", "timeout", now=0)
    snap = sh.snapshot(now=10)
    assert snap["rss"]["disabled"] is True
    assert snap["rss"]["healthy"] is False
    assert snap["rss"]["last_error"] == "timeout"
    assert snap["rss"]["retry_in_sec"] == pytest.approx(90.0)


def test_sources_independent():
    sh = SourceHealth(fail_threshold=1, cooldown_sec=100)
    sh.record_failure("rss", now=0)
    assert sh.is_disabled("rss", now=1) is True
    assert sh.is_disabled("binance", now=1) is False  # diğer kaynak etkilenmez


# ── fetch_all entegrasyonu ───────────────────────────────────────────────
@pytest.fixture()
def fetch_env(monkeypatch):
    monkeypatch.setattr(nb, "_source_health",
                        nb.sourcehealth.SourceHealth(fail_threshold=2, cooldown_sec=100))
    monkeypatch.setattr(nb, "get_rss_feeds", lambda: {"GoodFeed": "u1", "BadFeed": "u2"})
    monkeypatch.setattr(nb, "fetch_binance_announcements", lambda s: [])
    notes: list[str] = []
    monkeypatch.setattr(nb, "notify_remote", lambda m: notes.append(m))
    return notes


def test_fetch_all_disables_failing_feed(fetch_env, monkeypatch):
    def fake_rss(name, url):
        if name == "BadFeed":
            raise RuntimeError("boom")
        return []
    monkeypatch.setattr(nb, "fetch_rss", fake_rss)
    sess = object()
    # 2 hata eşiği → ikinci turda BadFeed devre dışı + uyarı
    nb.fetch_all(sess)
    nb.fetch_all(sess)
    assert nb._source_health.is_disabled("BadFeed") is True
    assert nb._source_health.is_disabled("GoodFeed") is False
    assert any("DEVRE DIŞI" in m for m in fetch_env)


def test_fetch_all_skips_disabled(fetch_env, monkeypatch):
    calls: list[str] = []

    def fake_rss(name, url):
        calls.append(name)
        if name == "BadFeed":
            raise RuntimeError("boom")
        return []
    monkeypatch.setattr(nb, "fetch_rss", fake_rss)
    sess = object()
    nb.fetch_all(sess)
    nb.fetch_all(sess)   # BadFeed burada devre dışı olur
    calls.clear()
    nb.fetch_all(sess)   # 3. tur: BadFeed atlanmalı
    assert "BadFeed" not in calls
    assert "GoodFeed" in calls
