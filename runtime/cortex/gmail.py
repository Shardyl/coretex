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


def _token_for(rt_key: str, purpose: str) -> str:
    with open(_CLIENT) as f:
        c = (json.load(f).get("web") or {})
    rt = db.setting_get(rt_key)
    if not rt:
        raise RuntimeError(f"Gmail not connected — authorise at /oauth/google/start?purpose={purpose}")
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": rt, "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _access_token() -> str:
    """The mailbox Cortex READS enquiries from (api@tabscanner.com)."""
    return _token_for("gmail_refresh_token", "gmail")


def send_account() -> str | None:
    """The mailbox replies are SENT from, if a separate one is connected (else replies go from the read inbox)."""
    return db.setting_get("gmail_send_account")


def _send_token() -> str:
    """Prefer the dedicated sending mailbox (so the reply lands in YOUR Sent folder); else the read inbox."""
    if db.setting_get("gmail_send_refresh_token"):
        return _token_for("gmail_send_refresh_token", "gmail_send")
    return _access_token()


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


def send_message(to: str, subject: str, body: str, from_addr: str | None = None,
                 cc: str | None = None, html: str | None = None,
                 inline_images: list | None = None, bcc: str | None = None,
                 files: list | None = None) -> dict:
    """Send an email from the connected sending mailbox (gmail.modify) — it lands in that account's Sent
    folder. If `html` is given, sends a multipart/alternative (plain + html); any `inline_images`
    (list of (cid, filepath)) are embedded so a footer logo renders. `from_addr` is honoured when it
    matches the authenticated account or a verified 'send mail as' identity."""
    # SAFETY (enforced HERE so no path can bypass it): a global kill-switch + recipient sanity check.
    if db.setting_get("email_sending_paused"):
        raise RuntimeError("email sending is PAUSED — resume it to send")
    primary = (to or "").split(",")[0].strip().strip("<>")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", primary):
        raise ValueError(f"refusing to send: invalid recipient {to!r}")
    if from_addr and primary.lower() == from_addr.split("<")[-1].strip(" <>").lower():
        raise ValueError("refusing to send: recipient equals sender (loop)")
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.image import MIMEImage
    tok = _send_token()
    if html:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body or "", "plain", "utf-8"))
        alt.attach(MIMEText(html, "html", "utf-8"))
        if inline_images:
            msg = MIMEMultipart("related")
            msg.attach(alt)
            for cid, path in inline_images:
                try:
                    with open(path, "rb") as f:
                        img = MIMEImage(f.read())
                    img.add_header("Content-ID", f"<{cid}>")
                    img.add_header("Content-Disposition", "inline", filename=cid)
                    msg.attach(img)
                except OSError:
                    pass
        else:
            msg = alt
    else:
        msg = MIMEText(body or "", "plain", "utf-8")
    if files:   # real file attachments (data: URLs) -> wrap the body in a multipart/mixed
        from email import encoders as _enc
        from email.mime.base import MIMEBase
        outer = MIMEMultipart("mixed")
        outer.attach(msg)
        for i, u in enumerate(files):
            if not isinstance(u, str) or not u.startswith("data:") or ";base64," not in u:
                continue
            head, b64 = u.split(";base64,", 1)
            mime = head[5:] or "application/octet-stream"
            maintype, _, subtype = mime.partition("/")
            try:
                data = base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                continue
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(data)
            _enc.encode_base64(part)
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
                   "application/pdf": "pdf"}.get(mime, "bin")
            part.add_header("Content-Disposition", "attachment", filename=f"attachment{i + 1}.{ext}")
            outer.attach(part)
        msg = outer
    msg["To"] = to
    msg["Subject"] = subject
    if from_addr:
        msg["From"] = from_addr
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = httpx.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                   headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                   json={"raw": raw}, timeout=30)
    r.raise_for_status()
    return r.json()


def list_inquiries(days: int = 7, limit: int = 50) -> list[dict]:
    """Contact-form inquiries from the last `days` days, parsed."""
    tok = _access_token()
    q = f'subject:"New enquiry from" subject:"tabscanner.com" newer_than:{days}d'
    res = _get(tok, "messages", {"q": q, "maxResults": limit})
    out = []
    for m in res.get("messages", []):
        out.append(_parse(_get(tok, f"messages/{m['id']}", {"format": "full"})))
    return out


def _parse_generic(msg: dict) -> dict:
    """Light parse of ANY email (sender, subject, body) — for the inbox classifier, no form assumptions."""
    h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
    frm = h.get("from", "")
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>', frm)
    name = ((m.group(1).strip() if m else "") or (frm.split("@")[0] if "@" in frm else frm)).strip()
    email = (m.group(2).strip() if m else frm).strip().strip("<>")
    return {"gmail_id": msg.get("id"), "subject": h.get("subject", ""), "from": frm, "date": h.get("date", ""),
            "name": name, "email": email, "body": _plain_body(msg.get("payload", {})),
            "snippet": msg.get("snippet", "")}


def list_recent(days: int = 2, limit: int = 30, rt_key: str = "gmail_refresh_token",
                q: str | None = None, skip: set | None = None) -> list[dict]:
    """Recent inbox mail (not just the contact form) for the inbox classifier. `rt_key` selects which
    company's mailbox refresh token to use; `q` overrides the default Gmail search; `skip` is a set of
    already-seen message ids to NOT re-fetch (so each message is fetched in full at most once)."""
    tok = _token_for(rt_key, "gmail")
    res = _get(tok, "messages", {"q": q or f"in:inbox newer_than:{days}d", "maxResults": limit})
    skip = skip or set()
    return [_parse_generic(_get(tok, f"messages/{m['id']}", {"format": "full"}))
            for m in res.get("messages", []) if m["id"] not in skip]
