"""Telegram — the approval rail. Send messages with inline buttons, poll updates.

Plain text (no parse_mode) so arbitrary draft content never breaks formatting.
"""
from __future__ import annotations

import time

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


# Telegram is the MIRROR, never the flow: a dead/ratelimited/stale-message Telegram must not fail the
# work it reflects (a 400 on editing a >48h-old card was 500-ing Inbox skips; a 429 killed the engine).
def send(text: str, buttons: list[list[dict]] | None = None, chat_id: str | None = None) -> dict:
    payload: dict = {"chat_id": chat_id or _chat_id(), "text": text, "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        return _call("sendMessage", payload)
    except Exception as e:   # noqa: BLE001
        print(f"telegram send failed (ignored): {e}")
        return {"message_id": None}


def edit(message_id: int, text: str, buttons: list[list[dict]] | None = None,
         chat_id: str | None = None) -> dict:
    payload: dict = {"chat_id": chat_id or _chat_id(), "message_id": message_id, "text": text,
                     "disable_web_page_preview": True}
    payload["reply_markup"] = {"inline_keyboard": buttons or []}
    try:
        return _call("editMessageText", payload)
    except Exception as e:   # noqa: BLE001
        print(f"telegram edit of message {message_id} failed (ignored): {e}")
        return {}


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except Exception as e:   # noqa: BLE001
        print(f"telegram answer_callback failed (ignored): {e}")


def get_updates(offset: int | None = None, timeout: int = 25) -> list[dict]:
    payload: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    try:
        return _call("getUpdates", payload)
    except Exception as e:   # noqa: BLE001
        print(f"telegram get_updates failed, backing off 5s: {e}")
        time.sleep(5)   # keeps a hard 429 from turning the poll loop into a hammer
        return []


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}
