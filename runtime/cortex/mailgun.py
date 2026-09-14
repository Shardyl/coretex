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
    if "%unsubscribe_url%" in (html or "") or "%unsubscribe_url%" in (text or ""):
        ensure_unsubscribe_tracking(domain)   # never let the token go out as literal text
        data["h:List-Unsubscribe"] = "<%unsubscribe_url%>"                 # one-click unsubscribe (Gmail/Yahoo bulk rules)
        data["h:List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    files = [("inline", (cid, content, "image/jpeg")) for cid, content in (inline or [])]
    r = httpx.post(f"{BASE}/{domain}/messages", auth=("api", key),
                   data=data, files=files or None, timeout=90)
    r.raise_for_status()
    return r.json()


def events(domain: str, event: str, begin: int, *, tag: str | None = None, end: int | None = None,
           limit: int = 300, pages: int = 20) -> list[dict]:
    """Mailgun event log for a domain: all `event` items since `begin` (unix ts), following pagination.
    Used for per-send stats (accepted/delivered/failed/opened/clicked/unsubscribed/complained)."""
    key = config.require("MAILGUN_API_KEY")
    params: dict = {"event": event, "begin": begin, "ascending": "yes", "limit": limit}
    if end:
        params["end"] = end
    if tag:
        params["tags"] = tag
    url = f"{BASE}/{domain}/events"
    out: list[dict] = []
    for _ in range(pages):
        r = httpx.get(url, auth=("api", key), params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        items = d.get("items") or []
        out += items
        nxt = (d.get("paging") or {}).get("next")
        if not items or not nxt or len(items) < limit:
            break
        url, params = nxt, {}
    return out


_UNSUB_OK: dict[str, float] = {}


def unsubscribe_tracking_on(domain: str, max_age: float = 600.0) -> bool:
    """Is Mailgun's unsubscribe tracking active for this domain? Without it %unsubscribe_url% is sent as
    LITERAL TEXT (the HBMSU issue went to 1,900 people with a dead unsubscribe, 13 Sep 2026). Cached 10 min."""
    import time as _t
    now = _t.time()
    if _UNSUB_OK.get(domain, 0) > now:
        return True
    key = config.require("MAILGUN_API_KEY")
    r = httpx.get(f"{BASE}/domains/{domain}/tracking", auth=("api", key), timeout=30)
    r.raise_for_status()
    ok = bool(((r.json().get("tracking") or {}).get("unsubscribe") or {}).get("active"))
    if ok:
        _UNSUB_OK[domain] = now + max_age
    return ok


_HTTPS_OK: dict[str, float] = {}


def https_links_ready(domain: str, max_age: float = 600.0) -> tuple[bool, str]:
    """Do this domain's tracked links go out as https with a live certificate? Mailgun rewrites every link
    through email.<domain>; with web_scheme http a strict browser blocks the click, with web_scheme https but
    no certificate the click fails outright (Matthew at Uscenes, 14 Sep 2026). Cached 10 min when ready."""
    import time as _t
    if _HTTPS_OK.get(domain, 0) > _t.time():
        return True, "ok"
    key = config.require("MAILGUN_API_KEY")
    v4 = f"{BASE.replace('/v3', '/v4')}/domains/{domain}"
    d = httpx.get(v4, auth=("api", key), timeout=30).json().get("domain") or {}
    if d.get("web_scheme") != "https":
        # SELF-HEAL: news.sensa.digital flipped back to http on its own ~10 min after being set (14 Sep 2026).
        # Re-apply and re-read; only report failure if it will not stick.
        httpx.put(v4, auth=("api", key), data={"web_scheme": "https"}, timeout=30)
        d = httpx.get(v4, auth=("api", key), timeout=30).json().get("domain") or {}
        if d.get("web_scheme") != "https":
            return False, "web_scheme is http and could not be set to https"
        print(f"[mailgun] {domain}: web_scheme was http, re-applied https", flush=True)
    host = f"{d.get('web_prefix') or 'email'}.{domain}"
    st = httpx.get(f"{BASE.replace('/v3', '/v2')}/x509/{host}/status", auth=("api", key), timeout=30).json()
    if st.get("status") != "active":
        return False, f"tracking certificate for {host}: {st.get('status') or st.get('message') or 'missing'}"
    _HTTPS_OK[domain] = _t.time() + max_age
    return True, "ok"


def ensure_unsubscribe_tracking(domain: str) -> None:
    """Switch unsubscribe tracking ON for the domain if it is off (idempotent), then verify."""
    if unsubscribe_tracking_on(domain):
        return
    key = config.require("MAILGUN_API_KEY")
    httpx.put(f"{BASE}/domains/{domain}/tracking/unsubscribe", auth=("api", key), data={"active": "true"},
              timeout=30).raise_for_status()
    _UNSUB_OK.pop(domain, None)
    if not unsubscribe_tracking_on(domain):
        raise RuntimeError(f"unsubscribe tracking could not be enabled on {domain}; refusing to send")


def suppressions(domain: str, kind: str) -> list[str]:
    """List a domain's suppression addresses. kind = 'unsubscribes' | 'complaints' | 'bounces'."""
    key = config.require("MAILGUN_API_KEY")
    r = httpx.get(f"{BASE}/{domain}/{kind}", auth=("api", key), params={"limit": 1000}, timeout=30)
    r.raise_for_status()
    return [it["address"] for it in r.json().get("items", []) if it.get("address")]
