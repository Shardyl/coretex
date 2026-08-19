"""WhatsApp inbound: the office-box runner reads WhatsApp Web (linked device), pushes each chat that needs a
reply here, and Cortex classifies it, captures the contact, drafts a reply IN RASHAD'S VOICE and lands it in
the Inbox for approval. Approving QUEUES the reply for the runner to type back (engine._execute 'wa_reply').

Deliberately the same shape as social_dm.py (LinkedIn) — ingest -> dedupe -> classify -> draft -> card — with
two WhatsApp-specific differences:
  • the identity is a PHONE NUMBER, not an email, so CRM capture goes through crm.match_or_add_by_phone
    (crm_master is email-keyed and add_inbound_contact hard-refuses an email-less contact).
  • approval actually SENDS (via the runner), so the card's kind is 'wa_reply' — outward, never auto.

VOICE: WhatsApp is a personal channel — people message it precisely because it is NOT a formal business
inbox. The tone lives in the skill craft/rules (social-dm-replies), never here; this module only says
"it's WhatsApp, keep it human" in the brief and lets the editable skill do the rest.
"""
from __future__ import annotations

import hashlib
import re

from . import crm, db, provider, social_dm, store, worker

# Which WhatsApp account routes to which company. Overridable live via the 'wa_routing' setting so a new
# number/company never needs a deploy.
_DEFAULT_ROUTING = {"sensa-uk": {"company_id": 3, "skill_key": "social-dm-replies", "author": "rashad"}}


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


def ingest_threads(account: str, threads: list[dict]) -> dict:
    """The runner pushes [{phone, name, message, ts, chat_id, from_me}]. Drafts a reply card per NEW inbound
    message. Reads + drafts only — nothing is sent here; the owner approves in the Inbox."""
    rt = routing(account)
    co = store.get_company(rt["company_id"])
    skill = store.get_skill_by_key(rt["company_id"], rt["skill_key"]) if co else None
    if not (co and skill):
        return {"drafted": 0, "skipped": 0, "reason": "company/skill missing"}
    slug = co.get("slug", "sensa")
    seen = set(db.setting_get(f"wa_seen:{account}") or [])
    drafted = skipped = stale = captured = 0
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
        verdict = _classify(name, phone, msg, slug)
        # CRM capture happens for every real human (even ones we don't draft for) so the contact is never lost.
        if verdict.get("category") != "spam":
            try:
                # WhatsApp never gives us a name for an unknown sender - the row title IS the number. The
                # only honest source is the message itself, when they introduce themselves. Fill-if-blank,
                # so a later message where they DO say who they are backfills the contact.
                status, _row = crm.match_or_add_by_phone(
                    phone, verdict.get("name") or name, slug, source="whatsapp",
                    summary=verdict.get("summary") or msg[:200], classification=verdict.get("category"))
                captured += 1 if status == "added" else 0
            except Exception:  # noqa: BLE001 — a CRM hiccup must not lose the reply
                pass
        if not verdict.get("reply"):
            skipped += 1
            continue
        personal = verdict.get("category") == "personal"
        # a real name only — never the phone number WhatsApp puts in the chat-list title slot
        know_name = verdict.get("name") or ("" if _clean_phone(name) else name)
        brief = (
            "Draft a reply to this WhatsApp message. WhatsApp is a PERSONAL, informal channel — write the way "
            "a real person texts: short (1 to 4 sentences), warm, natural, no email formatting, no subject "
            "line, no sign-off, no signature, no corporate tone. "
            + ("This is a PERSONAL message from someone who knows Rashad, NOT a business lead — reply as a "
               "friend would, do not pitch, do not sell.\n\n" if personal else
               f"This is a '{verdict.get('category')}' message.\n\n")
            + ("You do NOT know this person's name yet — WhatsApp only gave us their number. If it "
               "reads naturally, ask who you're speaking with as part of the reply, the way anyone "
               "would when a message arrives from an unknown number. Do not force it if the message "
               "doesn't invite it.\n\n" if not know_name else "")
            + f"From: {know_name or phone}\nTheir message: {msg}")
        try:
            draft = worker.draft(skill, co, {"brief": brief}, author=rt.get("author") or "rashad")
        except Exception:  # noqa: BLE001
            draft = ""
        task = store.create_task(rt["company_id"], skill["id"], "wa_reply", {
            "brief": brief, "channel": "whatsapp", "account": account, "recipient": know_name or phone,
            "phone": phone, "chat_id": t.get("chat_id") or "", "their_message": msg, "triage": verdict})
        if draft:
            store.update_task(task["id"], draft=draft, status="awaiting_approval")
        drafted += 1
    if fresh:
        db.setting_set(f"wa_seen:{account}", (list(seen) + fresh)[-800:])
    return {"drafted": drafted, "skipped": skipped, "stale": stale, "captured": captured}
