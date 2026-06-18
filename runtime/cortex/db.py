"""Postgres access for Cortex (psycopg 3, sync, autocommit).

Simple by design: short-lived connections, dict rows. The engine is a polling
loop, so we don't need a pool or async here.
"""
from __future__ import annotations

import pathlib

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from . import config

SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql"


# Cortex's standard timezone (GST / Asia/Dubai, GMT+4, no DST). Set on every DB session so now(), ts::date,
# date_trunc, "today", by_day grouping etc. are all GST — not UTC. Timestamps are still stored as UTC
# internally (timestamptz); this only changes how they're computed/displayed.
TZ = config.get("CORTEX_TZ", "Asia/Dubai")


def connect() -> psycopg.Connection:
    return psycopg.connect(config.require("DATABASE_URL"), row_factory=dict_row, autocommit=True,
                           options=f"-c timezone={TZ}")


# columns added after the first schema shipped — applied to existing DBs on migrate.
_ALTERS = [
    "alter table skills add column if not exists category text",
    "alter table skills add column if not exists department text",
    "alter table skills add column if not exists manager text",
    "alter table skills add column if not exists model text",
    "alter table skills add column if not exists overrides jsonb not null default '[]'::jsonb",
    # Phase 3 — unified task entity: scheduling + provenance (all nullable/defaulted, so existing rows
    # become immediate tasks; merges scheduled_tasks into tasks on one timeline). See merged spec §3a.
    "alter table tasks add column if not exists origin        text",            # talk|calendar|incoming|system
    "alter table tasks add column if not exists title         text",
    "alter table tasks add column if not exists schedule_kind text",            # null=now | once | recurring
    "alter table tasks add column if not exists cadence       text",            # daily|weekly|monthly
    "alter table tasks add column if not exists weekday       int",             # 0=Mon..6=Sun
    "alter table tasks add column if not exists hour          int",
    "alter table tasks add column if not exists minute        int",
    "alter table tasks add column if not exists run_at        timestamptz",     # one-off fire time
    "alter table tasks add column if not exists next_run      timestamptz",     # recurring next fire
    "alter table tasks add column if not exists last_run      timestamptz",
    "alter table tasks add column if not exists last_status   text",
    "alter table tasks add column if not exists enabled       boolean default true",
    "alter table tasks add column if not exists parent_id     bigint references tasks(id)",
]


def migrate() -> None:
    """Apply db/schema.sql + incremental column adds (idempotent)."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect() as conn:
        conn.execute(sql)
        for stmt in _ALTERS:
            conn.execute(stmt)


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> dict | None:
    """Run a statement; returns the first row if the statement RETURNs, else None."""
    with connect() as conn:
        cur = conn.execute(sql, params)
        if cur.description:
            return cur.fetchone()
        return None


def setting_get(key: str, default=None):
    row = one("select value from settings where key = %s", (key,))
    return row["value"] if row else default


def setting_set(key: str, value) -> None:
    execute(
        "insert into settings (key, value) values (%s, %s) "
        "on conflict (key) do update set value = excluded.value",
        (key, Json(value)),
    )
