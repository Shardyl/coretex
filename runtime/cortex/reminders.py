"""Reminders — the TRIGGER layer (see CORTEX-TASKS-NOTIFICATIONS-MERGED-SPEC).

A reminder is a user-set, entity-attached, timed nudge. It is thin: on firing it EITHER emits a
notification (a pure nudge) OR spawns a normal task (an action reminder) which then flows through the
standard draft -> manager -> Inbox-approval pipeline. A reminder never needs approval itself.

Targets are polymorphic: contact:4821 / deal:77 / project:12 / account:9 / task:34 / none (freeform),
so one mechanism covers every entity. GST clock (reuse schedule._GST). Recurrence:
none | daily | weekly | monthly | weekday (Mon-Fri) | custom (every N days).
"""
from __future__ import annotations

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
    """Fire one reminder. Nudge -> a notification (info card). Action -> spawn a task (action card).
    Then reschedule (recurring) or close (one-off)."""
    action = r.get("action")
    note_id, task_id = None, None
    if action:                                   # ACTION reminder -> spawn a normal task
        task_id = _spawn_task(r, action)
    else:                                        # NUDGE -> drop an info card pointing at the target
        n = notifications.notify(
            r["title"], "Reminder", priority=r.get("priority") or "normal", category="reminder",
            company_id=r.get("company_id"), target_type=r.get("target_type"), target_id=r.get("target_id"))
        note_id = n["id"]

    nxt = next_due(r["due_at"], r.get("recurrence") or "none", r.get("custom_days"))
    if nxt is not None:                          # recurring -> queue the next fire
        db.execute("update reminders set due_at=%s, status='pending', snooze_until=null, "
                   "last_notification_id=%s, last_task_id=%s where id=%s",
                   (nxt, note_id, task_id, r["id"]))
    else:                                        # one-off -> done
        db.execute("update reminders set status='fired', snooze_until=null, "
                   "last_notification_id=%s, last_task_id=%s where id=%s",
                   (note_id, task_id, r["id"]))
    return {"reminder_id": r["id"], "notification_id": note_id, "task_id": task_id}


def _spawn_task(r: dict, action: dict) -> int | None:
    """Turn an action reminder into a normal task in the standard pipeline (engine drafts it -> Inbox)."""
    try:
        co = store.get_company_by_slug(action.get("company") or "")
        if not co:
            return None
        sk = store.get_skill_by_key(co["id"], action.get("skill") or "")
        kind = action.get("kind") or "content"
        brief = action.get("brief") or r["title"]
        t = store.create_task(co["id"], sk["id"] if sk else None, kind, {"brief": brief, "title": r["title"]})
        return t["id"] if t else None
    except Exception:  # noqa: BLE001
        return None


def fire_due() -> dict:
    """Called from the engine 60s loop: fire every due reminder."""
    fired = []
    for r in due():
        try:
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
        where.append("(company_id=%s or company_id is null)"); params.append(company_id)
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
