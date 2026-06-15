"""Data access for Cortex entities (companies, skills, tasks, decisions)."""
from __future__ import annotations

from psycopg.types.json import Json

from . import db


# ---- companies ----
def get_company(cid: int) -> dict | None:
    return db.one("select * from companies where id=%s", (cid,))


def get_company_by_slug(slug: str) -> dict | None:
    return db.one("select * from companies where slug=%s", (slug,))


def upsert_company(slug, name, kind="owned", context=None, north_star=None) -> dict:
    return db.execute(
        "insert into companies (slug,name,kind,context,north_star) values (%s,%s,%s,%s,%s) "
        "on conflict (slug) do update set name=excluded.name, kind=excluded.kind, "
        "context=excluded.context, north_star=excluded.north_star returning *",
        (slug, name, kind, Json(context or {}), north_star),
    )


# ---- skills ----
def get_skill(sid: int) -> dict | None:
    return db.one("select * from skills where id=%s", (sid,))


def get_skill_by_key(company_id, key) -> dict | None:
    return db.one("select * from skills where company_id=%s and skill_key=%s", (company_id, key))


def upsert_skill(company_id, key, name, craft, authority="ask", stakes="low", auto_threshold=10,
                 category=None, department=None, manager=None) -> dict:
    return db.execute(
        "insert into skills (company_id,skill_key,name,craft,authority,stakes,auto_threshold,"
        "category,department,manager) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (company_id,skill_key) do update set name=excluded.name, craft=excluded.craft, "
        "stakes=excluded.stakes, category=excluded.category, department=excluded.department, "
        "manager=excluded.manager returning *",
        (company_id, key, name, craft, authority, stakes, auto_threshold, category, department, manager),
    )


def add_rule(skill_id, rule: str) -> dict:
    return db.execute(
        "update skills set rules = rules || %s::jsonb, updated_at=now() where id=%s returning *",
        (Json([rule]), skill_id),
    )


def bump_streak(skill_id, by=1) -> dict:
    return db.execute(
        "update skills set trust_streak=trust_streak+%s, updated_at=now() where id=%s returning *",
        (by, skill_id),
    )


def set_authority(skill_id, authority) -> dict:
    return db.execute("update skills set authority=%s, updated_at=now() where id=%s returning *",
                      (authority, skill_id))


# ---- tasks ----
def create_task(company_id, skill_id, kind, request) -> dict:
    return db.execute(
        "insert into tasks (company_id,skill_id,kind,request) values (%s,%s,%s,%s) returning *",
        (company_id, skill_id, kind, Json(request)),
    )


def get_task(tid: int) -> dict | None:
    return db.one("select * from tasks where id=%s", (tid,))


def update_task(tid: int, **fields) -> dict | None:
    if not fields:
        return get_task(tid)
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k}=%s")
        vals.append(Json(v) if isinstance(v, (dict, list)) else v)
    vals.append(tid)
    return db.execute(f"update tasks set {', '.join(sets)}, updated_at=now() where id=%s returning *", tuple(vals))


def tasks_by_status(status: str) -> list[dict]:
    return db.query("select * from tasks where status=%s order by id", (status,))


# ---- decisions ----
def log_decision(task_id, skill_id, actor, action, note=None, snapshot=None) -> dict:
    return db.execute(
        "insert into decisions (task_id,skill_id,actor,action,note,snapshot) "
        "values (%s,%s,%s,%s,%s,%s) returning *",
        (task_id, skill_id, actor, action, note, Json(snapshot) if snapshot is not None else None),
    )
