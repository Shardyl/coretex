"""Notifications — the SIGNAL layer (see CORTEX-TASKS-NOTIFICATIONS-MERGED-SPEC).

A notification is the atomic "tell Rashad something" unit: one row, one table. It is NOT a task —
tasks are their own Inbox cards, read straight from `tasks`; this table holds ONLY the things that
aren't tasks (leads captured, report ready, fired nudges, system/health, auto-action receipts).

`notify()` is the single entry point every source calls, so priority -> channel routing, dedup and
read-state all live in one place. Channels: in-app Inbox (always, this row), Telegram (critical mirror
only), Web Push (phone lock screen — wired in phase 4). The Inbox UI unions these rows (info cards)
with open tasks (action cards).
"""
from __future__ import annotations

from psycopg.types.json import Json

from . import db
from .integrations import telegram as tg

# priority -> which channels fire. in-app is always on (the row itself).
PRIORITIES = ("critical", "normal", "fyi")

_SCHEMA = """
create table if not exists notifications (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  fired_at   timestamptz not null default now(),
  title text not null,
  body  text,
  priority text not null default 'normal',     -- critical | normal | fyi
  category text not null default 'system',     -- reminder|report|due|system|receipt|security|lead|...
  company_id bigint,
  target_type text, target_id text,            -- deep link back to a record
  channels jsonb not null default '{}'::jsonb,  -- where it was sent
  state text not null default 'unread',         -- unread | read | dismissed | snoozed
  snooze_until timestamptz,
  dedup_key text,                               -- coalesce repeats / group FYI (e.g. lead:tabscanner:2026-06-18)
  count int not null default 1                  -- how many coalesced into this one
);
create index if not exists notifications_state_idx on notifications (state, fired_at desc);
create index if not exists notifications_dedup_idx on notifications (dedup_key) where state = 'unread';
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_SCHEMA)


def _push(notif: dict) -> bool:
    """Send a Web Push to subscribed devices. No-op until phase 4 wires push_subscriptions."""
    try:
        from . import push  # phase 4
        return push.send_to_devices(notif)
    except Exception:  # noqa: BLE001 — module not present yet / no subscriptions
        return False


# THE NOTIFICATION POLICY (owner, 13 Oct 2026: "I'm getting a lot ... it's a bit of a minefield", 454 notices in 30
# days, ~15 a day, before an Instantly campaign makes it busier). Every notice gets one treatment, decided HERE:
#   KEEP    something that needs the owner and has no card: one live notice per deal and topic (repeats coalesce)
#   COVERED a notice about a card that is already open in the Inbox: the card IS the notification (a phone ping only)
#   DIGEST  for-information only: rolled into ONE daily summary per company, each item listed inside it
# Critical notices (security, phishing) are always KEEP. Add a title prefix below to change a notice's treatment.
DIGEST_PREFIXES = ("New opportunity", "New ", "WhatsApp filed", "No reply drafted to", "Internal links seeded",
                   "Untracked sales email", "Card #", "Creative proposal ready", "Follow-up on '")
DIGEST_CATEGORIES = ("contact", "registration")


def _topic(title: str) -> str:
    """The kind of notice, from its title: 'Client deadline 2026-10-14T15:00: X' -> 'client-deadline'."""
    import re as _re
    head = _re.split(r"[:\u2014(]", title or "", 1)[0]
    head = " ".join(w for w in head.split() if not any(ch.isdigit() for ch in w))   # dates/times/ids are not topics
    return _re.sub(r"[^a-z]+", "-", head.lower()).strip("-")[:40] or "notice"


def _treatment(title: str, priority: str, category: str, target_type, target_id) -> str:
    if priority == "critical" or category in ("security",):
        return "keep"
    if target_type == "task" and str(target_id or "").isdigit():
        t = db.one("select status from tasks where id=%s", (int(target_id),))
        if t and t["status"] in ("new", "drafting", "queued", "awaiting_approval", "awaiting_correction"):
            return "covered"
    if category in DIGEST_CATEGORIES or any((title or "").startswith(p) for p in DIGEST_PREFIXES):
        return "digest"
    return "keep"


def notify(title: str, body: str = "", *, priority: str = "normal", category: str = "system",
           company_id: int | None = None, target_type: str | None = None, target_id=None,
           dedup_key: str | None = None, item: dict | None = None) -> dict:
    """Create + route ONE notification, under the NOTIFICATION POLICY above. If `dedup_key` matches an existing
    UNREAD row, coalesce into it (bumps count + refreshes title/body/time) instead of adding another. `item`
    (a small dict, e.g. a captured contact) is accumulated into the row's `items` so the card can expand."""
    ensure_schema()
    priority = priority if priority in PRIORITIES else "normal"
    tid = target_id if target_id is None else str(target_id)
    how = _treatment(title, priority, category, target_type, tid)
    if how == "covered":     # the card is already in the Inbox: ping the phone, no second row to drift
        push_only(title, body, url=f"/#task/{tid}", category=category)
        return {"id": None, "title": title, "treatment": "covered"}
    if how == "digest":      # one daily summary per company; this notice becomes a line inside it
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).date().isoformat()
        item = {"cat": category, "name": (title + (f": {body}" if body else ""))[:220],
                **({k: v for k, v in (item or {}).items() if k in ("email", "phone")})}
        dedup_key = f"digest:{company_id or 0}:{day}"
        priority, category = "fyi", "digest"
        title, body = "Today's updates", "For information: contacts captured, filed messages, new opportunities."
        target_type = tid = None
    elif target_type == "deal" and tid and not dedup_key:
        dedup_key = f"deal:{tid}:{_topic(title)}"     # one live notice per deal and topic

    if dedup_key:
        existing = db.one("select * from notifications where dedup_key=%s and state='unread' "
                          "order by id desc limit 1", (dedup_key,))
        if existing:
            items = (existing.get("items") or [])
            if not item and existing.get("title") != title and not dedup_key.startswith("digest:"):
                # one notice per deal and topic: a DIFFERENT earlier notice on it is kept as a line, never lost
                if not items:
                    items = [{"name": (existing.get("title") or "")[:220]}]
                item = {"name": (title or "")[:220]}
            if item:
                items = (items + [item])[-50:]   # keep the most recent 50 in one rolling card
            if dedup_key.startswith("digest:"):
                title = f"Today's updates ({len(items)})"
            row = db.execute("update notifications set count=count+1, title=%s, body=%s, items=%s::jsonb, "
                             "fired_at=now() where id=%s returning *",
                             (title, body, Json(items), existing["id"]))
            _route(row, priority)
            return row

    row = db.execute(
        "insert into notifications (title, body, priority, category, company_id, target_type, target_id, "
        "dedup_key, items) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (title, body, priority, category, company_id, target_type, tid, dedup_key, Json([item] if item else [])))
    _route(row, priority)
    return row


def _route(row: dict, priority: str) -> None:
    """Apply channel routing: critical -> Telegram mirror; normal/critical -> push (phone)."""
    sent = {"inapp": True}
    try:
        if priority == "critical":
            tg.send(f"⚠ {row['title']}" + (f"\n{row['body']}" if row.get("body") else ""))
            sent["telegram"] = True
        if priority in ("critical", "normal"):
            sent["push"] = _push(row)
    except Exception:  # noqa: BLE001 — routing must never break the caller
        pass
    try:
        db.execute("update notifications set channels=%s::jsonb where id=%s",
                   (__import__("json").dumps(sent), row["id"]))
    except Exception:  # noqa: BLE001
        pass


# ---- reads for the Inbox ----

def push_only(title: str, body: str = "", url: str = "/", category: str = "approval") -> bool:
    """Fire a Web Push WITHOUT persisting a row. For things that are ALREADY their own Inbox card (a task
    needing approval) — the task is the source of truth; this is just the instant lock-screen ping, so we
    keep the no-mirror rule (no duplicate notification row to drift)."""
    try:
        from . import push
        return push.send_to_devices({"id": 0, "title": title, "body": body, "category": category, "url": url})
    except Exception:  # noqa: BLE001
        return False


def _cid_filter(company_id):
    """Accept a single company_id OR a list (multi-company scope). Returns (sql, params) or ('', [])."""
    if company_id is None:
        return "", []
    cids = list(company_id) if isinstance(company_id, (list, tuple)) else [company_id]
    return " and (company_id = any(%s) or company_id is null)", [cids]


def active(company_id=None) -> list[dict]:
    """Live info cards for the Inbox: unread + snoozed-now-due. Newest first. company_id may be a list (scope)."""
    ensure_schema()
    where = "(state='unread' or (state='snoozed' and (snooze_until is null or snooze_until <= now())))"
    f, params = _cid_filter(company_id)
    return db.query(f"select * from notifications where {where}{f} order by fired_at desc", tuple(params))


def history(company_id: int | None = None, limit: int = 80) -> list[dict]:
    ensure_schema()
    where = "state in ('read','dismissed')"
    params: list = []
    if company_id is not None:
        where += " and (company_id = %s or company_id is null)"
        params.append(company_id)
    params.append(limit)
    return db.query(f"select * from notifications where {where} order by fired_at desc limit %s", tuple(params))


def unread_count(company_id=None) -> int:
    ensure_schema()
    f, params = _cid_filter(company_id)
    r = db.one(f"select count(*) n from notifications where state='unread'{f}", tuple(params))
    return int(r["n"]) if r else 0


def set_state(nid: int, state: str, snooze_until=None) -> dict | None:
    if state == "snoozed":
        return db.execute("update notifications set state='snoozed', snooze_until=%s where id=%s returning *",
                          (snooze_until, nid))
    return db.execute("update notifications set state=%s, snooze_until=null where id=%s returning *", (state, nid))
