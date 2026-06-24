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


def notify(title: str, body: str = "", *, priority: str = "normal", category: str = "system",
           company_id: int | None = None, target_type: str | None = None, target_id=None,
           dedup_key: str | None = None, item: dict | None = None) -> dict:
    """Create + route ONE notification. If `dedup_key` matches an existing UNREAD row, coalesce into it
    (bumps count + refreshes title/body/time) instead of adding another — this is the FYI grouping. `item`
    (a small dict, e.g. a captured contact) is accumulated into the row's `items` so the card can expand."""
    ensure_schema()
    priority = priority if priority in PRIORITIES else "normal"
    tid = target_id if target_id is None else str(target_id)

    if dedup_key:
        existing = db.one("select * from notifications where dedup_key=%s and state='unread' "
                          "order by id desc limit 1", (dedup_key,))
        if existing:
            items = (existing.get("items") or [])
            if item:
                items = (items + [item])[-50:]   # keep the most recent 50 in one rolling card
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
