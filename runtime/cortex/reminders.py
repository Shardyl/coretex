"""Reminders — the TRIGGER layer (see CORTEX-TASKS-NOTIFICATIONS-MERGED-SPEC).

A reminder is a user-set, entity-attached, timed nudge. It is thin: on firing it EITHER emits a
notification (a pure nudge) OR spawns a normal task (an action reminder) which then flows through the
standard draft -> manager -> Inbox-approval pipeline. A reminder never needs approval itself.

Targets are polymorphic: contact:4821 / deal:77 / project:12 / account:9 / task:34 / none (freeform),
so one mechanism covers every entity. GST clock (reuse schedule._GST). Recurrence:
none | daily | weekly | monthly | weekday (Mon-Fri) | custom (every N days).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from psycopg.types.json import Json

from . import db, store, notifications
from .schedule import _GST

RECURRENCES = ("none", "daily", "weekly", "monthly", "weekday", "custom")

_SCHEMA = """
create table if not exists reminders (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  created_by text default 'rashad',
  company_id bigint,
  target_type text, target_id text,            -- contact|deal|project|account|task|calendar|none
  title text not null,
  due_at timestamptz not null,
  recurrence text not null default 'none',      -- none|daily|weekly|monthly|weekday|custom
  custom_days int,                              -- for recurrence='custom' (every N days)
  priority text not null default 'normal',
  action jsonb,                                 -- null = pure nudge; else {company, skill, kind, brief}
  status text not null default 'pending',       -- pending|fired|done|snoozed|cancelled
  snooze_until timestamptz,
  last_notification_id bigint,
  last_task_id bigint
);
create index if not exists reminders_due_idx on reminders (status, due_at);
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_SCHEMA)


def _add_month(d: datetime) -> datetime:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    day = min(d.day, 28)   # keep it simple/safe across month lengths
    return d.replace(year=y, month=m, day=day)


def next_due(due_at: datetime, recurrence: str, custom_days: int | None = None) -> datetime | None:
    """The next fire time for a recurring reminder, or None if it doesn't repeat."""
    if recurrence == "daily":
        return due_at + timedelta(days=1)
    if recurrence == "weekly":
        return due_at + timedelta(days=7)
    if recurrence == "monthly":
        return _add_month(due_at)
    if recurrence == "weekday":
        n = due_at + timedelta(days=1)
        while n.weekday() >= 5:      # skip Sat(5)/Sun(6)
            n += timedelta(days=1)
        return n
    if recurrence == "custom":
        return due_at + timedelta(days=max(1, custom_days or 1))
    return None


def parse_when(text: str) -> datetime | None:
    """Resolve a natural-language time phrase ('next Tuesday 10am', 'in 3 days', 'tomorrow evening')
    into a GST datetime, on the cheap router model. Defaults to 09:00 if no time is given."""
    from . import provider
    now = datetime.now(_GST)
    out = provider.think_json(
        "Convert a natural-language time phrase into an exact timestamp. The current date-time is "
        f"{now.strftime('%Y-%m-%d %H:%M')} on a {now.strftime('%A')} (GST, GMT+4). Resolve relative "
        "phrases against it; default the time to 09:00 if none is given; 'evening'=18:00, 'morning'=09:00, "
        'noon=12:00. Return {"iso":"YYYY-MM-DDTHH:MM:SS"} in GST, or {"iso":null} if you truly cannot tell.',
        text or "", model=provider.MODEL_ROUTER, max_tokens=60, purpose="reminder-parse")
    iso = (out or {}).get("iso")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=_GST) if dt.tzinfo is None else dt
    except Exception:  # noqa: BLE001
        return None


