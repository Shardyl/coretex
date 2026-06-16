"""Scheduled tasks — recurring jobs Cortex runs on a cadence (e.g. the weekly per-company SEO report).

The engine loop checks `due()` each minute, runs the task, drops the result into the Cortex Inbox, and reschedules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Json

from . import db

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
    now = after or datetime.now(timezone.utc)
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
    nr = next_run(t["cadence"], t["weekday"] or 0, t["hour"] or 8, t["minute"] or 0)
    db.execute("update scheduled_tasks set last_run=now(), last_status=%s, next_run=%s where id=%s",
               (status[:120], nr, t["id"]))
