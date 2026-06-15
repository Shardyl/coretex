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
                 category=None, department=None, manager=None, model=None) -> dict:
    return db.execute(
        "insert into skills (company_id,skill_key,name,craft,authority,stakes,auto_threshold,"
        "category,department,manager,model) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (company_id,skill_key) do update set name=excluded.name, craft=excluded.craft, "
        "stakes=excluded.stakes, category=excluded.category, department=excluded.department, "
        "manager=excluded.manager, model=excluded.model returning *",
        (company_id, key, name, craft, authority, stakes, auto_threshold, category, department, manager, model),
    )


def add_rule(skill_id, rule: str) -> dict:
    return db.execute(
        "update skills set rules = rules || %s::jsonb, updated_at=now() where id=%s returning *",
        (Json([rule]), skill_id),
    )


# ---- universal rules (apply to every company for a skill_key) ----
def get_universal_rules(skill_key: str) -> list:
    row = db.one("select rules from universal_skill_rules where skill_key=%s", (skill_key,))
    return (row["rules"] if row else []) or []


def add_universal_rule(skill_key: str, rule: str) -> list:
    rules = get_universal_rules(skill_key) + [rule]
    db.execute("insert into universal_skill_rules (skill_key, rules) values (%s, %s) "
               "on conflict (skill_key) do update set rules=%s, updated_at=now()",
               (skill_key, Json(rules), Json(rules)))
    return rules


def remove_universal_rule(skill_key: str, index: int) -> list:
    rules = get_universal_rules(skill_key)
    if 0 <= index < len(rules):
        rules.pop(index)
    db.execute("insert into universal_skill_rules (skill_key, rules) values (%s, %s) "
               "on conflict (skill_key) do update set rules=%s, updated_at=now()",
               (skill_key, Json(rules), Json(rules)))
    return rules


def bump_streak(skill_id, by=1) -> dict:
    return db.execute(
        "update skills set trust_streak=trust_streak+%s, updated_at=now() where id=%s returning *",
        (by, skill_id),
    )


def effective_rules(skill: dict) -> tuple[list, list]:
    """The rules a company actually follows for a skill: (universal minus this company's overrides, local).
    An override = a local rule explicitly supersedes a universal one for this company only."""
    uni = get_universal_rules(skill.get("skill_key", "")) if skill.get("skill_key") else []
    ov = skill.get("overrides") or []
    uni = [r for r in uni if r not in ov]
    return list(uni), list(skill.get("rules") or [])


def add_override(skill_id, universal_rule: str) -> dict:
    """Mark a universal rule as superseded for this company's skill (drops it from that company)."""
    return db.execute(
        "update skills set overrides = overrides || %s::jsonb, updated_at=now() where id=%s returning *",
        (Json([universal_rule]), skill_id),
    )


def reset_streak(skill_id) -> dict:
    """A correction or rejection breaks the clean-approval streak (manager and owner disagreed)."""
    return db.execute("update skills set trust_streak=0, updated_at=now() where id=%s returning *",
                      (skill_id,))


def set_authority(skill_id, authority) -> dict:
    return db.execute("update skills set authority=%s, updated_at=now() where id=%s returning *",
                      (authority, skill_id))


def set_threshold(skill_id, n: int) -> dict:
    """Raise/lower how many clean approvals in a row are needed before auto is offered."""
    return db.execute("update skills set auto_threshold=%s, updated_at=now() where id=%s returning *",
                      (max(1, int(n)), skill_id))


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


# ---- conversations (Talk history) ----
def conv_list(limit=60) -> list[dict]:
    return db.query("select id, title, company, updated_at from conversations order by updated_at desc limit %s", (limit,))


def conv_get(cid: int) -> dict | None:
    return db.one("select * from conversations where id=%s", (cid,))


def conv_create(title="New chat", company=None) -> dict:
    return db.execute("insert into conversations (title, company) values (%s,%s) returning *", (title, company))


def conv_save(cid: int, messages: list, title: str | None = None) -> dict | None:
    if title:
        return db.execute("update conversations set messages=%s, title=%s, updated_at=now() where id=%s returning *",
                          (Json(messages), title, cid))
    return db.execute("update conversations set messages=%s, updated_at=now() where id=%s returning *",
                      (Json(messages), cid))


def conv_delete(cid: int) -> None:
    db.execute("delete from conversations where id=%s", (cid,))


# ---- decisions ----
def log_decision(task_id, skill_id, actor, action, note=None, snapshot=None) -> dict:
    return db.execute(
        "insert into decisions (task_id,skill_id,actor,action,note,snapshot) "
        "values (%s,%s,%s,%s,%s,%s) returning *",
        (task_id, skill_id, actor, action, note, Json(snapshot) if snapshot is not None else None),
    )