def create(title: str, due_at: datetime, *, company_id: int | None = None, target_type: str | None = None,
           target_id=None, recurrence: str = "none", custom_days: int | None = None,
           priority: str = "normal", action: dict | None = None, created_by: str = "rashad") -> dict:
    ensure_schema()
    recurrence = recurrence if recurrence in RECURRENCES else "none"
    tid = target_id if target_id is None else str(target_id)
    if tid is not None and recurrence == "none" and created_by != "cortex-pipeline":
        # pipeline commitment/deadline reminders are a tracked LEDGER (auto-settled by matching title
        # and creator) - they must never be merged away into a nudge
        # ONE reminder per target per day (audit follow-on: deal 103 accumulated SIX overlapping Monday
        # reminders from four mechanisms). Same target, same day -> the existing reminder absorbs it.
        ex = db.one("select * from reminders where status in ('pending','snoozed') and target_type=%s "
                    "and target_id=%s and due_at::date = %s::date limit 1",
                    (target_type, tid, due_at))
        if ex:
            if title and title.lower()[:40] not in (ex.get("title") or "").lower():
                db.execute("update reminders set title = left(title || ' + ' || %s, 400) where id=%s",
                           (title, ex["id"]))
            return ex
    return db.execute(
        "insert into reminders (title, due_at, company_id, target_type, target_id, recurrence, custom_days, "
        "priority, action, created_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (title, due_at, company_id, target_type, tid, recurrence, custom_days, priority,
         Json(action) if action else None, created_by))


def due() -> list[dict]:
    """Reminders ready to fire: pending & due, or snoozed past their snooze time."""
    ensure_schema()
    return db.query(
        "select * from reminders where due_at <= now() and "
        "(status='pending' or (status='snoozed' and (snooze_until is null or snooze_until <= now()))) "
        "order by due_at")


def fire(r: dict) -> dict:
    """Fire one reminder. ORDER MATTERS (audit): the row is advanced/closed FIRST, then the side effect
    runs — so a failure can never leave a past-due 'pending' row refiring every 60s. If the side effect
    then fails, we alert once instead of multiplying output."""
    nxt = next_due(r["due_at"], r.get("recurrence") or "none", r.get("custom_days"))
    if nxt is not None:                          # recurring -> queue the next fire
        db.execute("update reminders set due_at=%s, status='pending', snooze_until=null where id=%s",
                   (nxt, r["id"]))
    else:                                        # one-off -> done
        db.execute("update reminders set status='fired', snooze_until=null where id=%s", (r["id"],))
    action = r.get("action") or _commitment_action(r)
    note_id, task_id = None, None
    try:
        if action:                               # ACTION reminder -> spawn a normal task
            task_id = _spawn_task(r, action)
            if task_id is None and action:       # spawn failed -> the owner must know the work didn't start
                notifications.notify(f"Reminder \"{r['title']}\" fired but its task could not be created - "
                                     "set it again or run it by hand.", "Reminder action failed",
                                     priority="high", category="reminder", company_id=r.get("company_id"))
        else:                                    # NUDGE -> drop an info card pointing at the target
            n = notifications.notify(
                r["title"], "Reminder", priority=r.get("priority") or "normal", category="reminder",
                company_id=r.get("company_id"), target_type=r.get("target_type"), target_id=r.get("target_id"))
            note_id = n["id"]
    except Exception:  # noqa: BLE001 - the row is already advanced; never refires in a loop
        pass
    db.execute("update reminders set last_notification_id=%s, last_task_id=%s where id=%s",
               (note_id, task_id, r["id"]))
    return {"reminder_id": r["id"], "notification_id": note_id, "task_id": task_id}


def _spawn_task(r: dict, action: dict) -> int | None:
    """Turn an action reminder into a normal task in the standard pipeline (engine drafts it -> Inbox).
    If the company/skill can't be resolved, do NOT create a broken task — drop an info card instead."""
    try:
        co = store.get_company_by_slug(action.get("company") or "")
        sk = store.get_skill_by_key(co["id"], action.get("skill") or "") if co else None
        if not co or not sk:
            notifications.notify(
                "Reminder couldn't run automatically",
                f"\"{r['title']}\" — couldn't find the {'business' if not co else 'skill'} to draft it. "
                "Set it again with a valid skill, or as a plain reminder.",
                priority="normal", category="reminder", company_id=r.get("company_id"),
                target_type=r.get("target_type"), target_id=r.get("target_id"))
            return None
        kind = action.get("kind") or "content"
        brief = action.get("brief") or r["title"]
        # a full request on the action wins (e.g. a scheduled email draft carrying recipient + sender
        # mailbox); else the plain brief/title task as before
        req = action.get("request") or {"brief": brief, "title": r["title"]}
        if isinstance(req, dict):
            req = _with_deal(r, kind, req)
        contact = ((req.get("inquiry") or {}).get("email") if isinstance(req, dict) else None)
        t = store.create_card(co["id"], sk["id"], kind, req, contact=contact,
                              deal_id=(req.get("deal_id") if isinstance(req, dict) else None))
        return t["id"] if t else None
    except Exception:  # noqa: BLE001
        return None


