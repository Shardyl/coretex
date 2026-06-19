"""Company Profile wizard — the per-company setup that every worker/skill reads from.

A fixed, curated question set (NOT AI-generated — these are specific structured fields) that Rashad runs
through by voice/type, handing Cortex the values, links and locations. Answers populate ONE structured
profile record per company (company_profiles.data). Resumable (idx). It's the single source of company
standards + the index to the real assets (a Drive folder Cortex reads, the website repo, etc.).
"""
from __future__ import annotations

from psycopg.types.json import Json

from . import db

_SCHEMA = """
create table if not exists company_profiles (
  company_id bigint primary key references companies(id) on delete cascade,
  data       jsonb not null default '{}'::jsonb,
  idx        int   not null default 0,
  status     text  not null default 'in_progress',
  updated_at timestamptz not null default now());
"""

# section, field, question, type (text|long|email|url|list|note)
QUESTIONS = [
    # 1. Identity
    ("Identity", "legal_name", "What's the company's full legal / registered name?", "text"),
    ("Identity", "brand_names", "Trading name and any brand names?", "text"),
    ("Identity", "domains", "Primary domain and all website URLs?", "text"),
    ("Identity", "address", "Registered / operating address?", "text"),
    ("Identity", "phone", "Main contact phone number(s)?", "text"),
    ("Identity", "registration", "VAT number / company registration number?", "text"),
    # (Brand voice / positioning / audience / don'ts are captured by the General Operations questionnaire
    #  — NOT re-asked here. This wizard is concrete profile + assets + locations only.)
    # 2. Brand & visual identity / assets
    ("Brand & assets", "asset_folder", "Paste the link to this company's ASSET FOLDER in Drive. Cortex reads this folder (and its subfolders) only.", "url"),
    ("Brand & assets", "brand_guidelines", "Brand guidelines — link or filename in the asset folder. (Tell me if one needs creating.)", "text"),
    ("Brand & assets", "logo_dark", "Official DARK logo — filename or link in the asset folder.", "text"),
    ("Brand & assets", "logo_light", "Official LIGHT / white logo — filename or link.", "text"),
    ("Brand & assets", "footer", "Official footer — filename/link, or the footer content.", "long"),
    ("Brand & assets", "header", "Official header — filename/link, or details.", "long"),
    ("Brand & assets", "palette", "Brand colour palette (primary / accent / etc.)?", "text"),
    ("Brand & assets", "fonts", "Official fonts (which, and where they live)?", "text"),
    ("Brand & assets", "visual_style", "Graphical / motion / imagery style notes?", "long"),
    # 4. Repositories & sources
    ("Repositories & sources", "website_repo", "Website repository (GitHub URL)?", "text"),
    ("Repositories & sources", "live_site", "Live site URL(s)?", "text"),
    ("Repositories & sources", "hosting", "Hosting / deploy details?", "long"),
    # 5. Communications
    ("Communications", "inbox_email", "Which email address do inquiries arrive at? (the inbox Cortex reads — e.g. api@tabscanner.com)", "email"),
    ("Communications", "reply_from", "When Cortex sends a reply, which email address should it come FROM? (what the customer sees — e.g. rashad@tabscanner.com)", "email"),
    ("Communications", "default_cc", "Anyone CC'd by default on replies? (or none)", "text"),
    ("Communications", "default_bcc", "Anyone BCC'd by default on replies? (e.g. an inbox you watch, to keep a copy) — or none", "text"),
    ("Communications", "signature", "Standard email signature (plain text — name, role, contact lines).", "long"),
    ("Communications", "signature_html", "Email signature — rich HTML (the designed version with logo, rendered on sent emails).", "long"),
    ("Communications", "social", "Social handles (per platform)?", "text"),
    ("Communications", "send_domain", "Which verified Mailgun domain does this company send newsletters FROM? (e.g. news.tabscanner.com — the domain must be verified in Mailgun first.)", "text"),
    ("Communications", "test_group", "Which email addresses should be in this company's TEST GROUP — the people who get a [TEST] copy of every newsletter before it goes to the real list? List them (comma or new-line separated).", "list"),
    # 6. Finance
    ("Finance", "currency", "Currency?", "text"),
    ("Finance", "vat", "VAT / tax rate?", "text"),
    ("Finance", "payment_terms", "Standard payment terms?", "text"),
    ("Finance", "bank_details", "Bank details (for quotes / invoices)?", "long"),
    ("Finance", "accounts_contact", "Who handles finance / who to CC for accounts?", "text"),
    # 7. Team email directory (the contact details — General Ops covers who-does-what, NOT the emails)
    ("Team directory", "team", "The team email directory — for each person: name, email and role, so CC's, sign-offs and hand-offs resolve to real people.", "long"),
    # 8. CRM
    ("CRM", "crm", "Which CRM database does this company use? (e.g. the Cortex CRM)", "text"),
]


