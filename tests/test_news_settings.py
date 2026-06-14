"""news_bot çalışma zamanı ayarları (eşik + uzak bildirim aç/kapat)."""

from __future__ import annotations

import pytest

import news_bot as nb
from storage import Store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = Store(str(tmp_path / "set.db"))
    monkeypatch.setattr(nb, "_store", s)
    monkeypatch.setattr(nb, "_settings_loaded", False)
    # varsayılanlara dön (modül global'i testler arası sızmasın)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    yield s
    s.close()


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


def test_update_threshold_persists_and_clamps(store, monkeypatch):
    out = nb.update_news_settings({"alert_threshold": 9})
    assert out["alert_threshold"] == 9
    assert nb._status["alert_threshold"] == 9
    # üst sınır kırpılır
    assert nb.update_news_settings({"alert_threshold": 50})["alert_threshold"] == 10
    assert nb.update_news_settings({"alert_threshold": 0})["alert_threshold"] == 1
    # store'a yazıldı
    assert store.get_setting("news_alert_threshold") == "1"


def test_threshold_reloads_from_store(store, monkeypatch):
    nb.update_news_settings({"alert_threshold": 8})
    # yeniden yükleme simülasyonu
    monkeypatch.setattr(nb, "_settings_loaded", False)
    monkeypatch.setattr(nb, "_news_settings", {"alert_threshold": 7, "remote_notify": True})
    assert nb.get_news_settings()["alert_threshold"] == 8


def test_remote_notify_gate(store, monkeypatch):
    fake = _Notifier()
    monkeypatch.setattr(nb, "_notifier", fake)
    nb.update_news_settings({"remote_notify": False})
    nb.notify_remote("sessiz olmalı")
    assert fake.sent == []                      # kapalı → gönderilmez
    nb.update_news_settings({"remote_notify": True})
    nb.notify_remote("şimdi git")
    assert fake.sent == ["şimdi git"]


def test_get_settings_reports_channel_availability(store, monkeypatch):
    from notify import Notifier
    monkeypatch.setattr(nb, "_notifier", Notifier())     # env yok → enabled False
    assert nb.get_news_settings()["remote_channels_available"] is False
    monkeypatch.setattr(nb, "_notifier", Notifier(telegram_token="T", telegram_chat_id="1"))
    assert nb.get_news_settings()["remote_channels_available"] is True
