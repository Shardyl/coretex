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
alter table crm_master add column if not exists newsletter_opt_out boolean not null default false;
alter table crm_master add column if not exists newsletter_bounced boolean not null default false;
alter table crm_master add column if not exists waitlist boolean not null default false;
alter table crm_master drop column if exists do_not_market;
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
        "insert into crm_master (organisation, first_name, last_name, email, website, "
        "lead_source, lead_status, stage, note, history) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (lower(email)) do nothing",
        (org, fn, ln, email, dom, "Website enquiry", "New", "Engaged", note, Json([ev])))
    return ("added", email)


def add_registration(reg: dict, company: str = "tabscanner",
                     source: str = "Tabscanner registrations", waitlist: bool = False) -> tuple[str, str | None]:
    """Upsert a website registration as an OPTED-IN newsletter subscriber (dedup by email).
    Registering on the site = newsletter opt-in -> newsletter_subscriber='True', newsletter_opt_out=false.
    `waitlist=True` also flags `waitlist` (never unsets it on an existing contact). Carries over (never
    clobbers) existing fields; just tags org/source/subscriber/waitlist and logs the event."""
    ensure_schema()
    email = (reg.get("email") or "").strip().lower()
    if not email:
        return ("skipped-no-email", None)
    first = (reg.get("first_name") or "").strip()
    last = (reg.get("last_name") or "").strip()
    name = (reg.get("name") or reg.get("full_name") or "").strip()
    if not first and not last and name:
        first, last = _split_name(name)
    company_name = (reg.get("company_name") or reg.get("company") or "").strip() or None
    phone = (reg.get("phone") or reg.get("phone_number") or "").strip() or None
    org = _org(company)
    dom = email.split("@")[-1] if "@" in email else None
    ev_text = (f"Joined the {org} waitlist." if waitlist
               else f"Registered on the {org} website (newsletter opt-in).")
    ev = {"ts": _now(), "event": "registration", "text": ev_text}
    existing = db.one("select id, organisation from crm_master where lower(email)=%s limit 1", (email,))
    if existing:
        cur = existing.get("organisation") or ""
        new_org = (cur + ", " + org).strip(", ") if (org and org.lower() not in cur.lower()) else (cur or org)
        db.execute(
            "update crm_master set organisation=%s, newsletter_subscriber='True', newsletter_opt_out=false, "
            "lead_source=coalesce(nullif(btrim(lead_source),''), %s), "
            "company_name=coalesce(company_name, %s), phone=coalesce(phone, %s), "
            "waitlist=(waitlist or %s), updated_at=now() where id=%s",
            (new_org, source, company_name, phone, bool(waitlist), existing["id"]))
        log_event(email, "registration", ev["text"], company)
        return ("matched", email)
    db.execute(
        "insert into crm_master (organisation, first_name, last_name, email, company_name, phone, website, "
        "lead_source, newsletter_subscriber, newsletter_opt_out, waitlist, lead_status, stage, history) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,'True',false,%s,%s,%s,%s) on conflict (lower(email)) do nothing",
        (org, first or None, last or None, email, company_name, phone, dom,
         source, bool(waitlist), "New", "Engaged", Json([ev])))
    return ("added", email)


