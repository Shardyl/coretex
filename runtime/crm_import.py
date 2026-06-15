"""Import the Bitrix export (downloaded to /opt/cortex-bitrix/) into a multi-company CRM on Hetzner.

Every record is tagged with `sensa_company` = which of OUR businesses it belongs to (best-effort, from
the Bitrix source/category text), keeps the full raw record in `data jsonb`, and the 157 webhook-loop
contacts are flagged `verified=false` (Rashad is replacing them). Idempotent: re-running upserts.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/opt/coretex/runtime")
from psycopg.types.json import Json  # noqa: E402

from cortex import db  # noqa: E402

SRC = "/opt/cortex-bitrix"
BIZ = {"tabscanner": ["tabscan"], "sensa": ["sensa"], "skyvision": ["skyvision", "sky vision"],
       "filmspoke": ["filmspoke"]}

SCHEMA = """
create table if not exists crm_companies (
  bitrix_id bigint primary key, sensa_company text, title text, industry text, revenue text,
  employees text, is_my_company boolean, email text, phone text, created timestamptz,
  data jsonb, imported_at timestamptz not null default now());
create table if not exists crm_contacts (
  bitrix_id bigint primary key, sensa_company text, name text, email text, phone text, post text,
  company_bitrix_id bigint, lead_id bigint, source text, source_desc text,
  verified boolean not null default false, created timestamptz,
  data jsonb, imported_at timestamptz not null default now());
create table if not exists crm_deals (
  bitrix_id bigint primary key, sensa_company text, title text, category_id text, stage_id text,
  opportunity numeric, currency text, company_bitrix_id bigint, contact_bitrix_id bigint,
  source text, source_desc text, created timestamptz, closed_at timestamptz,
  data jsonb, imported_at timestamptz not null default now());
create table if not exists crm_activities (
  bitrix_id bigint primary key, subject text, type_id text, direction text, completed boolean,
  owner_id bigint, owner_type_id text, created timestamptz,
  data jsonb, imported_at timestamptz not null default now());
"""


def biz_of(*texts) -> str | None:
    blob = " ".join(str(t) for t in texts if t).lower()
    for slug, keys in BIZ.items():
        if any(k in blob for k in keys):
            return slug
    return None


def first_val(rec, key):
    v = rec.get(key)
    return v[0].get("VALUE") if isinstance(v, list) and v and isinstance(v[0], dict) else None


def load(name):
    p = os.path.join(SRC, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def main():
    with db.connect() as conn:
        conn.execute(SCHEMA)

    companies = load("companies.json")
    for c in companies:
        db.execute(
            "insert into crm_companies (bitrix_id,sensa_company,title,industry,revenue,employees,"
            "is_my_company,email,phone,created,data) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (bitrix_id) do update set sensa_company=excluded.sensa_company,title=excluded.title,"
            "industry=excluded.industry,is_my_company=excluded.is_my_company,data=excluded.data,imported_at=now()",
            (_int(c.get("ID")), biz_of(c.get("TITLE")), c.get("TITLE"), c.get("INDUSTRY"), c.get("REVENUE"),
             c.get("EMPLOYEES"), c.get("IS_MY_COMPANY") == "Y", first_val(c, "EMAIL"), first_val(c, "PHONE"),
             c.get("DATE_CREATE") or None, Json(c)))

    contacts = load("contacts.json")
    for c in contacts:
        name = " ".join(x for x in (c.get("NAME"), c.get("SECOND_NAME"), c.get("LAST_NAME")) if x).strip()
        email = first_val(c, "EMAIL")
        db.execute(
            "insert into crm_contacts (bitrix_id,sensa_company,name,email,phone,post,company_bitrix_id,"
            "lead_id,source,source_desc,verified,created,data) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (bitrix_id) do update set sensa_company=excluded.sensa_company,name=excluded.name,"
            "email=excluded.email,source=excluded.source,data=excluded.data,imported_at=now()",
            (_int(c.get("ID")), biz_of(c.get("SOURCE_ID"), c.get("SOURCE_DESCRIPTION"), email),
             name, email, first_val(c, "PHONE"), c.get("POST"), _int(c.get("COMPANY_ID")), _int(c.get("LEAD_ID")),
             c.get("SOURCE_ID"), c.get("SOURCE_DESCRIPTION"), False, c.get("DATE_CREATE") or None, Json(c)))

    deals = load("deals.json")
    for d in deals:
        try:
            opp = float(d.get("OPPORTUNITY") or 0)
        except (TypeError, ValueError):
            opp = 0
        db.execute(
            "insert into crm_deals (bitrix_id,sensa_company,title,category_id,stage_id,opportunity,currency,"
            "company_bitrix_id,contact_bitrix_id,source,source_desc,created,closed_at,data) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (bitrix_id) do update set sensa_company=excluded.sensa_company,title=excluded.title,"
            "stage_id=excluded.stage_id,opportunity=excluded.opportunity,data=excluded.data,imported_at=now()",
            (_int(d.get("ID")), biz_of(d.get("SOURCE_ID"), d.get("SOURCE_DESCRIPTION"), d.get("TITLE")),
             d.get("TITLE"), d.get("CATEGORY_ID"), d.get("STAGE_ID"), opp, d.get("CURRENCY_ID"),
             _int(d.get("COMPANY_ID")), _int(d.get("CONTACT_ID")), d.get("SOURCE_ID"), d.get("SOURCE_DESCRIPTION"),
             d.get("DATE_CREATE") or None, d.get("CLOSEDATE") or None, Json(d)))

    acts = load("activities.json")
    for a in acts:
        db.execute(
            "insert into crm_activities (bitrix_id,subject,type_id,direction,completed,owner_id,owner_type_id,"
            "created,data) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (bitrix_id) do update set subject=excluded.subject,completed=excluded.completed,"
            "data=excluded.data,imported_at=now()",
            (_int(a.get("ID")), a.get("SUBJECT"), a.get("TYPE_ID"), a.get("DIRECTION"),
             a.get("COMPLETED") == "Y", _int(a.get("OWNER_ID")), a.get("OWNER_TYPE_ID"),
             a.get("CREATED") or None, Json(a)))

    print(f"imported: {len(companies)} companies, {len(contacts)} contacts, {len(deals)} deals, {len(acts)} activities")
    print("\n== deals by business ==")
    for r in db.query("select coalesce(sensa_company,'(unknown)') b, count(*) n, "
                      "count(*) filter (where opportunity>0) won_val from crm_deals group by b order by n desc"):
        print(f"  {r['b']:12} {r['n']} deals")
    print("== contacts by business (all verified=false / webhook junk) ==")
    for r in db.query("select coalesce(sensa_company,'(unknown)') b, count(*) n from crm_contacts group by b order by n desc"):
        print(f"  {r['b']:12} {r['n']}")


if __name__ == "__main__":
    main()
