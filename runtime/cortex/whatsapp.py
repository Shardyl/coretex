"""WhatsApp inbound: a message arrives, Cortex classifies it, captures the contact, drafts a reply IN
RASHAD'S VOICE and lands it in the Inbox for approval. Approving is what sends.

TWO TRANSPORTS, one brain. Both funnel into `_process_message`, so triage, CRM capture and drafting behave
identically whichever way a message reached us:
  • CLOUD API (the real one, Meta's WhatsApp Business Platform) — Meta POSTs to /api/whatsapp/webhook and
    approval sends via `send_text`. Requires the WABA env keys below.
  • RUNNER (WhatsApp Web on an office box, driven by Patchright) — kept as a fallback transport. The runner
    pushes chats to /api/whatsapp/inbox and types approved replies back itself. NOTE: automating WhatsApp Web
    is against WhatsApp's terms and got a fresh number banned within hours on 19 Aug 2026, so the Cloud API
    is the supported path and this is here only because it exists and works.

Deliberately the same shape as social_dm.py (LinkedIn) — ingest -> dedupe -> classify -> draft -> card — with
two WhatsApp-specific differences:
  • the identity is a PHONE NUMBER, not an email, so CRM capture goes through crm.match_or_add_by_phone
    (crm_master is email-keyed and add_inbound_contact hard-refuses an email-less contact).
  • approval actually SENDS, so the card's kind is 'wa_reply' — outward, never auto, biometric step-up.

VOICE: WhatsApp is a personal channel — people message it precisely because it is NOT a formal business
inbox. The tone lives in the skill craft/rules (social-dm-replies), never here; this module only says
"it's WhatsApp, keep it human" in the brief and lets the editable skill do the rest.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request

from . import config, crm, db, provider, social_dm, store, worker

# Which WhatsApp account routes to which company. Overridable live via the 'wa_routing' setting so a new
# number/company never needs a deploy.
_DEFAULT_ROUTING = {"sensa-uk": {"company_id": 3, "skill_key": "social-dm-replies", "author": "rashad"}}

GRAPH = "https://graph.facebook.com/v23.0"


def routing(account: str) -> dict:
    cfg = db.setting_get("wa_routing") or {}
    return {**_DEFAULT_ROUTING, **cfg}.get(account) or _DEFAULT_ROUTING["sensa-uk"]


def _key(account: str, phone: str, msg: str) -> str:
    return hashlib.sha1(f"{account}|{phone}|{msg}".encode("utf-8", "replace")).hexdigest()[:16]


def _classify(name: str, phone: str, msg: str, slug: str) -> dict:
    """WhatsApp triage. People who message a business WhatsApp are overwhelmingly real, so the bar to reply is
    LOW — only obvious spam/scam is filed. Also flags 'personal' (an actual friend/acquaintance, common on a
    number that used to be Rashad's personal line) so the draft doesn't answer a mate like a lead."""
    try:
        out = provider.think_json(
            "You triage inbound WHATSAPP messages for a Dubai production company. WhatsApp is a personal, "
            "informal channel. Almost every real human deserves a reply. Only decline for obvious spam, "
            "scams, crypto, or automated blasts. Distinguish a genuine business ENQUIRY from a PERSONAL "
            "message (a friend, family, or someone who knows the owner personally) — this number was once a "
            "personal line, so old contacts still message it.",
            f"From: {name or 'unknown'} ({phone})\nTheir message: {msg}\n\n"
            'Return JSON: {"reply": boolean, "category": "enquiry|personal|supplier|spam", '
            '"name": "their personal name ONLY if they actually state it or sign off with it, '
            'else empty string - never guess, never use the phone number", '
            '"summary": "one line, who and what", "reason": "short"}',
            model=provider.MODEL_ROUTER, purpose="wa_triage", company=slug)
        return {"reply": bool(out.get("reply")), "category": (out.get("category") or "enquiry"),
                "name": (out.get("name") or "").strip(),
                "summary": (out.get("summary") or "").strip(), "reason": (out.get("reason") or "").strip()}
    except Exception:  # noqa: BLE001 — triage must never block an inbound message
        return {"reply": True, "category": "enquiry", "name": "", "summary": "",
                "reason": "triage unavailable -> default reply"}


def _clean_phone(p: str) -> str:
    p = re.sub(r"[^\d+]", "", p or "")
    return ("+" + p.lstrip("+")) if p else ""


# ---- the shared brain: one inbound message -> CRM capture + an approval card --------------------------

def _process_message(rt: dict, co: dict, skill: dict, slug: str, account: str,
                     phone: str, name: str, msg: str, chat_id: str = "") -> str:
    """Triage one inbound message, capture the contact, draft a reply card. Returns drafted | skipped.
    Shared by BOTH transports so the Cloud API and the runner can never drift apart in behaviour."""
    verdict = _classify(name, phone, msg, slug)
    # CRM capture happens for every real human (even ones we don't draft for) so the contact is never lost.
    if verdict.get("category") != "spam":
        try:
            # WhatsApp never gives us a name for an unknown sender. The only honest source is the message
            # itself, when they introduce themselves. Fill-if-blank, so a later message where they DO say who
            # they are backfills the contact.
            crm.match_or_add_by_phone(
                phone, verdict.get("name") or name, slug, source="whatsapp",
                summary=verdict.get("summary") or msg[:200], classification=verdict.get("category"))
        except Exception:  # noqa: BLE001 — a CRM hiccup must not lose the reply
            pass
    if not verdict.get("reply"):
        # Traffic on this number is very low (Rashad, 22 Aug 2026), so nothing arrives silently: a message we
        # decline to answer still raises an FYI card, with the reason, so he can see it was a real decision
        # and override it. No draft, no approval, nothing armed to send.
        try:
            from . import notifications
            notifications.notify(
                f"WhatsApp filed, no reply drafted ({verdict.get('category') or 'spam'})",
                f"{name or phone}: {msg[:180]}" + (f"  [{verdict.get('reason')}]" if verdict.get("reason") else ""),
                priority="fyi", category="social", company_id=rt["company_id"],
                item={"name": name or phone, "phone": phone, "cat": verdict.get("category") or "spam"})
        except Exception:  # noqa: BLE001 — visibility must never block ingest
            pass
        return "skipped"
    personal = verdict.get("category") == "personal"
    # a real name only — never the phone number WhatsApp puts where a name would go
    know_name = verdict.get("name") or ("" if _clean_phone(name) else name)
    # channel voice + personal/unknown-number behaviour live in the social-dm-replies skill RULES
    # (worker.draft serves them) — the brief carries only the FACTS of this message.
    brief = (
        "Draft a reply to this WhatsApp message (the skill's standing rules govern the voice and shape). "
        + ("FACT: this is a PERSONAL message from someone who knows the owner, not a business lead.\n\n"
           if personal else f"FACT: triaged as a '{verdict.get('category')}' message.\n\n")
        + ("FACT: the sender's name is unknown — only their number.\n\n" if not know_name else "")
        + f"From: {know_name or phone}\nTheir message: {msg}")
    try:
        draft = worker.draft(skill, co, {"brief": brief}, author=rt.get("author") or "rashad")
    except Exception:  # noqa: BLE001
        draft = ""
    task = store.create_task(rt["company_id"], skill["id"], "wa_reply", {
        "brief": brief, "channel": "whatsapp", "account": account, "recipient": know_name or phone,
        "phone": phone, "chat_id": chat_id, "their_message": msg, "triage": verdict})
    if draft:
        store.update_task(task["id"], draft=draft, status="awaiting_approval")
    return "drafted"


def _lane(account: str):
    """(routing, company, skill, slug) or None when the company/skill isn't set up."""
    rt = routing(account)
    co = store.get_company(rt["company_id"])
    skill = store.get_skill_by_key(rt["company_id"], rt["skill_key"]) if co else None
    if not (co and skill):
        return None
    return rt, co, skill, co.get("slug", "sensa")


# ---- transport 1: Meta Cloud API ---------------------------------------------------------------------

def verify_webhook(mode: str, token: str, challenge: str) -> str:
    """Meta's subscription handshake. It GETs the webhook with hub.verify_token and will not save the
    config unless we echo hub.challenge back verbatim. Constant-time compare, and a missing/blank
    configured token must NEVER pass."""
    want = config.get("WHATSAPP_VERIFY_TOKEN") or ""
    if mode == "subscribe" and want and hmac.compare_digest(token or "", want):
        return challenge or ""
    raise PermissionError("bad verify token")


def check_signature(raw: bytes, header: str) -> bool:
    """Meta signs every webhook POST with the APP SECRET as X-Hub-Signature-256. Unsigned or wrongly signed
    payloads are forgeries — anyone can POST to a public URL. No secret configured = reject everything,
    rather than silently accepting spoofed messages."""
    secret = config.get("WHATSAPP_APP_SECRET") or ""
    if not (secret and header and header.startswith("sha256=")):
        return False
    mine = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, header.split("=", 1)[1])