def add_inbound_contact(reg: dict, company: str, classification: str, stage: str = "Engaged",
                        source: str = "inbound email", newsletter: bool = False) -> tuple[str, str | None]:
    """Add/dedup a contact from an INBOUND email (someone emailed a catch-all inbox). Org-tagged, the
    classification logged in history, lead_source set. `newsletter` = is this inbound category eligible
    for the newsletter (leads/customers/partners yes; freelancers/vendors/applicants no). Because the
    audience is OPT-OUT, a NEW non-eligible contact is inserted newsletter_opt_out=true so it stays
    CRM-only. EXISTING contacts are never touched on this flag (preserve their real subscription status).
    Carries over existing fields, never clobbers."""
    ensure_schema()
    email = (reg.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return ("skipped-no-email", None)
    name = (reg.get("name") or "").strip()
    first, last = _split_name(name) if name else ("", "")
    org = _org(company)
    ev = {"ts": _now(), "event": "inbound_email",
          "text": f"Inbound email to {org}, classified as: {classification}."}
    existing = db.one("select id, organisation from crm_master where lower(email)=%s limit 1", (email,))
    if existing:
        cur = existing.get("organisation") or ""
        new_org = (cur + ", " + org).strip(", ") if (org and org.lower() not in cur.lower()) else (cur or org)
        db.execute("update crm_master set organisation=%s, "
                   "lead_source=coalesce(nullif(btrim(lead_source),''), %s), updated_at=now() where id=%s",
                   (new_org, source, existing["id"]))
        log_event(email, "inbound_email", ev["text"], company)
        return ("matched", email)
    db.execute(
        "insert into crm_master (organisation, first_name, last_name, email, website, lead_source, "
        "newsletter_opt_out, lead_status, stage, history) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (lower(email)) do nothing",
        (org, first or None, last or None, email, email.split("@")[-1], source,
         not newsletter, "New", stage, Json([ev])))
    return ("added", email)


_MIGRATE_DEALS = """
alter table crm_projects add column if not exists contacts jsonb not null default '[]'::jsonb;
alter table crm_projects add column if not exists account_id bigint;
alter table crm_master add column if not exists account_id bigint;
create table if not exists crm_accounts (
  id bigserial primary key, name text not null, domain text, website text, phone text,
  note text, history jsonb not null default '[]'::jsonb, created_at timestamptz default now());
alter table crm_accounts add column if not exists company text;
"""


def ensure_deal_schema() -> None:
    with db.connect() as c:
        c.execute(_MIGRATE_DEALS)


def _name_of(email: str) -> str:
    c = db.one("select first_name, last_name from crm_master where lower(email)=lower(%s)", (email,))
    return (((c.get("first_name") or "") + " " + (c.get("last_name") or "")).strip()) if c else ""


def add_deal_contact(deal_id: int, email: str, role: str = "", primary: bool = False) -> dict | None:
    """Attach a contact to a deal (primary or secondary). One primary is always kept; mirrored to contact_email."""
    ensure_deal_schema()
    p = db.one("select contacts, contact_email from crm_projects where id=%s", (deal_id,))
    if not p:
        return None
    email = (email or "").strip().lower()
    lst = [c for c in (p.get("contacts") or []) if (c.get("email") or "").lower() != email]
    if primary:
        for c in lst:
            c["primary"] = False
    lst.append({"email": email, "name": _name_of(email), "role": role or "", "primary": bool(primary)})
    if not any(c.get("primary") for c in lst):
        lst[0]["primary"] = True
    prim = next((c["email"] for c in lst if c.get("primary")), email)
    db.execute("update crm_projects set contacts=%s::jsonb, contact_email=%s, updated_at=now() where id=%s",
               (Json(lst), prim, deal_id))
    log_event(email, "deal_linked", f"Linked to deal: {db.one('select title from crm_projects where id=%s',(deal_id,))['title']}")
    return db.one("select * from crm_projects where id=%s", (deal_id,))


def remove_deal_contact(deal_id: int, email: str) -> dict | None:
    p = db.one("select contacts from crm_projects where id=%s", (deal_id,))
    if not p:
        return None
    email = (email or "").strip().lower()
    lst = [c for c in (p.get("contacts") or []) if (c.get("email") or "").lower() != email]
    if lst and not any(c.get("primary") for c in lst):
        lst[0]["primary"] = True
    prim = next((c["email"] for c in lst if c.get("primary")), None)
    db.execute("update crm_projects set contacts=%s::jsonb, contact_email=%s, updated_at=now() where id=%s",
               (Json(lst), prim, deal_id))
    return db.one("select * from crm_projects where id=%s", (deal_id,))


def set_deal_primary(deal_id: int, email: str) -> dict | None:
    p = db.one("select contacts from crm_projects where id=%s", (deal_id,))
    if not p:
        return None
    email = (email or "").strip().lower()
    lst = p.get("contacts") or []
    for c in lst:
        c["primary"] = (c.get("email") or "").lower() == email
    db.execute("update crm_projects set contacts=%s::jsonb, contact_email=%s, updated_at=now() where id=%s",
               (Json(lst), email, deal_id))
    return db.one("select * from crm_projects where id=%s", (deal_id,))


# ---- Deals = the full lifecycle in crm_projects. Forecast stages show on the Opportunities screen;
#      won/ongoing stages show on the Projects screen. Crossing 'Booked' promotes an opportunity to a project.
FORECAST_STAGES = ["Opportunity", "Quote"]
WON_STAGES = ["Booked", "Production", "Recurring", "Delivered", "Final Payment", "Close & review"]
LOST_STAGE = "Lost"                                    # pitched but didn't win — exits both screens
DEAL_STAGES = FORECAST_STAGES + WON_STAGES + [LOST_STAGE]   # 'Recurring' = ongoing/repeat won work (retainers)
PROJECT_STAGES = DEAL_STAGES   # back-compat alias (any valid deal stage)

# Contact status = the relationship ladder ONLY (deals carry Opportunity/Quote — kept off contacts to avoid
# the same word meaning two things). is_client is a separate flag set when a linked deal is won.
CONTACT_STAGES = ["Cold", "Contacted", "Engaged", "Qualified", "Not interested", "Dormant/dead"]


def set_contact_stage(email: str, stage: str) -> dict | None:
    """Move a contact along the relationship ladder; logs the change on their history."""
    c = db.one("select id, stage from crm_master where lower(email)=lower(%s)", (email,))
    if not c:
        return None
    db.execute("update crm_master set stage=%s, updated_at=now() where id=%s", (stage, c["id"]))
    log_event(email, "status_change", f"{c.get('stage')} -> {stage}")
    return db.one("select * from crm_master where lower(email)=lower(%s)", (email,))


def set_newsletter_opt_out(email: str, on: bool) -> dict | None:
    """Flag/unflag a contact as newsletter opt-out. Default is opted-IN (everyone is sent the
    newsletter) unless this is true. Single source of truth for newsletter consent."""
    ensure_schema()
    c = db.one("select id from crm_master where lower(email)=lower(%s)", (email,))
    if not c:
        return None
    db.execute("update crm_master set newsletter_opt_out=%s, updated_at=now() where id=%s", (bool(on), c["id"]))
    log_event(email, "newsletter_opt_out", "Opted out of newsletter" if on else "Newsletter opt-out removed (re-subscribed)")
    return db.one("select * from crm_master where lower(email)=lower(%s)", (email,))


def set_newsletter_bounced(email: str, on: bool) -> dict | None:
    """Flag/unflag a contact's email as a NEWSLETTER hard-bounce (dead address). Distinct from
    newsletter_opt_out (chose to leave) and from Instantly's 'Bounced' status (cold-campaign channel).
    A bounced address is NEVER sent a newsletter again until cleared."""
    ensure_schema()
    c = db.one("select id from crm_master where lower(email)=lower(%s)", (email,))
    if not c:
        return None
    db.execute("update crm_master set newsletter_bounced=%s, updated_at=now() where id=%s", (bool(on), c["id"]))
    log_event(email, "newsletter_bounced", "Newsletter hard-bounced (suppressed)" if on else "Newsletter bounce cleared")
    return db.one("select * from crm_master where lower(email)=lower(%s)", (email,))


def add_contact_note(email: str, note: str) -> dict | None:
    if not db.one("select 1 from crm_master where lower(email)=lower(%s)", (email,)):
        return None
    log_event(email, "note", note)
    return db.one("select * from crm_master where lower(email)=lower(%s)", (email,))


def add_project_note(project_id: int, note: str) -> dict | None:
    p = db.one("select * from crm_projects where id=%s", (project_id,))
    if not p:
        return None
    ev = {"ts": _now(), "event": "note", "text": (note or "")[:1200]}
    db.execute("update crm_projects set history = history || %s::jsonb, updated_at=now() where id=%s",
               (Json([ev]), project_id))
    if p.get("contact_email"):
        log_event(p["contact_email"], "note", f"[{p['title']}] {note}")
    return db.one("select * from crm_projects where id=%s", (project_id,))


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
    p["stage"] = stage
    if stage in WON_STAGES:
        flag_clients_for_deal(p)        # won = the people/company on it become clients (sticky)
    if p.get("contact_email"):
        log_event(p["contact_email"], "deal_stage", f"{p['title']}: {old} -> {stage}")
    return db.one("select * from crm_projects where id=%s", (project_id,))


def flag_clients_for_deal(p: dict) -> int:
    """Mark every contact on a (won) deal — and everyone at its client account — as is_client. Sticky:
    we never auto-unmark, since a contact can be a client via another deal."""
    emails = {(c.get("email") or "").lower() for c in (p.get("contacts") or []) if c.get("email")}
    if p.get("contact_email"):
        emails.add(p["contact_email"].lower())
    n = 0
    for e in emails:
        n += 1 if db.execute("update crm_master set is_client=true where lower(email)=lower(%s) returning id", (e,)) else 0
    if p.get("account_id"):
        db.execute("update crm_master set is_client=true where account_id=%s", (p["account_id"],))
    return n

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
        ce = contact_email.strip().lower()
        db.execute("update crm_projects set contacts=%s::jsonb where id=%s",
                   (Json([{"email": ce, "name": _name_of(ce), "role": "", "primary": True}]), row["id"]))
        if stage in WON_STAGES:
            db.execute("update crm_master set is_client=true where lower(email)=lower(%s)", (contact_email,))
        log_event(contact_email, "deal_created", f"{title} ({stage})"
                  + (f" — {value} {currency}" if value else ""), company)
    return row["id"]


# ---- Accounts = the directory of CLIENT companies (the customers; distinct from your own businesses) ----

def get_or_create_account(name: str, domain: str | None = None) -> int:
    ensure_deal_schema()
    a = db.one("select id, domain from crm_accounts where lower(name)=lower(%s)", (name,))
    if a:
        if domain and not a.get("domain"):
            db.execute("update crm_accounts set domain=%s where id=%s", (domain, a["id"]))
        return a["id"]
    return db.execute("insert into crm_accounts (name, domain) values (%s,%s) returning id", (name, domain))["id"]


def link_account(account_id: int, deal_id: int | None = None, email: str | None = None) -> None:
    if deal_id:
        db.execute("update crm_projects set account_id=%s where id=%s", (account_id, deal_id))
    if email:
        db.execute("update crm_master set account_id=%s where lower(email)=lower(%s)", (account_id, email))


def add_account_note(account_id: int, note: str) -> dict | None:
    if not db.one("select 1 from crm_accounts where id=%s", (account_id,)):
        return None
    db.execute("update crm_accounts set history = history || %s::jsonb where id=%s",
               (Json([{"ts": _now(), "event": "note", "text": (note or "")[:1200]}]), account_id))
    return db.one("select * from crm_accounts where id=%s", (account_id,))


def rename_account(account_id: int, name: str) -> dict | None:
    if not db.one("select 1 from crm_accounts where id=%s", (account_id,)):
        return None
    db.execute("update crm_accounts set name=%s where id=%s", (name, account_id))
    return db.one("select * from crm_accounts where id=%s", (account_id,))


# ---- Manual creates (cockpit + Cortex): a company, a contact (under a company), a deal (for a company) ----

def create_account(name: str, domain: str | None = None, website: str | None = None,
                   phone: str | None = None, company: str | None = None) -> dict:
    aid = get_or_create_account(name.strip(), domain)
    label = _org(company) if company else None
    if website or phone or label:
        db.execute("update crm_accounts set website=coalesce(%s,website), phone=coalesce(%s,phone), "
                   "company=coalesce(%s,company) where id=%s", (website, phone, label, aid))
    return db.one("select * from crm_accounts where id=%s", (aid,))


def _account_has_won(account_id) -> bool:
    return bool(account_id and db.one("select 1 from crm_projects where account_id=%s and stage = any(%s) limit 1",
                                      (account_id, WON_STAGES)))


def create_contact(first_name: str, last_name: str, email: str, account_id=None, company: str | None = None,
                   phone: str | None = None, job_title: str | None = None, stage: str = "Cold") -> dict:
    ensure_schema()
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email required")
    cn = dom = None
    if account_id:
        a = db.one("select name, domain from crm_accounts where id=%s", (account_id,))
        if a:
            cn, dom = a["name"], a.get("domain")
    # atomic upsert (unique index on lower(email)) — concurrent submits can never create duplicates
    db.execute(
        "insert into crm_master (organisation, first_name, last_name, email, account_id, company_name, "
        "website, phone, job_title, stage, lead_source) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (lower(email)) do update set first_name=excluded.first_name, last_name=excluded.last_name, "
        "account_id=coalesce(excluded.account_id, crm_master.account_id), "
        "company_name=coalesce(excluded.company_name, crm_master.company_name), "
        "website=coalesce(excluded.website, crm_master.website), "
        "phone=coalesce(excluded.phone, crm_master.phone), "
        "job_title=coalesce(excluded.job_title, crm_master.job_title), updated_at=now()",
        (_org(company) if company else "", first_name, last_name, email, account_id, cn, dom,
         phone, job_title, stage, "Manual"))
    if _account_has_won(account_id):
        db.execute("update crm_master set is_client=true where lower(email)=lower(%s)", (email,))
    return db.one("select * from crm_master where lower(email)=lower(%s)", (email,))


class DuplicateDeal(ValueError):
    pass


def create_deal(company: str, title: str, value=None, currency: str = "AED", stage: str = "Opportunity",
                account_id=None, owner: str | None = None) -> dict:
    """A deal belongs to one of YOUR businesses (company) and one CLIENT company (account). Its people are
    that account's contacts (company-mediated — no per-deal contact list). Blocks an active same-name duplicate."""
    org = _org(company)
    title = (title or "").strip()
    dup = db.one("select id from crm_projects where company=%s and lower(title)=lower(%s) and stage <> %s limit 1",
                 (org, title, LOST_STAGE))
    if dup:
        raise DuplicateDeal(f"A deal '{title}' already exists for {org} (deal #{dup['id']}). "
                            "Open that one, or give this a different name.")
    row = db.execute(
        "insert into crm_projects (company, title, value, currency, stage, owner, account_id, history) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s) returning id",
        (org, title, value, currency, stage, owner, account_id,
         Json([{"ts": _now(), "event": "deal_created", "text": f"{title} ({stage})"}])))
    if account_id and stage in WON_STAGES:
        flag_clients_for_deal(db.one("select * from crm_projects where id=%s", (row["id"],)))
    return db.one("select * from crm_projects where id=%s", (row["id"],))


def set_contact_account(email: str, account_id) -> dict | None:
    if not db.one("select 1 from crm_master where lower(email)=lower(%s)", (email,)):
        return None
    a = db.one("select name from crm_accounts where id=%s", (account_id,)) if account_id else None
    db.execute("update crm_master set account_id=%s, company_name=coalesce(%s,company_name), updated_at=now() "
               "where lower(email)=lower(%s)", (account_id, a["name"] if a else None, email))
    if _account_has_won(account_id):
        db.execute("update crm_master set is_client=true where lower(email)=lower(%s)", (email,))
    return db.one("select * from crm_master where lower(email)=lower(%s)", (email,))


def set_deal_account(deal_id: int, account_id) -> dict | None:
    p = db.one("select * from crm_projects where id=%s", (deal_id,))
    if not p:
        return None
    db.execute("update crm_projects set account_id=%s, updated_at=now() where id=%s", (account_id, deal_id))
    p["account_id"] = account_id
    if p["stage"] in WON_STAGES:
        flag_clients_for_deal(p)
    return db.one("select * from crm_projects where id=%s", (deal_id,))


def update_contact(email: str, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items()
               if k in ("first_name", "last_name", "job_title", "phone", "email") and v is not None}
    if not db.one("select 1 from crm_master where lower(email)=lower(%s)", (email,)):
        return None
    if allowed:
        sets = ", ".join(f"{k}=%s" for k in allowed)
        db.execute(f"update crm_master set {sets}, updated_at=now() where lower(email)=lower(%s)",
                   tuple(allowed.values()) + (email,))
    return db.one("select * from crm_master where lower(email)=lower(%s)", (allowed.get("email", email),))


def update_deal(deal_id: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items()
               if k in ("title", "value", "currency", "owner") and v is not None}
    if not db.one("select 1 from crm_projects where id=%s", (deal_id,)):
        return None
    if allowed:
        sets = ", ".join(f"{k}=%s" for k in allowed)
        db.execute(f"update crm_projects set {sets}, updated_at=now() where id=%s",
                   tuple(allowed.values()) + (deal_id,))
    return db.one("select * from crm_projects where id=%s", (deal_id,))


def delete_contact(email: str) -> None:
    db.execute("update crm_projects set contact_email=NULL where lower(contact_email)=lower(%s)", (email,))
    db.execute("delete from crm_master where lower(email)=lower(%s)", (email,))


def delete_account(account_id: int) -> None:
    """Delete a client company; its contacts and deals are kept but unlinked from it."""
    db.execute("update crm_master set account_id=NULL where account_id=%s", (account_id,))
    db.execute("update crm_projects set account_id=NULL where account_id=%s", (account_id,))
    db.execute("delete from crm_accounts where id=%s", (account_id,))


def delete_deal(deal_id: int) -> None:
    db.execute("delete from crm_projects where id=%s", (deal_id,))


def ingest(inquiries: list[dict], company: str = "tabscanner") -> dict:
    added, matched = [], []
    for inq in inquiries:
        status, em = add_inquiry(inq, company)
        (added if status == "added" else matched).append({"email": em})
    return {"added": added, "matched": matched}
