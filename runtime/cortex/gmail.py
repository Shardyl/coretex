"""Gmail intake — read Tabscanner's contact-form inquiries (read-only, keyless OAuth).

Authorised as the api@tabscanner.com mailbox (its own refresh token, separate from the Drive account).
Filters to the contact-form signature (subject 'New enquiry from … — tabscanner.com') and parses out the
lead's name, their real email (in the body), and the message — so a reply goes to the lead, not the site.
"""
from __future__ import annotations

import base64
import json
import re

import httpx

from . import config, db

_CLIENT = config.get("GOOGLE_OAUTH_CLIENT") or "/etc/cortex/google_oauth_client.json"


def connected() -> bool:
    return bool(db.setting_get("gmail_refresh_token"))


def _access_token() -> str:
    with open(_CLIENT) as f:
        c = (json.load(f).get("web") or {})
    rt = db.setting_get("gmail_refresh_token")
    if not rt:
        raise RuntimeError("Gmail not connected — authorise at /oauth/google/start?purpose=gmail")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _get(tok: str, path: str, params: dict | None = None) -> dict:
    r = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/" + path,
                  params=params or {}, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _decode(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "===").decode("utf-8", "ignore")


def _plain_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain":
        return _decode(payload.get("body", {}).get("data"))
    for p in payload.get("parts", []) or []:
        t = _plain_body(p)
        if t:
            return t
    return _decode(payload.get("body", {}).get("data"))


def _field(body: str, label: str) -> str | None:
    m = re.search(rf"^\s*{label}\s*:\s*(.+)$", body, re.I | re.M)
    return m.group(1).strip() if m else None


def _message_after(body: str) -> str:
    lines = body.splitlines()
    idx = 0
    for i, ln in enumerate(lines):
        if re.match(r"^\s*Email\s*:", ln, re.I):
            idx = i + 1
            break
    return ("\n".join(lines[idx:]).strip()) or body.strip()


def _parse(msg: dict) -> dict:
    h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
    subject = h.get("subject", "")
    body = _plain_body(msg.get("payload", {}))
    name = _field(body, "Name")
    if not name:
        m = re.search(r"New enquiry from (.+?)\s*[—-]", subject)
        name = m.group(1).strip() if m else ""
    return {"gmail_id": msg.get("id"), "subject": subject, "from": h.get("from", ""),
            "date": h.get("date", ""), "name": name, "email": _field(body, "Email"),
            "message": _message_after(body), "snippet": msg.get("snippet", "")}


def list_inquiries(days: int = 7, limit: int = 50) -> list[dict]:
    """Contact-form inquiries from the last `days` days, parsed."""
    tok = _access_token()
    q = f'subject:"New enquiry from" subject:"tabscanner.com" newer_than:{days}d'
    res = _get(tok, "messages", {"q": q, "maxResults": limit})
    out = []
    for m in res.get("messages", []):
        out.append(_parse(_get(tok, f"messages/{m['id']}", {"format": "full"})))
    return out