def ingest_cloud(payload: dict, account: str = "sensa-uk") -> dict:
    """Handle one Meta webhook body. Shape:
       entry[].changes[].value.messages[]  + .contacts[] (the sender's WhatsApp profile name)
    Statuses (delivered/read receipts) arrive on the same hook and are ignored. Meta RETRIES on any non-200,
    so this must never raise: a bad message is dropped, not propagated."""
    lane = _lane(account)
    if not lane:
        return {"drafted": 0, "skipped": 0, "reason": "company/skill missing"}
    rt, co, skill, slug = lane
    seen = set(db.setting_get(f"wa_seen:{account}") or [])
    drafted = skipped = 0
    fresh: list[str] = []
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            # the sender's WhatsApp profile name, keyed by wa_id — this is the push name Rashad expected
            names = {c.get("wa_id"): ((c.get("profile") or {}).get("name") or "")
                     for c in (value.get("contacts") or [])}
            for m in value.get("messages") or []:
                if m.get("type") != "text":       # media/audio/location: capture later, never guess at text
                    continue
                phone = _clean_phone(m.get("from") or "")
                msg = ((m.get("text") or {}).get("body") or "").strip()
                if not (phone and msg):
                    continue
                k = _key(account, phone, msg)
                if k in seen or k in fresh:       # Meta retries on non-200; never draft the same twice
                    continue
                fresh.append(k)
                try:
                    out = _process_message(rt, co, skill, slug, account, phone,
                                           names.get(m.get("from"), ""), msg, chat_id=m.get("id") or "")
                except Exception:  # noqa: BLE001 — one bad message must not 500 the whole webhook
                    continue
                drafted += 1 if out == "drafted" else 0
                skipped += 1 if out == "skipped" else 0
    if fresh:
        db.setting_set(f"wa_seen:{account}", (list(seen) + fresh)[-800:])
    return {"drafted": drafted, "skipped": skipped}


