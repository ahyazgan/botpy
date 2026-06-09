"""notify.Notifier ve arb_bot.format_opp testleri."""

from __future__ import annotations

import arb_bot as ab
from notify import Notifier


class _FakeResp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _Poster:
    def __init__(self, status_code: int = 200, raise_exc: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.status_code = status_code
        self.raise_exc = raise_exc

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if self.raise_exc:
            raise RuntimeError("network down")
        return _FakeResp(self.status_code)


def test_disabled_when_no_config():
    n = Notifier()
    assert n.enabled is False
    assert n.send("hi") is False


def test_telegram_send():
    p = _Poster()
    n = Notifier(telegram_token="TOK", telegram_chat_id="42", post_fn=p)
    assert n.telegram_enabled is True
    assert n.send("merhaba") is True
    assert len(p.calls) == 1
    url, payload = p.calls[0]
    assert "botTOK/sendMessage" in url
    assert payload == {"chat_id": "42", "text": "merhaba"}


def test_discord_send():
    p = _Poster()
    n = Notifier(discord_webhook="https://discord/wh", post_fn=p)
    assert n.send("selam") is True
    url, payload = p.calls[0]
    assert url == "https://discord/wh"
    assert payload == {"content": "selam"}


def test_both_channels():
    p = _Poster()
    n = Notifier(
        telegram_token="T", telegram_chat_id="1",
        discord_webhook="https://d/wh", post_fn=p,
    )
    assert n.send("x") is True
    assert len(p.calls) == 2


def test_send_swallows_exceptions():
    p = _Poster(raise_exc=True)
    n = Notifier(discord_webhook="https://d/wh", post_fn=p)
    assert n.send("x") is False  # patlamaz, False döner


def test_http_error_is_failure():
    p = _Poster(status_code=500)
    n = Notifier(discord_webhook="https://d/wh", post_fn=p)
    assert n.send("x") is False


def test_from_env():
    n = Notifier.from_env(env={
        "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "9",
        "DISCORD_WEBHOOK_URL": "https://d/wh",
    })
    assert n.telegram_enabled and n.discord_enabled


def test_format_opp():
    m = ab.Market(
        id="m1", question="Seçim sonucu ne olur?", yes_token_id="y",
        no_token_id="n", yes_bid=0.4, yes_ask=0.45, no_bid=0.4, no_ask=0.45,
        volume24h=1.0,
    )
    opp = ab.ArbOpportunity(m, "buy", 8.5, 0.45, 0.46)
    msg = ab.format_opp(opp)
    assert "ARB BUY" in msg
    assert "8.50%" in msg
    assert "0.450" in msg and "0.460" in msg
