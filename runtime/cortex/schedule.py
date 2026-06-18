"""Scheduled tasks — recurring jobs Cortex runs on a cadence (e.g. the weekly per-company SEO report).

The engine loop checks `due()` each minute, runs the task, drops the result into the Cortex Inbox, and reschedules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Json

from . import db

_GST = timezone(timedelta(hours=4))   # Cortex standard time: GST (GMT+4, no DST) — schedules fire on GST clock

_SCHEMA = """
create table if not exists scheduled_tasks (
  id bigserial primary key,
  company text, kind text not null, title text,
  cadence text not null default 'weekly',   -- daily | weekly | monthly
  weekday int default 0,                     -- 0=Mon .. 6=Sun (weekly)
  hour int default 8, minute int default 0,
  config jsonb not null default '{}'::jsonb,
  enabled boolean not null default true,
  next_run timestamptz, last_run timestamptz, last_status text,
  created_at timestamptz default now());
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_SCHEMA)


def next_run(cadence: str, weekday: int, hour: int, minute: int, after=None) -> datetime:
    now = after or datetime.now(_GST)
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cadence == "daily":
        return t if t > now else t + timedelta(days=1)
    if cadence == "monthly":
        return t + timedelta(days=30) if t <= now else t
    days = (weekday - t.weekday()) % 7        # weekly (default)
    t += timedelta(days=days)
    return t if t > now else t + timedelta(days=7)


def create(company, kind, title, cadence="weekly", weekday=0, hour=8, minute=0, config=None) -> dict:
    ensure_schema()
    nr = next_run(cadence, weekday, hour, minute)
    return db.execute(
        "insert into scheduled_tasks (company,kind,title,cadence,weekday,hour,minute,config,next_run) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (company, kind, title, cadence, weekday, hour, minute, Json(config or {}), nr))


def first_of_next_month(after: datetime, hour: int = 9) -> datetime:
    y, m = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
    return after.replace(year=y, month=m, day=1, hour=hour, minute=0, second=0, microsecond=0)


def next_monthly_slot(company: str, kind: str = "newsletter", hour: int = 9) -> datetime:
    """Next free 1st-of-month for a company's scheduled newsletters, so approved drafts STACK one per
    month: if the company already has one scheduled, take the 1st of the month after its latest; else the
    next upcoming 1st."""
    ensure_schema()
    now = datetime.now(_GST)
    rows = db.query("select next_run from scheduled_tasks where company=%s and kind=%s and enabled=true "
                    "and next_run is not null order by next_run desc limit 1", (company, kind))
    if rows and rows[0]["next_run"] and rows[0]["next_run"] > now:
        return first_of_next_month(rows[0]["next_run"], hour)
    first_this = now.replace(day=1, hour=hour, minute=0, second=0, microsecond=0)
    return first_this if first_this > now else first_of_next_month(now, hour)


def create_once(company, kind, title, when: datetime, config=None) -> dict:
    """A ONE-OFF scheduled task that fires at `when`, then disables itself (mark_ran handles 'once')."""
    ensure_schema()
    return db.execute(
        "insert into scheduled_tasks (company,kind,title,cadence,next_run,config) "
        "values (%s,%s,%s,'once',%s,%s) returning *",
        (company, kind, title, when, Json(config or {})))


def listing(company=None) -> list:
    ensure_schema()
    if company and company not in ("all", ""):
        return db.query("select * from scheduled_tasks where company=%s order by next_run nulls last", (company,))
    return db.query("select * from scheduled_tasks order by next_run nulls last")


def toggle(tid, enabled) -> dict:
    return db.execute("update scheduled_tasks set enabled=%s where id=%s returning *", (bool(enabled), tid))


def delete(tid) -> None:
    db.execute("delete from scheduled_tasks where id=%s", (tid,))


def run_now(tid) -> None:
    db.execute("update scheduled_tasks set next_run=now() where id=%s", (tid,))


def due() -> list:
    ensure_schema()
    return db.query("select * from scheduled_tasks where enabled=true and next_run is not null and next_run <= now()")


def mark_ran(t: dict, status: str) -> None:
    if t["cadence"] == "once":   # one-off (e.g. a scheduled newsletter): fire once, then disable
        db.execute("update scheduled_tasks set last_run=now(), last_status=%s, enabled=false, next_run=null "
                   "where id=%s", (status[:120], t["id"]))
        return
    nr = next_run(t["cadence"], t["weekday"] or 0, t["hour"] or 8, t["minute"] or 0)
    db.execute("update scheduled_tasks set last_run=now(), last_status=%s, next_run=%s where id=%s",
               (status[:120], nr, t["id"]))
