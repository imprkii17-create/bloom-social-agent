"""Telegram Bot API client: send messages to the owner, read what they wrote to the bot.

The bot token is part of every request URL, so nothing here ever logs a URL, and errors are re-raised with the token
removed. Sending is always best-effort: a Telegram outage must never stop a post from going out.
"""

from __future__ import annotations

import contextlib
import html
import logging
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
MAX_LEN = 4096


class TelegramError(RuntimeError):
    pass


def esc(text: object) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return html.escape(str(text), quote=False)


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Telegram caps a message at 4096 characters: split on line breaks, never inside an HTML tag line."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single giant line (rare): hard-cut it
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 30) -> None:
        if not token or ":" not in token:
            raise TelegramError("TELEGRAM_BOT_TOKEN doesn't look like a bot token (it should contain a ':').")
        self._token = token
        self._http = httpx.Client(base_url=f"{API}/bot{token}", timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._http.post(f"/{method}", json=payload or {})
        except httpx.HTTPError as exc:
            raise TelegramError(self._redact(f"Telegram {method}: {type(exc).__name__}: {exc}")) from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"Telegram {method}: HTTP {resp.status_code}, not JSON") from None
        if not data.get("ok"):
            desc = self._redact(str(data.get("description") or f"HTTP {resp.status_code}"))
            raise TelegramError(f"Telegram {method}: {desc}")
        return data.get("result")

    def me(self) -> dict[str, Any]:
        return dict(self.call("getMe") or {})

    def updates(self, offset: int | None = None, *, timeout: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "limit": limit, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self.call("getUpdates", payload) or [])

    def acknowledge(self, last_update_id: int) -> None:
        """Tell Telegram everything up to `last_update_id` was handled, so it isn't delivered again."""
        self.call("getUpdates", {"offset": last_update_id + 1, "timeout": 0, "limit": 1, "allowed_updates": ["message"]})

    def send(self, chat_id: str | int, text: str, *, html_mode: bool = True, reply_to: int | None = None) -> None:
        for i, part in enumerate(split_message(text)):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": part,
                "link_preview_options": {"is_disabled": True},
            }
            if html_mode:
                payload["parse_mode"] = "HTML"
            if reply_to and i == 0:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            self.call("sendMessage", payload)

    def typing(self, chat_id: str | int) -> None:
        with contextlib.suppress(TelegramError):
            self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def send_to_owner(settings: Settings, text: str, *, transport: httpx.BaseTransport | None = None) -> bool:
    """Message the owner if the bot is set up. Never raises; returns whether it was sent."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    try:
        with TelegramClient(settings.telegram_bot_token, transport=transport) as tg:
            tg.send(settings.telegram_chat_id, text)
        return True
    except TelegramError as exc:
        log.warning("Telegram message not sent: %s", exc)
        return False
