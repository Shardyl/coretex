"""CRM writes — add genuine website inquiries into the contacts database.

A parsed inquiry (name + the lead's real email + their message) becomes a VERIFIED contact (unlike the
157 Bitrix webhook-junk rows), tagged with the company, with the inquiry kept as a note. Deduped by
email, so a returning person isn't added twice.
"""
from __future__ import annotations

from email.utils import parsedate_to_datetime

from psycopg.types.json import Json

from . import db

# one-time shape upgrade so the table can hold non-Bitrix contacts (Gmail/manual): surrogate id PK,
# nullable bitrix_id (+ unique so the Bitrix import stays idempotent), source_type, a message note.
_MIGRATE = """
alter table crm_contacts add column if not exists id bigserial;
alter table crm_contacts add column if not exists source_type text not null default 'bitrix';
alter table crm_contacts add column if not exists message text;
do $$ begin
  if exists (select 1 from information_schema.columns where table_name='crm_contacts'
             and column_name='bitrix_id' and is_nullable='NO') then
    alter table crm_contacts drop constraint if exists crm_contacts_pkey;
    alter table crm_contacts alter column bitrix_id drop not null;
    alter table crm_contacts add constraint crm_contacts_id_pk primary key (id);
  end if;
end $$;
create unique index if not exists crm_contacts_bitrix_uq on crm_contacts(bitrix_id);
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_MIGRATE)


def _date(s):
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def add_inquiry(inq: dict, company: str = "tabscanner") -> tuple[str, int]:
    """Add a parsed inquiry as a verified contact (dedup by email). Returns (status, contact_id)."""
    ensure_schema()
    email = (inq.get("email") or "").strip()
    name = (inq.get("name") or "").strip()
    msg = (inq.get("message") or "").strip()
    if email:
        ex = db.one("select id from crm_contacts where lower(email)=lower(%s) limit 1", (email,))
        if ex:
            return ("matched", ex["id"])
    row = db.execute(
        "insert into crm_contacts (sensa_company,name,email,source,source_desc,message,verified,"
        "source_type,created,data) values (%s,%s,%s,%s,%s,%s,true,'gmail',%s,%s) returning id",
        (company, name, email, "Website contact form", msg[:500], msg, _date(inq.get("date")), Json(inq)))
    return ("added", row["id"])


def ingest(inquiries: list[dict], company: str = "tabscanner") -> dict:
    added, matched = [], []
    for inq in inquiries:
        status, cid = add_inquiry(inq, company)
        (added if status == "added" else matched).append({"name": inq.get("name"), "email": inq.get("email"), "id": cid})
    return {"added": added, "matched": matched}
