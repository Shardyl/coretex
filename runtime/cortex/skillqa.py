"""Per-skill questionnaires — the expert "what it handles" + granular question set for each skill.

The questions are UNIVERSAL (same for a skill_key across every company); the ANSWERS are per-company.
Each question is a doorway into a Talk-mode conversation with that area's Manager; the outcome of that
conversation is either LOCKED IN as a rule on the company's skill, or PARKED to the ideas parking lot.
"""
from __future__ import annotations

from psycopg.types.json import Json

from . import db, store

_SCHEMA = """
create table if not exists skill_questionnaires (
  skill_key   text primary key,
  explanation text not null default '',
  questions   jsonb not null default '[]'::jsonb,
  updated_at  timestamptz not null default now());

create table if not exists questionnaire_progress (
  company_id  bigint references companies(id) on delete cascade,
  skill_key   text,
  q_idx       int,
  status      text not null default 'open',     -- open | decided | parked
  conversation_id text,
  note        text,
  updated_at  timestamptz not null default now(),
  primary key (company_id, skill_key, q_idx));
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_SCHEMA)


def upsert(skill_key: str, explanation: str, questions: list) -> None:
    ensure_schema()
    db.execute(
        "insert into skill_questionnaires (skill_key, explanation, questions) values (%s,%s,%s) "
        "on conflict (skill_key) do update set explanation=excluded.explanation, "
        "questions=excluded.questions, updated_at=now()",
        (skill_key, explanation or "", Json(questions or [])))


def get(skill_key: str) -> dict | None:
    ensure_schema()
    return db.one("select * from skill_questionnaires where skill_key=%s", (skill_key,))


def has_any() -> int:
    ensure_schema()
    return db.one("select count(*) n from skill_questionnaires")["n"]


def progress(company_id: int, skill_key: str) -> dict:
    ensure_schema()
    rows = db.query("select q_idx, status, conversation_id, note from questionnaire_progress "
                    "where company_id=%s and skill_key=%s", (company_id, skill_key))
    return {r["q_idx"]: {"status": r["status"], "conversation_id": r["conversation_id"],
                         "note": r["note"]} for r in rows}


def set_status(company_id: int, skill_key: str, q_idx: int, status: str,
               conversation_id: str | None = None, note: str | None = None) -> None:
    ensure_schema()
    db.execute(
        "insert into questionnaire_progress (company_id, skill_key, q_idx, status, conversation_id, note) "
        "values (%s,%s,%s,%s,%s,%s) on conflict (company_id, skill_key, q_idx) do update set "
        "status=excluded.status, conversation_id=coalesce(excluded.conversation_id, questionnaire_progress.conversation_id), "
        "note=coalesce(excluded.note, questionnaire_progress.note), updated_at=now()",
        (company_id, skill_key, q_idx, status, conversation_id, note))


def lock_in(company_id: int, skill_key: str, q_idx: int, rule: str, conversation_id: str | None = None) -> dict:
    """Bake a decided answer into the company's skill as a standing rule, and mark the question decided."""
    skill = store.get_skill_by_key(company_id, skill_key)
    if not skill:
        return {"ok": False, "error": "no such skill for this company"}
    store.add_rule(skill["id"], rule)
    set_status(company_id, skill_key, q_idx, "decided", conversation_id, note=rule)
    return {"ok": True, "rule": rule}


def park(company_id: int, skill_key: str, q_idx: int, idea: str, question: str = "",
         conversation_id: str | None = None) -> dict:
    """Send an undecided idea to the roadmap / ideas parking lot, tagged to the skill it came from."""
    lot = store.get_skill_by_key(company_id, "roadmap-ideas-parking-lot")
    tagged = f"[from {skill_key}] {question + ' — ' if question else ''}{idea}".strip()
    if lot:
        store.add_rule(lot["id"], tagged)
    set_status(company_id, skill_key, q_idx, "parked", conversation_id, note=idea)
    return {"ok": True, "parked": tagged}
