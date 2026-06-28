"""Source-of-truth store for the social engine's CONFIG: per-account strategy + the anchor library + persona.

Cortex is the brain and the single source of truth. The runner (hands) READS these at run-time instead of
holding its own local JSON, so the strategy / anchors / personas live in ONE editable place (here), not
scattered across office machines. Editable from the cockpit later.
"""
from __future__ import annotations

from psycopg.types.json import Json

from . import db

_SCHEMA = """
create table if not exists social_accounts (
  account      text primary key,            -- runner account key: 'rashad', 'live' (Paul), ...
  platform     text default 'linkedin',
  company_id   bigint,
  persona_name text,
  person_key   text,                         -- links to company_profiles.data.voice.people.<key>
  role         text,
  cfg          jsonb not null default '{}'::jsonb,   -- caps, icp, goal, content_focus, flywheel_source, geo, ...
  persona      text,                          -- full persona / content-voice brief (was paul-persona.md)
  updated_at   timestamptz default now());
create table if not exists social_anchor_library (
  id        bigserial primary key,
  company_id bigint,
  account   text,                             -- which account harvests it (the split); null = unassigned pool
  name      text,
  url       text,
  platform  text default 'linkedin',
  segment   text,                             -- marketing | founders | creators
  audience  text,                             -- marketers | business-owners | creators | mixed
  status    text default 'queued',            -- live | queued | flag | hold | parked
  why       text,
  updated_at timestamptz default now(),
  unique(account, url));
"""


def ensure_schema():
    with db.connect() as c:
        c.execute(_SCHEMA)


def get_account(account: str) -> dict | None:
    """The account's full strategy/config (cfg fields flattened up), or None."""
    ensure_schema()
    r = db.one("select * from social_accounts where account=%s", (account,))
    if not r:
        return None
    out = dict(r.get("cfg") or {})
    out.update({"account": r["account"], "platform": r["platform"], "company_id": r["company_id"],
                "persona": r["persona_name"], "person_key": r["person_key"], "role": r["role"],
                "persona_brief": r["persona"]})
    return out


def set_account(account: str, *, platform: str = "linkedin", company_id=None, persona_name=None,
                person_key=None, role=None, cfg: dict | None = None, persona: str | None = None) -> None:
    ensure_schema()
    db.execute(
        """insert into social_accounts (account, platform, company_id, persona_name, person_key, role, cfg,
             persona, updated_at) values (%s,%s,%s,%s,%s,%s,%s,%s,now())
           on conflict (account) do update set platform=excluded.platform, company_id=excluded.company_id,
             persona_name=excluded.persona_name, person_key=excluded.person_key, role=excluded.role,
             cfg=excluded.cfg, persona=coalesce(excluded.persona, social_accounts.persona), updated_at=now()""",
        (account, platform, company_id, persona_name, person_key, role, Json(cfg or {}), persona))


def list_anchors(account: str, harvestable: bool = False) -> list[dict]:
    """The account's anchor slice. harvestable=True -> only status live|queued (what the harvest rotates)."""
    ensure_schema()
    q = "select * from social_anchor_library where account=%s"
    if harvestable:
        q += " and status in ('live','queued')"
    rows = db.query(q + " order by id", (account,))
    return [{"name": r["name"], "url": r["url"], "platform": r["platform"], "segment": r["segment"],
             "audience": r["audience"], "status": r["status"], "why": r["why"]} for r in rows]


def replace_anchors(account: str, company_id, anchors: list[dict]) -> int:
    """Replace an account's whole anchor slice (idempotent migration / cockpit edit). Returns count."""
    ensure_schema()
    db.execute("delete from social_anchor_library where account=%s", (account,))
    n = 0
    for a in anchors:
        if not a.get("url"):
            continue
        db.execute(
            """insert into social_anchor_library (company_id, account, name, url, platform, segment, audience,
                 status, why) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (account, url) do nothing""",
            (company_id, account, a.get("name"), a.get("url"), a.get("platform", "linkedin"),
             a.get("segment"), a.get("audience"), a.get("status", "queued"), a.get("why")))
        n += 1
    return n