_EMAIL_KINDS = ("email_reply", "email_draft")
_COMMITMENT_RX = re.compile(r"^Commitment owed to (\S+@\S+):\s*(.+?)\s*\(deal (\d+)\)", re.I)
# promises an email cannot keep on its own: the owner acts, the reminder stays a nudge
_NOT_BY_EMAIL = re.compile(r"\b(attend|on[- ]site|visit|be there|deliver the|shoot|film|record)\b", re.I)


def _commitment_action(r: dict) -> dict | None:
    """A DUE PROMISE BECOMES THE EMAIL THAT KEEPS IT (owner, 17 Sep 2026). "Schedule a Google Meet call"
    owed to Antoni fell due and Cortex only nudged the owner. A commitment reminder Cortex set from our own
    email now spawns the email that fulfils it (a call gets real slots proposed; a document gets sent),
    written with the deal behind it (_with_deal). Promises that need a person on site stay nudges."""
    if r.get("action") or str(r.get("created_by") or "") != "cortex-pipeline":
        return None
    m = _COMMITMENT_RX.match(r.get("title") or "")
    if not m or _NOT_BY_EMAIL.search(m.group(2)):
        return None
    co = store.get_company(r.get("company_id")) if r.get("company_id") else None
    if not co:
        return None
    email, what = m.group(1).strip().lower(), m.group(2).strip()
    return {"company": co.get("slug"), "skill": "sales-followup", "kind": "email_reply",
            "brief": (f"KEEP A PROMISE WE MADE: we told {email} we would \"{what}\" and it is now due. Write the email "
                      "that does exactly that (a call: propose specific slots from the availability list; a document or "
                      "answer: give it). Do not re-promise it, do not apologise at length, do not add a chase."),
            "request": {"brief": "", "title": r.get("title"), "deal_id": int(m.group(3)),
                        "inquiry": {"name": "", "email": email, "subject": ""}, "system_note": f"From commitment reminder #{r.get('id')}."}}


def _last_subject(deal: dict) -> str:
    """The subject of the latest email either way on the deal's timeline, so a reminder's email continues
    that conversation ('Re: ...') instead of opening a new one. '' when the timeline holds no email."""
    for h in reversed(deal.get("history") or []):
        ev, text = h.get("event") or "", h.get("text") or ""
        m = None
        if ev in ("email_out", "email_out_manual"):
            m = re.search(r"to \S+@\S+: (.+)$", text)
        elif ev == "email_in":
            m = re.search(r"^from \S+@\S+: (.+?)(?: - |$)", text)
        if m:
            subj = re.sub(r"^\s*((re|fwd|fw)\s*:\s*)+", "", m.group(1).strip(), flags=re.I).strip()
            return f"Re: {subj}" if subj else ""
    return ""