def _q(i):
    s, f, t, ty = QUESTIONS[i]
    return {"idx": i, "section": s, "field": f, "text": t, "type": ty}


def _sync_test_group(company_id, value):
    """A test-group answer defines this company's LIVE newsletter test group: parse the emails, ensure each
    is a CRM contact tagged to the company, add them to newsletter_test_group, and drop any no longer listed."""
    import re
    from . import newsletter, crm
    want = {e.lower() for e in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", value or "")}
    slug_row = db.one("select slug from companies where id=%s", (company_id,))
    slug = slug_row["slug"] if slug_row else None
    current = {r["email"].lower() for r in newsletter.test_group(company_id)}
    for e in want:
        if slug:
            crm.ensure_contact(e, slug)
        newsletter.set_test_group(e, company_id, True)
    for e in current - want:
        newsletter.set_test_group(e, company_id, False)


def ensure_schema():
    with db.connect() as c:
        c.execute(_SCHEMA)


def _row(company_id):
    ensure_schema()
    r = db.one("select * from company_profiles where company_id=%s", (company_id,))
    if not r:
        r = db.execute("insert into company_profiles (company_id) values (%s) returning *", (company_id,))
    return r


def _state(r):
    total = len(QUESTIONS)
    idx = r["idx"]
    cur = _q(idx) if idx < total else None
    return {"idx": idx, "total": total, "status": "done" if cur is None else "in_progress",
            "question": cur, "data": r["data"] or {}}


def status(company_id):
    return _state(_row(company_id))


def start(company_id, restart=False):
    r = _row(company_id)
    if restart:
        r = db.execute("update company_profiles set idx=0, status='in_progress', updated_at=now() "
                       "where company_id=%s returning *", (company_id,))
    return _state(r)


def answer(company_id, value):
    r = _row(company_id)
    total = len(QUESTIONS)
    idx = r["idx"]
    data = dict(r["data"] or {})
    if idx < total:
        field = QUESTIONS[idx][1]
        data[field] = value
        idx += 1
        status_ = "done" if idx >= total else "in_progress"
        r = db.execute("update company_profiles set data=%s, idx=%s, status=%s, updated_at=now() "
                       "where company_id=%s returning *", (Json(data), idx, status_, company_id))
        if field == "test_group":
            try:
                _sync_test_group(company_id, value)
            except Exception:  # noqa: BLE001
                pass
    return _state(r)


def questions():
    return [_q(i) for i in range(len(QUESTIONS))]


def set_field(company_id, field, value):
    """Set a single profile field directly (used by the review-screen edit)."""
    valid = {f for _, f, _, _ in QUESTIONS}
    if field not in valid:
        return get(company_id)
    r = _row(company_id)
    data = dict(r["data"] or {})
    data[field] = value
    db.execute("update company_profiles set data=%s, updated_at=now() where company_id=%s", (Json(data), company_id))
    if field == "test_group":
        try:
            _sync_test_group(company_id, value)
        except Exception:  # noqa: BLE001
            pass
    return data


def get(company_id):
    """The full structured profile (for workers/skills to read)."""
    return (_row(company_id)["data"]) or {}