def send_text(phone: str, text: str) -> dict:
    """Send a plain text reply via the Cloud API. Only valid inside the 24h customer service window; outside
    it Meta requires a pre-approved template, which we do not have yet and which is a separate build."""
    from . import db as _db
    if _db.setting_get("whatsapp_paused") or _db.setting_get("outbound_paused"):
        raise RuntimeError("WhatsApp sending is PAUSED - resume it to send")
    token = config.get("WHATSAPP_TOKEN")
    pnid = config.get("WHATSAPP_PHONE_NUMBER_ID")
    if not (token and pnid):
        raise RuntimeError("WhatsApp Cloud API not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID)")
    body = json.dumps({"messaging_product": "whatsapp", "recipient_type": "individual",
                       "to": phone.lstrip("+"), "type": "text",
                       "text": {"preview_url": False, "body": text}}).encode()
    req = urllib.request.Request(f"{GRAPH}/{pnid}/messages", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:           # surface Meta's own error text, not a bare 400
        raise RuntimeError(f"WhatsApp send failed ({e.code}): {e.read().decode()[:300]}") from e


def cloud_ready() -> bool:
    return bool(config.get("WHATSAPP_TOKEN") and config.get("WHATSAPP_PHONE_NUMBER_ID"))


# ---- transport 2: the office-box runner (fallback) ----------------------------------------------------

def ingest_threads(account: str, threads: list[dict]) -> dict:
    """The runner pushes [{phone, name, message, ts, chat_id, from_me}]. Drafts a reply card per NEW inbound
    message. Reads + drafts only — nothing is sent here; the owner approves in the Inbox."""
    lane = _lane(account)
    if not lane:
        return {"drafted": 0, "skipped": 0, "reason": "company/skill missing"}
    rt, co, skill, slug = lane
    seen = set(db.setting_get(f"wa_seen:{account}") or [])
    drafted = skipped = stale = 0
    fresh: list[str] = []
    for t in threads or []:
        if t.get("from_me"):                       # our own outbound echo — never a thing to answer
            continue
        msg = (t.get("message") or "").strip()
        phone = _clean_phone(t.get("phone") or "")
        name = (t.get("name") or "").strip()
        if not msg or not phone:
            continue
        if t.get("ts") and not social_dm._is_recent(t.get("ts")):   # current messages only, never the backlog
            stale += 1
            continue
        k = _key(account, phone, msg)
        if k in seen or k in fresh:                # never draft the same message twice
            continue
        fresh.append(k)
        out = _process_message(rt, co, skill, slug, account, phone, name, msg, chat_id=t.get("chat_id") or "")
        drafted += 1 if out == "drafted" else 0
        skipped += 1 if out == "skipped" else 0
    if fresh:
        db.setting_set(f"wa_seen:{account}", (list(seen) + fresh)[-800:])
    return {"drafted": drafted, "skipped": skipped, "stale": stale}
