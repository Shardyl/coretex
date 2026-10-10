"""Write a contact into the owner's PERSONAL Google Contacts, so it reaches his phone.

WHY THIS EXISTS. WhatsApp hands us a number and a display name and nothing else, and the business API
turned out to be the wrong shape for a live sales chat (sites reverted to his personal number, 10 Oct
2026). The flow he chose instead: the API number captures the enquiry, Cortex acknowledges it, creates
the opportunity and files the person here. Google syncs that contact to his Android handset, the handset
is where WhatsApp reads its contact list, so by the time he opens the chat the stranger already has a
name, a company and a note saying where they came from.

NOT the CRM. `crm_master` remains the record; this is a convenience copy on his phone, deliberately
named so he can spot and prune them ("Moe (Sensa enquiry, 10 Oct)").

Scope `auth/contacts` on the personal OAuth client (`contacts_refresh_token:personal`). The People API
must be enabled on the `cortex-personal` project; it was, 10 Oct 2026.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import db

PEOPLE = "https://people.googleapis.com/v1"
_CLIENT_JSON = "/etc/cortex/google_oauth_client_personal.json"


def ready() -> bool:
    return bool(db.setting_get("contacts_refresh_token:personal"))


def _access_token() -> str:
    """A fresh access token for the personal account. Raises when contacts was never authorised."""
    rt = db.setting_get("contacts_refresh_token:personal")
    if not rt:
        raise RuntimeError("Google Contacts not authorised - run /oauth/google/start?purpose=contacts"
                           "&company=personal")
    with open(_CLIENT_JSON, encoding="utf-8") as fh:
        cj = json.load(fh)
    w = cj.get("web") or cj.get("installed") or {}
    data = urllib.parse.urlencode({"client_id": w.get("client_id"), "client_secret": w.get("client_secret"),
                                   "refresh_token": rt, "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def _call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{PEOPLE}/{path.lstrip('/')}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + _access_token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:                 # surface Google's own message, not a bare 400
        raise RuntimeError(f"People API {method} {path} failed ({e.code}): "
                           f"{e.read().decode()[:300]}") from e


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def find_by_phone(phone: str) -> str | None:
    """The resourceName of an existing contact on this number, or None.

    Matched on the LAST NINE DIGITS, which is the opposite of the CRM's rule and right here: this is one
    person's own address book, not 33k imported rows, so a collision is vanishingly unlikely and the real
    risk is the reverse, writing a second copy of someone he already has under a different spelling."""
    d = _digits(phone)
    if len(d) < 9:
        return None
    try:
        out = _call("GET", "people:searchContacts?" + urllib.parse.urlencode(
            {"query": d[-9:], "readMask": "names,phoneNumbers"}))
    except Exception:  # noqa: BLE001 - a failed search must never block the write
        return None
    for r in out.get("results") or []:
        p = r.get("person") or {}
        for n in p.get("phoneNumbers") or []:
            if _digits(n.get("canonicalForm") or n.get("value") or "").endswith(d[-9:]):
                return p.get("resourceName")
    return None


def add_enquiry(phone: str, name: str = "", company: str = "", brand: str = "",
                note: str = "", source: str = "") -> dict:
    """File a WhatsApp enquiry as a contact on his phone. Returns {created|exists|skipped, ...}.

    Fail-soft by design: the CRM row and the opportunity are the record, so a Google hiccup must never
    cost the enquiry. The caller logs the outcome; nothing here raises on a normal failure."""
    if not _digits(phone):
        return {"ok": False, "skipped": "no number"}
    if not ready():
        return {"ok": False, "skipped": "contacts not authorised"}
    existing = find_by_phone(phone)
    if existing:
        return {"ok": True, "exists": existing}
    # The NAME is how he finds and prunes these later, so it says what it is and when. A WhatsApp push
    # name is often a handle or the number itself; an unnamed contact is honest, a contact called
    # "+971 58" is not.
    day = datetime.now(timezone.utc).strftime("%d %b").lstrip("0")
    label = f"{(brand or 'Sensa')} enquiry, {day}"
    display = (name or "").strip()
    if not re.search(r"[A-Za-z]", display):               # the number, or an emoji handle: not a name
        display = ""
    given = f"{display} ({label})" if display else f"Unknown ({label})"
    body: dict = {
        "names": [{"givenName": given}],
        "phoneNumbers": [{"value": phone, "type": "mobile"}],
        "biographies": [{"value": "  ".join(x for x in [note, source] if x)[:1000],
                         "contentType": "TEXT_PLAIN"}] if (note or source) else [],
    }
    if company:
        body["organizations"] = [{"name": company}]
    try:
        out = _call("POST", "people:createContact", body)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "created": out.get("resourceName"), "name": given}
