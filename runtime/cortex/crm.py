"""CRM — ONE source of truth: the `crm_master` table.

Every legitimate inbound inquiry, Cortex-wide and for any company, is upserted here (deduped by email),
tagged to its company via `organisation`, with the inquiry kept as a `note` and a full `history` log of
every interaction (enquiry received, reply sent, correction, etc.). There is no other contacts table.
"""
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Json

from . import db

# company slug -> the Organisation label used in crm_master (matches the imported master sheet)
ORG = {"tabscanner": "Tabscanner", "sensa": "Sensa", "skyvision": "Sky Vision",
       "filmspoke": "FilmSpoke", "snaprewards": "Snap Rewards", "flixton": "Flixton Manor"}

_MIGRATE = """
alter table crm_master add column if not exists note text;
alter table crm_master add column if not exists history jsonb not null default '[]'::jsonb;
alter table crm_master add column if not exists updated_at timestamptz not null default now();
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_MIGRATE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _org(company: str | None) -> str:
    return ORG.get((company or "").lower(), (company or "").title())


def _split_name(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split()
    if not parts:
        return ("", "")
    return (parts[0], " ".join(parts[1:]))


def log_event(email: str, event: str, text: str = "", company: str | None = None) -> None:
    """Append one interaction to a contact's history in crm_master (matched by email)."""
    if not email:
        return
    ensure_schema()
    ev = {"ts": _now(), "event": event, "text": (text or "")[:1200]}
    db.execute("update crm_master set history = history || %s::jsonb, updated_at=now() "
               "where lower(email)=lower(%s)", (Json([ev]), email))


def add_inquiry(inq: dict, company: str = "tabscanner") -> tuple[str, str | None]:
    """Upsert a legitimate inquiry into crm_master (dedup by email), with a note + history event.
    Returns (status, email). Works for ANY company via the `company` slug."""
    ensure_schema()
    email = (inq.get("email") or "").strip().lower()
    if not email:
        return ("skipped-no-email", None)
    name = (inq.get("name") or "").strip()
    msg = (inq.get("message") or inq.get("snippet") or "").strip()
    org = _org(company)
    ev = {"ts": _now(), "event": "enquiry", "text": msg[:1200]}
    existing = db.one("select id, organisation from crm_master where lower(email)=%s limit 1", (email,))
    if existing:
        # already a contact: record the new enquiry in history; make sure this company is on the tag
        cur = existing.get("organisation") or ""
        if org and org.lower() not in cur.lower():
            new_org = (cur + ", " + org).strip(", ") if cur else org
            db.execute("update crm_master set organisation=%s where id=%s", (new_org, existing["id"]))
        log_event(email, "enquiry", msg, company)
        return ("matched", email)
    fn, ln = _split_name(name)
    dom = email.split("@")[-1] if "@" in email else ""
    note = f"Website enquiry ({datetime.now(timezone.utc):%Y-%m-%d}): {msg}" if msg else "Website enquiry."
    # a genuine inbound enquiry (already past triage) = the contact has Engaged with us
    db.execute(
        "insert into crm_master (organisation, first_name, last_name, email, company_domain, "
        "lead_source, lead_status, stage, note, history) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (org, fn, ln, email, dom, "Website enquiry", "New", "Engaged", note, Json([ev])))
    return ("added", email)


# ---- Deals = the full lifecycle in crm_projects. Forecast stages show on the Opportunities screen;
#      won/ongoing stages show on the Projects screen. Crossing 'Booked' promotes an opportunity to a project.
FORECAST_STAGES = ["Opportunity", "Quote"]
WON_STAGES = ["Booked", "Production", "Delivered", "Final Payment", "Close & review"]
DEAL_STAGES = FORECAST_STAGES + WON_STAGES
PROJECT_STAGES = DEAL_STAGES   # back-compat alias (any valid deal stage)


def set_project_stage(project_id: int, stage: str) -> dict | None:
    """Move a deal to a new stage; logs the change, and (un)marks the linked contact a client across the
    Booked boundary. Moving across 'Booked' shifts it between the Opportunities and Projects screens."""
    p = db.one("select * from crm_projects where id=%s", (project_id,))
    if not p:
        return None
    old = p.get("stage")
    ev = {"ts": _now(), "event": "stage_change", "text": f"{old} -> {stage}"}
    db.execute("update crm_projects set stage=%s, history = history || %s::jsonb, updated_at=now() where id=%s",
               (stage, Json([ev]), project_id))
    if p.get("contact_email"):
        db.execute("update crm_master set is_client=%s where lower(email)=lower(%s)",
                   (stage in WON_STAGES, p["contact_email"]))
        log_event(p["contact_email"], "deal_stage", f"{p['title']}: {old} -> {stage}")
    return db.one("select * from crm_projects where id=%s", (project_id,))

def create_project(company: str, contact_email: str, title: str, value=None,
                   currency: str = "AED", stage: str = "Booked", owner: str | None = None,
                   quote_ref: str | None = None, note: str | None = None) -> int:
    """Create a project (the cash pipeline). Marks the contact an existing client + logs it on their history."""
    org = _org(company)
    row = db.execute(
        "insert into crm_projects (company, contact_email, title, value, currency, stage, owner, quote_ref, "
        "note, history) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
        (org, (contact_email or "").lower(), title, value, currency, stage, owner, quote_ref, note,
         Json([{"ts": _now(), "event": "project_created", "text": f"{title} ({stage})"}])))
    if contact_email:
        if stage in WON_STAGES:
            db.execute("update crm_master set is_client=true where lower(email)=lower(%s)", (contact_email,))
        log_event(contact_email, "deal_created", f"{title} ({stage})"
                  + (f" — {value} {currency}" if value else ""), company)
    return row["id"]


def ingest(inquiries: list[dict], company: str = "tabscanner") -> dict:
    added, matched = [], []
    for inq in inquiries:
        status, em = add_inquiry(inq, company)
        (added if status == "added" else matched).append({"email": em})
    return {"added": added, "matched": matched}