def _with_deal(r: dict, kind: str, req: dict) -> dict:
    """AN ACTION REMINDER ON A DEAL DRAFTS WITH THE DEAL (owner, 16 Sep 2026). Reminder 149 'check in on the
    RFP' on the ECBD tender fired as card 692 carrying only those four words: no recipient, no timeline, no
    deal, and the drafter rightly refused. The deal is the reminder's whole context: its primary contact is
    the recipient of an email kind, the latest email subject threads the reply, the timeline rides the
    brief, and the card is linked to the deal."""
    did = req.get("deal_id")
    if did is None and r.get("target_type") in ("deal", "project"):
        did = r.get("target_id")
    try:
        did = int(did)
    except (TypeError, ValueError):
        return req
    d = db.one("select * from crm_projects where id=%s", (did,))
    if not d:
        return req
    from . import pipeline   # lazy: pipeline imports this module
    req = dict(req)
    req["deal_id"] = did
    email = ""
    if kind in _EMAIL_KINDS and "@" not in ((req.get("inquiry") or {}).get("email") or ""):
        contacts = d.get("contacts") or []
        email = (d.get("contact_email")
                 or next((c.get("email") for c in contacts if c.get("primary") and c.get("email")), None)
                 or next((c.get("email") for c in contacts if c.get("email")), None) or "")
        if email:
            c = db.one("select first_name, last_name from crm_master where lower(email)=lower(%s)", (email,)) or {}
            name = (" ".join(x for x in (c.get("first_name"), c.get("last_name")) if x).strip()
                    or next((x.get("name") for x in contacts if (x.get("email") or "").lower() == email.lower()), "")
                    or "")
            subj = _last_subject(d)
            req["inquiry"] = {"name": name, "email": email, "subject": subj or (d.get("title") or r["title"])}
            if subj:
                req["thread_reply"] = True
    what = req.get("brief") or r["title"]
    req["brief"] = (f"REMINDER ACTION on opportunity #{did} '{d.get('title')}' (stage {d.get('stage')}): {what}."
                    + (f" Write it to {email} as the next step in this deal, picking up exactly where the "
                       "timeline leaves off; never re-introduce us or restate what they already know." if email else "")
                    + "\n\n" + (pipeline.deal_context(did, limit=40) or ""))
    req["system_note"] = f"From reminder #{r.get('id')} '{r.get('title')}' set on deal {did}."
    req["title"] = f"{r.get('title')} ({d.get('title')})"[:200]
    return req


def _closed_deal(r: dict) -> bool:
    """A reminder CORTEX set on a deal that has since closed (Lost, Dormant, Completed). The last check before
    anything fires: closing a deal cancels these, and this catches any that slip past (owner, 12 Sep 2026)."""
    if r.get("target_type") != "deal" or not str(r.get("created_by") or "").startswith("cortex"):
        return False
    try:
        d = db.one("select stage from crm_projects where id=%s", (int(r.get("target_id")),))
    except (TypeError, ValueError):
        return False
    from . import crm
    return bool(d) and d.get("stage") in crm.CLOSED_STAGES


def fire_due() -> dict:
    """Called from the engine 60s loop: fire every due reminder."""
    fired = []
    for r in due():
        try:
            if _closed_deal(r):          # dropped quietly: a closed deal owes nothing
                db.execute("update reminders set status='cancelled' where id=%s", (r["id"],))
                continue
            fire(r)
            fired.append(r["id"])
        except Exception:  # noqa: BLE001
            pass
    return {"fired": fired}


# ---- management ----

def listing(status: str | None = None, company_id: int | None = None, limit: int = 100) -> list[dict]:
    ensure_schema()
    where, params = [], []
    if status:
        where.append("status=%s"); params.append(status)
    if company_id is not None:
        cids = list(company_id) if isinstance(company_id, (list, tuple)) else [company_id]
        where.append("(company_id = any(%s) or company_id is null)"); params.append(cids)
    clause = (" where " + " and ".join(where)) if where else ""
    params.append(limit)
    return db.query(f"select * from reminders{clause} order by due_at limit %s", tuple(params))


def snooze(rid: int, until: datetime) -> dict | None:
    return db.execute("update reminders set status='snoozed', snooze_until=%s, due_at=%s where id=%s returning *",
                      (until, until, rid))


def mark_done(rid: int) -> dict | None:
    return db.execute("update reminders set status='done' where id=%s returning *", (rid,))


def cancel(rid: int) -> dict | None:
    return db.execute("update reminders set status='cancelled' where id=%s returning *", (rid,))
