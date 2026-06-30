"""LinkedIn DM inbound: for each conversation that needs a reply, classify it and draft a response IN THE
OWNER'S VOICE, then land it in the Inbox for approval. Reuses the email pipeline (engine.triage_inquiry +
worker.draft + store.create_task), just a different channel (linkedin) and the owner's personal voice.

The runner (inbox.py) reads the messaging list and pushes the thread previews here. We treat a thread as
"needs a reply" when the LAST message is NOT from the owner (snippet not starting 'You:'), which is more
reliable than the unread dot (it clears the moment the owner glances at the thread).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from . import db, provider, social_config, store, worker

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _is_recent(ts: str, days: int = 7) -> bool:
    """Is this conversation's last-activity timestamp within `days`? CONSERVATIVE: if it can't be confidently
    read as recent, return False (better to miss a reply than answer a months-old message). Handles an ISO
    datetime attr, a time-of-day (today), 'now'/'yesterday', weekday names, relative (Nm/Nh/Nd/Nw/Nmo), and
    absolute 'Apr 17' / '17 Apr' dates parsed against today."""
    t = (ts or "").strip()
    if not t:
        return False
    try:                                              # ISO datetime attr (most reliable)
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) <= timedelta(days=days)
    except Exception:  # noqa: BLE001
        pass
    tl = t.lower()
    if re.match(r"^\d{1,2}:\d{2}", tl):               # a time of day -> today
        return True
    if tl in ("now", "just now", "yesterday"):
        return True
    if tl[:3] in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):   # a weekday -> this week
        return days >= 6
    m = re.match(r"^(\d+)\s*(mo|month|min|m|hr|hour|h|day|d|week|wk|w|year|yr|y)\b", tl)
    if m:
        n, u = int(m.group(1)), m.group(2)
        if u in ("mo", "month", "year", "yr", "y"):
            return False
        if u in ("min", "m", "hr", "hour", "h"):
            return True
        if u in ("day", "d"):
            return n <= days
        if u in ("week", "wk", "w"):
            return n * 7 <= days
    mm = re.match(r"^([a-z]{3,9})\.?\s+(\d{1,2})$|^(\d{1,2})\s+([a-z]{3,9})", tl)   # 'Apr 17' / '17 Apr'
    if mm:
        mon = (mm.group(1) or mm.group(4) or "")[:3]
        day = int(mm.group(2) or mm.group(3))
        mi = _MONTHS.get(mon)
        if mi:
            now = datetime.now(timezone.utc)
            try:
                d = datetime(now.year, mi, day, tzinfo=timezone.utc)
                if d > now:                            # e.g. 'Dec 30' seen in early Jan -> last year
                    d = datetime(now.year - 1, mi, day, tzinfo=timezone.utc)
                return (now - d) <= timedelta(days=days)
            except Exception:  # noqa: BLE001
                return False
    return False

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


def _classify_dm(name: str, msg: str, slug: str) -> dict:
    """DM-appropriate triage (NOT the strict website-enquiry filter). The owner WANTS to reply to real people;
    only obvious automated spam is skipped. Returns {reply, category, reason}."""
    try:
        out = provider.think_json(
            "You triage LinkedIn DIRECT MESSAGES for the account owner, who WANTS to reply to real people: "
            "potential buyers/clients, peers in the industry, people congratulating them, genuine networking, "
            "and even a sales pitch FROM A REAL HUMAN (a short polite reply keeps the relationship warm). Only "
            "decline to reply to OBVIOUS automated spam: bots, mass-blast templates with zero personalisation, "
            "scams, crypto, or clearly automated outreach sequences.",
            f"From: {name}\nTheir message: {msg}\n\n"
            'Return JSON: {"reply": boolean, "category": "buyer|talent|supporter|peer|spam", "reason": "short"}',
            model=provider.MODEL_ROUTER, purpose="dm_triage", company=slug)
        return {"reply": bool(out.get("reply")), "category": (out.get("category") or "supporter"),
                "reason": (out.get("reason") or "").strip()}
    except Exception:  # noqa: BLE001
        return {"reply": True, "category": "supporter", "reason": "triage unavailable -> default to reply"}


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
    drafted = skipped = stale = 0
    fresh: list[str] = []
    for t in threads or []:
        name = (t.get("name") or "").strip()
        ok, msg = _parse(t.get("snippet"))
        if not ok or not name:
            continue
        if not _is_recent(t.get("ts")):      # CURRENT messages only: skip the old backlog entirely
            stale += 1
            continue
        k = _key(account, name, msg)
        if k in seen or k in fresh:          # dedupe: never re-draft the same message twice
            continue
        fresh.append(k)
        inq = {"name": name, "email": "", "subject": "LinkedIn message", "message": msg}
        verdict = _classify_dm(name, msg, slug)
        if not verdict.get("reply"):          # obvious automated spam -> filed, not drafted
            skipped += 1
            continue
        brief = ("Draft a reply to this LinkedIn direct message. Write a SHORT, natural LinkedIn DM reply "
                 "(1 to 4 sentences, conversational, no email formatting, no subject, no sign-off, no signature). "
                 f"This is a '{verdict.get('category')}' message. Reply directly to {name}.\n\n"
                 f"Their message: {msg}")
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
    return {"drafted": drafted, "skipped": skipped, "stale": stale}
