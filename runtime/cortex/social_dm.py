"""LinkedIn DM inbound: for each conversation that needs a reply, classify it and draft a response IN THE
OWNER'S VOICE, then land it in the Inbox for approval. Reuses the email pipeline (engine.triage_inquiry +
worker.draft + store.create_task), just a different channel (linkedin) and the owner's personal voice.

The runner (inbox.py) reads the messaging list and pushes the thread previews here. We treat a thread as
"needs a reply" when the LAST message is NOT from the owner (snippet not starting 'You:'), which is more
reliable than the unread dot (it clears the moment the owner glances at the thread).
"""
from __future__ import annotations

import hashlib

from . import db, engine, social_config, store, worker

# Whose inbound DMs route to which company. rashad's personal inbox -> Sensa (his production company);
# default Sensa(3). (Harvest routes to FilmSpoke(5); inbound routes to Sensa(3) - source decides company.)
_INBOUND_COMPANY = {"rashad": 3}


def _parse(snippet: str) -> tuple[bool, str]:
    """(needs_reply, message). 'You:' = owner replied last -> no. 'Sponsored' = ad -> no. Else strip the
    'Sender: ' prefix off the preview to get their message."""
    s = (snippet or "").strip()
    if not s or s.lower().startswith("you:") or s.lower().startswith("sponsored"):
        return False, ""
    msg = s.split(":", 1)[1].strip() if ":" in s else s
    return bool(msg), msg


def _key(account: str, name: str, msg: str) -> str:
    return hashlib.sha1(f"{account}|{name}|{msg}".encode("utf-8", "replace")).hexdigest()[:16]


def ingest_threads(account: str, threads: list[dict]) -> dict:
    cfg = social_config.get_account(account) or {}
    company_id = _INBOUND_COMPANY.get(account, cfg.get("inbound_company_id", 3))
    person_key = cfg.get("person_key") or account
    co = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "sales-first-response") if co else None
    if not (co and skill):
        return {"drafted": 0, "skipped": 0, "reason": "company/skill missing"}
    slug = co.get("slug", "sensa")
    seen = set(db.setting_get(f"dm_seen:{account}") or [])
    drafted = skipped = 0
    fresh: list[str] = []
    for t in threads or []:
        name = (t.get("name") or "").strip()
        ok, msg = _parse(t.get("snippet"))
        if not ok or not name:
            continue
        k = _key(account, name, msg)
        if k in seen or k in fresh:          # dedupe: never re-draft the same message twice
            continue
        fresh.append(k)
        inq = {"name": name, "email": "", "subject": "LinkedIn message", "message": msg}
        try:
            verdict = engine.triage_inquiry(inq, slug)
        except Exception:  # noqa: BLE001
            verdict = {"genuine": True, "category": "unclear", "reason": "triage unavailable"}
        if not verdict.get("genuine"):        # obvious spam/junk pitch -> filed, not drafted
            skipped += 1
            continue
        brief = ("Draft a reply to this LinkedIn direct message. Write a SHORT, natural LinkedIn DM reply "
                 "(1 to 4 sentences, conversational, no email formatting, no subject, no sign-off, no signature). "
                 f"Reply directly to {name}.\n\nTheir message: {msg}")
        try:
            draft = worker.draft(skill, co, {"brief": brief}, author=person_key)   # author -> the owner's voice
        except Exception:  # noqa: BLE001
            draft = ""
        task = store.create_task(company_id, skill["id"], "dm_reply", {
            "brief": brief, "channel": "linkedin", "account": account, "recipient": name,
            "their_message": msg, "thread": t.get("thread") or "", "triage": verdict, "inquiry": inq})
        if draft:
            store.update_task(task["id"], draft=draft, status="awaiting_approval")
        drafted += 1
    if fresh:
        db.setting_set(f"dm_seen:{account}", (list(seen) + fresh)[-800:])
    return {"drafted": drafted, "skipped": skipped}
