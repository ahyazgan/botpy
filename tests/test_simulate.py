"""POST /simulate — keyfi başlığı gerçek puanlama+karar yolundan geçir, yan etkisiz."""

from __future__ import annotations

from fastapi.testclient import TestClient

import news_bot as nb


def _client() -> TestClient:
    return TestClient(nb.app)


def test_symbol_from_coins_skips_stables():
    assert nb._symbol_from_coins(["USDT", "BTC"]) == "BTCUSDT"
    assert nb._symbol_from_coins(["ETH/USDT"]) == "ETHUSDT"
    assert nb._symbol_from_coins(["USDC", "USD"]) is None
    assert nb._symbol_from_coins([]) is None


def test_simulate_scores_and_derives_symbol():
    d = _client().post("/simulate", json={
        "title": "Binance lists new token BTC with huge volume", "coins": ["BTC"]}).json()
    assert d["item"]["impact"] >= 1
    assert d["item"]["symbol"] == "BTCUSDT"
    assert "BTC" in d["item"]["coins"]
    assert "would_trade" in d["decision"]
    assert "alert_threshold" in d


def test_simulate_empty_title_400():
    assert _client().post("/simulate", json={"title": "   "}).status_code == 400


def test_simulate_is_side_effect_free():
    # Simülasyon sonrası ne arşive ne _news'e ne _seen'e bir şey eklenmemeli.
    before_news = len(nb._news)
    before_seen = len(nb._seen_ids)
    _client().post("/simulate", json={"title": "Hack exploit drains protocol XYZ", "coins": ["XYZ"]})
    assert len(nb._news) == before_news
    assert len(nb._seen_ids) == before_seen


def test_simulate_confirmed_flag_reaches_decision():
    # confirmed + rel_volume ile teyit senaryosu kurulabilir (ağsız demo)
    d = _client().post("/simulate", json={
        "title": "SEC approves spot ETH ETF — landmark decision", "coins": ["ETH"],
        "confirmed": True, "rel_volume": 3.0}).json()
    assert d["item"]["confirmed"] is True
    assert d["note"].startswith("Yan etkisiz")


def test_simulate_notify_flag(monkeypatch):
    sent = {}
    monkeypatch.setattr(nb, "notify_remote", lambda text: sent.setdefault("text", text))
    d = _client().post("/simulate", json={"title": "Test alarm BTC", "notify": True}).json()
    assert d["notified"] is True
    assert "TEST" in sent["text"]


def test_simulate_token_guard(monkeypatch):
    # API_TOKEN tanımlıysa /simulate korunur (mutasyon değil ama bildirim/maliyet yan-etkisi olabilir)
    monkeypatch.setattr(nb, "API_TOKEN", "secret")
    c = _client()
    assert c.post("/simulate", json={"title": "x BTC"}).status_code == 401
    assert c.post("/simulate", json={"title": "x BTC"},
                  headers={"X-API-Token": "secret"}).status_code == 200
