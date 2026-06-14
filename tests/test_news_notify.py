"""news_bot.py uzak bildirim (Telegram/Discord) bağlantısı testleri."""

from __future__ import annotations

import news_bot as nb
from news_bot import NewsItem


def _item(**kw) -> NewsItem:
    base = dict(
        id="abc123",
        source="TreeNews",
        title="Binance lists FOO — new spot listing",
        url="https://example.com/foo",
        published=None,
        fetched_at="2026-06-14T00:00:00+00:00",
    )
    base.update(kw)
    return NewsItem(**base)


class _Notifier:
    """news_bot._notifier yerine geçen, gönderilen metinleri toplayan sahte."""

    def __init__(self, *, raise_exc: bool = False):
        self.sent: list[str] = []
        self.raise_exc = raise_exc

    def send(self, text: str) -> bool:
        if self.raise_exc:
            raise RuntimeError("network down")
        self.sent.append(text)
        return True


# ── mesaj formatı ─────────────────────────────────────────────────────────
def test_fmt_news_msg_contains_key_fields():
    it = _item(
        coins=["FOO"], impact=8, direction="bullish",
        reason="Yeni listeleme genelde fiyatı yukarı iter",
        confirmed=True, price_note="Haber + fiyat uyumlu (15dk +1.2%)",
    )
    msg = nb._fmt_news_msg(it)
    assert "Güç 8/10" in msg
    assert "YÜKSELİŞ" in msg
    assert "FOO" in msg
    assert "TreeNews" in msg
    assert "✅ TEYİTLİ" in msg
    assert "https://example.com/foo" in msg


def test_fmt_news_msg_unconfirmed_no_coins():
    it = _item(coins=[], impact=7, direction="bearish", confirmed=False)
    msg = nb._fmt_news_msg(it)
    assert "Genel" in msg
    assert "⏳ teyit yok" in msg
    assert "DÜŞÜŞ" in msg


def test_fmt_trade_msg_open():
    pos = {
        "mode": "paper", "side": "long", "symbol": "FOOUSDT",
        "usdt": 100.0, "entry_price": 1.23, "sl_price": 1.19, "tp_price": 1.30,
    }
    msg = nb._fmt_trade_msg(pos, opened=True)
    assert "OTO İŞLEM AÇILDI" in msg
    assert "[PAPER]" in msg
    assert "LONG FOOUSDT" in msg
    assert "SL 1.19" in msg and "TP 1.3" in msg


def test_fmt_trade_msg_close_profit():
    pos = {
        "mode": "live", "side": "short", "symbol": "FOOUSDT",
        "close_price": 1.10, "pnl": 6.0, "pnl_pct": 6.0, "close_reason": "take-profit",
    }
    msg = nb._fmt_trade_msg(pos, opened=False)
    assert "POZİSYON KAPANDI" in msg
    assert "[LIVE]" in msg
    assert "take-profit" in msg
    assert "P&L +6.00 USDT" in msg
    assert "🟩" in msg


def test_fmt_trade_msg_close_loss_emoji():
    pos = {"mode": "paper", "side": "long", "symbol": "X", "close_price": 1,
           "pnl": -3.5, "pnl_pct": -3.5, "close_reason": "stop-loss"}
    assert "🟥" in nb._fmt_trade_msg(pos, opened=False)


# ── notify_remote bağlantısı ───────────────────────────────────────────────
def test_notify_remote_sends_via_module_notifier(monkeypatch):
    fake = _Notifier()
    monkeypatch.setattr(nb, "_notifier", fake)
    nb.notify_remote("merhaba")
    assert fake.sent == ["merhaba"]


def test_notify_remote_swallows_errors(monkeypatch):
    fake = _Notifier(raise_exc=True)
    monkeypatch.setattr(nb, "_notifier", fake)
    nb.notify_remote("patlasa bile akış bozulmaz")  # exception fırlatmamalı


def test_notify_pushes_remote_even_without_winotify(monkeypatch):
    """winotify kurulu olmasa da uzak kanal tetiklenmeli."""
    fake = _Notifier()
    monkeypatch.setattr(nb, "_notifier", fake)
    monkeypatch.setitem(__import__("sys").modules, "winotify", None)  # import → ImportError
    nb.notify(_item(coins=["FOO"], impact=9, direction="bullish"))
    assert len(fake.sent) == 1
    assert "Güç 9/10" in fake.sent[0]
