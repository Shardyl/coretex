"""Telegram — the approval rail. Send messages with inline buttons, poll updates.

Plain text (no parse_mode) so arbitrary draft content never breaks formatting.
"""
from __future__ import annotations

import httpx

from .. import config


def _base() -> str:
    return f"https://api.telegram.org/bot{config.require('TELEGRAM_BOT_TOKEN')}"


def _chat_id() -> str:
    return config.require("TELEGRAM_CHAT_ID")


def _call(method: str, payload: dict | None = None) -> dict:
    r = httpx.post(f"{_base()}/{method}", json=payload or {}, timeout=70)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {data}")
    return data["result"]


def send(text: str, buttons: list[list[dict]] | None = None, chat_id: str | None = None) -> dict:
    payload: dict = {"chat_id": chat_id or _chat_id(), "text": text, "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return _call("sendMessage", payload)


def edit(message_id: int, text: str, buttons: list[list[dict]] | None = None,
         chat_id: str | None = None) -> dict:
    payload: dict = {"chat_id": chat_id or _chat_id(), "message_id": message_id, "text": text,
                     "disable_web_page_preview": True}
    payload["reply_markup"] = {"inline_keyboard": buttons or []}
    return _call("editMessageText", payload)


def answer_callback(callback_id: str, text: str = "") -> None:
    _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def get_updates(offset: int | None = None, timeout: int = 25) -> list[dict]:
    payload: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return _call("getUpdates", payload)


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}
