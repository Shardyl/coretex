"""Mailgun send (US region). Auth = HTTP Basic api:MAILGUN_API_KEY from /etc/cortex.

Each company sends from its OWN verified newsletter domain (see newsletter.SEND_DOMAINS). Tracking on;
recipient-variables make each send an individual message (recipients never see each other) and enable
per-recipient %unsubscribe_url% + %recipient.first_name% personalisation.
"""
from __future__ import annotations

import json

import httpx

from . import config

BASE = "https://api.mailgun.net/v3"


def send(domain: str, sender: str, to: list[str], subject: str, html: str, text: str, *,
         inline: list[tuple[str, bytes]] | None = None, recipient_vars: dict | None = None,
         reply_to: str | None = None, tag: str | None = None) -> dict:
    """Send one Mailgun message to up to ~1000 recipients. `inline` = [(cid_filename, bytes)]."""
    key = config.require("MAILGUN_API_KEY")
    # NOTE: with multipart (files present) httpx needs `data` as a Mapping; a list value repeats the key.
    data: dict = {
        "from": sender, "to": list(to), "subject": subject, "html": html, "text": text,
        "o:tracking": "yes", "o:tracking-opens": "yes", "o:tracking-clicks": "yes",
    }
    if recipient_vars:
        data["recipient-variables"] = json.dumps(recipient_vars)
    if reply_to:
        data["h:Reply-To"] = reply_to
    if tag:
        data["o:tag"] = tag
    files = [("inline", (cid, content, "image/jpeg")) for cid, content in (inline or [])]
    r = httpx.post(f"{BASE}/{domain}/messages", auth=("api", key),
                   data=data, files=files or None, timeout=90)
    r.raise_for_status()
    return r.json()


def suppressions(domain: str, kind: str) -> list[str]:
    """List a domain's suppression addresses. kind = 'unsubscribes' | 'complaints' | 'bounces'."""
    key = config.require("MAILGUN_API_KEY")
    r = httpx.get(f"{BASE}/{domain}/{kind}", auth=("api", key), params={"limit": 1000}, timeout=30)
    r.raise_for_status()
    return [it["address"] for it in r.json().get("items", []) if it.get("address")]
