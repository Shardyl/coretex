"""LinkedIn comment-REPLY watcher: after the persona comments on a target's post (the warm ladder), watch
for replies to that comment and draft the persona's response IN THEIR VOICE, landed as an approval card.
A real back-and-forth warms a target far more than a one-way comment, so this is the conversation layer.

Flow: `pending_reply_checks` hands the runner the posts the persona has recently commented on (+ the
persona's own comment text, to locate it on the page). The runner (notify.py) revisits each post, finds
replies under the persona's comment that AREN'T from the persona, and posts them back to `ingest_replies`,
which drafts a reply per new one and creates a `social_action` action='reply' approval card. Approved
replies execute via the existing poller -> actions.reply path. Reuses worker.draft (persona voice).
"""
from __future__ import annotations

import hashlib

from . import db, social, social_config, store, worker

_DAYS = 12   # only revisit posts commented on within this window (a stale thread isn't worth chasing)


def pending_reply_checks(account: str, limit: int = 25) -> list[dict]:
    """Posts the persona has recently commented on (done comment cards), with the persona's own comment text
    so the runner can locate it and read any replies beneath it."""
    rows = db.query(
        "select request->>'target' as post, request->>'content' as comment, request->>'person' as person "
        "from tasks where kind='social_action' and request->>'action'='comment' and status='done' "
        "and updated_at > now() - interval '%s days' order by updated_at desc limit %s" % (_DAYS, int(limit)))
    out, seen = [], set()
    for r in rows:
        post = r.get("post")
        if not post or post in seen:
            continue
        seen.add(post)
        out.append({"post": post, "comment": r.get("comment") or "", "person": r.get("person") or ""})
    return out


def _key(account: str, post: str, author: str, text: str) -> str:
    return hashlib.sha1(f"{account}|{post}|{author}|{text}".encode("utf-8", "replace")).hexdigest()[:16]


def ingest_replies(account: str, items: list[dict]) -> dict:
    """Each item = {post, parent_comment, replies:[{author, profile, text}]}. For every NEW reply (deduped),
    draft the persona's response and create a reply approval card. Returns {drafted}."""
    cfg = social_config.get_account(account) or {}
    company_id = cfg.get("company_id", 5)
    persona = cfg.get("persona", "Paul Anderson")
    person_key = cfg.get("person_key", "paul")
    co = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "outreach-linkedin-sequences") if co else None
    if not (co and skill):
        return {"drafted": 0, "reason": "company/skill missing"}
    seen = set(db.setting_get(f"reply_seen:{account}") or [])
    drafted = 0
    fresh: list[str] = []
    for it in items or []:
        post = it.get("post")
        parent = (it.get("parent_comment") or "").strip()
        for rep in it.get("replies") or []:
            author = (rep.get("author") or "").strip()
            text = (rep.get("text") or "").strip()
            profile = (rep.get("profile") or "").strip()
            if not author or not text or not post:
                continue
            k = _key(account, post, author, text)
            if k in seen or k in fresh:
                continue
            fresh.append(k)
            brief = (
                f"You are {persona}. You left a comment on a LinkedIn post, and {author} has replied to you "
                "under it. Write a SHORT, warm, natural reply (1 to 2 sentences) that keeps the conversation "
                "going and makes them feel good. Be personable and genuine; a light follow-up question is "
                "welcome. Do NOT be clever or profound, no aphorisms, no showing off, and NEVER pitch or "
                "mention your own company.\n\n"
                f"Your original comment: \"{parent}\"\n"
                f"{author}'s reply to you: \"{text}\"")
            try:
                draft = worker.draft(skill, co, {"brief": brief}, author=person_key)
            except Exception:  # noqa: BLE001
                draft = ""
            if not draft:
                continue
            t = social.post_action_card(company_id, account, persona, "reply", target=post,
                                        content=draft, person=profile, parent=parent)
            body = (f"{author} replied to {persona}'s comment.\n\n"
                    f"{persona.upper()}'S COMMENT:\n\"{parent}\"\n\n"
                    f"{author.upper()}'S REPLY:\n\"{text}\"\n\n"
                    f"{persona.upper()}'S DRAFT REPLY:\n{draft}")
            store.update_task(t["id"], title=f"{persona}: reply to {author}", draft=body)
            drafted += 1
    if fresh:
        db.setting_set(f"reply_seen:{account}", (list(seen) + fresh)[-1000:])
    return {"drafted": drafted}
