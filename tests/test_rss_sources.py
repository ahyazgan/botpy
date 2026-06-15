"""Yapılandırılabilir RSS kaynakları: get/set_rss_feeds + /news-sources."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import news_bot as nb
from storage import Store


@pytest.fixture()
def env(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "r.db"))
    monkeypatch.setattr(nb, "_store", s)
    monkeypatch.setattr(nb, "_rss_feeds", None)
    monkeypatch.setattr(nb, "API_TOKEN", None)
    yield s
    s.close()


def test_default_when_unset(env):
    assert nb.get_rss_feeds() == nb.RSS_FEEDS


def test_set_and_persist(env):
    out = nb.set_rss_feeds({"Foo": "https://foo/rss", "Bad": "ftp://nope", "Empty": ""})
    assert out == {"Foo": "https://foo/rss"}        # yalnızca http(s)
    # yeniden yükleme → store'dan okur
    nb._rss_feeds = None
    assert nb.get_rss_feeds() == {"Foo": "https://foo/rss"}


def test_fetch_all_uses_effective_feeds(env, monkeypatch):
    nb.set_rss_feeds({"OnlyOne": "https://one/rss"})
    seen = []
    monkeypatch.setattr(nb, "fetch_rss", lambda name, url: seen.append((name, url)) or [])
    monkeypatch.setattr(nb, "fetch_binance_announcements", lambda s: [])
    nb.fetch_all(None)
    assert seen == [("OnlyOne", "https://one/rss")]


def test_endpoints_roundtrip(env):
    c = TestClient(nb.app)
    assert "rss_feeds" in c.get("/news-sources").json()
    r = c.patch("/news-sources", json={"feeds": {"X": "https://x/rss"}})
    assert r.status_code == 200 and r.json()["rss_feeds"] == {"X": "https://x/rss"}
    assert c.get("/news-sources").json()["rss_feeds"] == {"X": "https://x/rss"}


def test_patch_requires_token_when_set(env, monkeypatch):
    monkeypatch.setattr(nb, "API_TOKEN", "secret")
    c = TestClient(nb.app)
    assert c.patch("/news-sources", json={"feeds": {"X": "https://x/rss"}}).status_code == 401
    assert c.get("/news-sources").status_code == 200      # okuma açık
