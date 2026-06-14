"""
Opsiyonel bildirimler: Telegram ve/veya Discord webhook.

Env ile yapılandırılır; hiçbiri ayarlı değilse sessizce devre dışı kalır.
Best-effort: gönderim hatası akışı bozmaz, yalnızca loglanır. HTTP çağrısı
enjekte edilebilir (post_fn) — bu sayede ağsız test edilebilir.

Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   → Telegram sendMessage
  DISCORD_WEBHOOK_URL                     → Discord webhook
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

TIMEOUT = 10

PostFn = Callable[[str, dict[str, Any]], Any]


def _default_post(url: str, payload: dict[str, Any]) -> Any:
    return requests.post(url, json=payload, timeout=TIMEOUT)


class Notifier:
    """Telegram/Discord bildirimcisi. Kanallar bağımsız; biri ya da ikisi açık."""

    def __init__(
        self,
        *,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
        discord_webhook: str | None = None,
        post_fn: PostFn | None = None,
    ) -> None:
        self.telegram_token = telegram_token or None
        self.telegram_chat_id = telegram_chat_id or None
        self.discord_webhook = discord_webhook or None
        self._post: PostFn = post_fn or _default_post

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Notifier:
        env = env if env is not None else dict(os.environ)
        return cls(
            telegram_token=env.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID"),
            discord_webhook=env.get("DISCORD_WEBHOOK_URL"),
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_webhook)

    @property
    def enabled(self) -> bool:
        return self.telegram_enabled or self.discord_enabled

    def send(self, text: str) -> bool:
        """Etkin kanalların hepsine gönder. En az biri başardıysa True."""
        if not self.enabled:
            return False
        ok = False
        if self.telegram_enabled:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            ok = self._safe_post(url, {"chat_id": self.telegram_chat_id, "text": text}) or ok
        if self.discord_webhook:
            ok = self._safe_post(self.discord_webhook, {"content": text}) or ok
        return ok

    def _safe_post(self, url: str, payload: dict[str, Any]) -> bool:
        try:
            resp = self._post(url, payload)
        except Exception:
            log.exception("Bildirim gönderilemedi")
            return False
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            log.warning("Bildirim reddedildi (HTTP %s)", status)
            return False
        return True
