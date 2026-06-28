"""CRM — ONE source of truth: the `crm_master` table.

Every legitimate inbound inquiry, Cortex-wide and for any company, is upserted here (deduped by email),
tagged to its company via `organisation`, with the inquiry kept as a `note` and a full `history` log of
every interaction (enquiry received, reply sent, correction, etc.). There is no other contacts table.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from . import config, db

# company slug -> the Organisation label used in crm_master (matches the imported master sheet)
ORG = {"tabscanner": "Tabscanner", "sensa": "Sensa", "skyvision": "Sky Vision",
       "filmspoke": "FilmSpoke", "snaprewards": "Snap Rewards", "flixton": "Flixton Manor"}

# ---- Organisation de-dup: a shared CUSTOM email domain implies the same org. Free/shared providers do NOT. ----
FREE_EMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "yahoo.fr", "yahoo.de", "ymail.com",
    "rocketmail.com", "hotmail.com", "hotmail.co.uk", "hotmail.fr", "outlook.com", "live.com", "live.co.uk", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me", "gmx.com", "gmx.net", "gmx.de",
    "mail.com", "mail.ru", "yandex.com", "yandex.ru", "qq.com", "163.com", "126.com", "sina.com", "foxmail.com",
    "zoho.com", "fastmail.com", "hey.com", "rediffmail.com", "web.de", "t-online.de", "orange.fr", "free.fr",
    "btinternet.com", "comcast.net", "sbcglobal.net", "verizon.net", "att.net", "cox.net", "sky.com",
    "virginmedia.com", "emirates.net.ae", "eim.ae", "etisalat.ae", "du.ae",
}
# your own brands/entities — a group containing one is flagged and never auto-merged.
OWN_BRAND_TOKENS = ("sensa", "sky vision", "skyvision", "filmspoke", "tabscanner", "snap rewards", "snaprewards",
                    "three digital", "3 digital")
_LEGAL = re.compile(r"\b(l\.?l\.?c\.?|ltd|limited|fz-?llc|fzco|fze|fz|llp|inc|corp|co|plc|pjsc|psc|est|gen|general|"
                    r"trading|holdings?|group)\b")


def _basename(s: str) -> str:
    """Normalised name with parentheticals + legal suffixes stripped, so 'Emaar Entertainment' == 'Emaar Entertainment LLC'."""
    s = re.sub(r"\(.*?\)", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", _LEGAL.sub("", s))


def _is_own_brand(name: str) -> bool:
    n = (name or "").lower()
    return any(t in n for t in OWN_BRAND_TOKENS)


def ensure_merge_schema() -> None:
    with db.connect() as c:
        c.execute("create table if not exists account_merges("
                  "id bigserial primary key, winner_id bigint, reason text, "
                  "reversed boolean not null default false, created_at timestamptz not null default now())")
        c.execute("create table if not exists account_merge_log("
                  "id bigserial primary key, merge_id bigint references account_merges(id), "
                  "action text, crm_id bigint, before jsonb)")


def account_dupe_groups() -> list[dict]:
    """Domain-based duplicate-organisation candidates: a non-free email domain shared by 2+ accounts. Each group =
    {domain, accounts[{id,name,contacts,deals,own_brand}], own_brand, auto_safe, size}. auto_safe = exactly 2
    accounts, no own-brand, and identical base names (legal suffix / parenthetical stripped)."""
    from collections import defaultdict
    dmap: dict[str, set] = defaultdict(set)
    for r in db.query("select lower(split_part(email,'@',2)) d, account_id aid from crm_master "
                      "where account_id is not null and position('@' in email) > 0"):
        d = r["d"]
        if d and "." in d and d not in FREE_EMAIL:
            dmap[d].add(r["aid"])
    groups = []
    for d, aids in dmap.items():
        if len(aids) < 2:
            continue
        accts = db.query(
            "select a.id, a.name, (select count(*) from crm_master m where m.account_id=a.id) contacts, "
            "(select count(*) from crm_projects p where p.account_id=a.id) deals "
            "from crm_accounts a where a.id = any(%s) order by 3 desc, id", (list(aids),))
        for a in accts:
            a["own_brand"] = _is_own_brand(a["name"])
        any_own = any(a["own_brand"] for a in accts)
        bases = {_basename(a["name"]) for a in accts}
        auto_safe = len(accts) == 2 and not any_own and len(bases) == 1 and "" not in bases
        groups.append({"domain": d, "accounts": accts, "own_brand": any_own, "auto_safe": auto_safe, "size": len(accts)})
    groups.sort(key=lambda g: (not g["auto_safe"], g["own_brand"], -g["size"], g["domain"]))
    return groups


def merge_accounts(winner_id: int, loser_ids: list[int], reason: str = "") -> dict:
    """Merge loser accounts into the winner: carry over every non-empty field, move all contacts + deals, then
    delete the losers. Reversible via account_merge_log (see reverse_account_merge). Returns a summary."""
    ensure_merge_schema()
    winner_id = int(winner_id)
    loser_ids = [int(x) for x in loser_ids if int(x) != winner_id]
    if not loser_ids:
        return {"merged": 0}
    conn = psycopg.connect(config.require("DATABASE_URL"), row_factory=dict_row, autocommit=False)
    try:
        w = conn.execute("select * from crm_accounts where id=%s", (winner_id,)).fetchone()
        if not w:
            return {"error": "winner not found"}
        mid = conn.execute("insert into account_merges(winner_id, reason) values (%s,%s) returning id",
                           (winner_id, reason)).fetchone()["id"]
        conn.execute("insert into account_merge_log(merge_id,action,crm_id,before) values (%s,'winner_enriched',%s,%s)",
                     (mid, winner_id, Json({k: w.get(k) for k in ("domain", "website", "phone", "note")}
                                           | {"history": w.get("history")})))
        losers = conn.execute("select * from crm_accounts where id = any(%s)", (loser_ids,)).fetchall()
        carry = {}
        for f in ("domain", "website", "phone", "note"):
            if not (w.get(f) or "").strip():
                for l in losers:
                    if (l.get(f) or "").strip():
                        carry[f] = l[f]; break
        hist = list(w.get("history") or [])
        for l in losers:
            hist += list(l.get("history") or [])
        hist.append({"event": "merge", "text": "merged: " + ", ".join(l["name"] for l in losers)})
        conn.execute("update crm_accounts set domain=%s, website=%s, phone=%s, note=%s, history=%s where id=%s",
                     (carry.get("domain", w.get("domain")), carry.get("website", w.get("website")),
                      carry.get("phone", w.get("phone")), carry.get("note", w.get("note")), Json(hist), winner_id))
        cm = dm = 0
        for l in losers:
            for r in conn.execute("select id from crm_master where account_id=%s", (l["id"],)).fetchall():
                conn.execute("insert into account_merge_log(merge_id,action,crm_id,before) values (%s,'contact_moved',%s,%s)",
                             (mid, r["id"], Json({"account_id": l["id"]}))); cm += 1
            conn.execute("update crm_master set account_id=%s, updated_at=now() where account_id=%s", (winner_id, l["id"]))
            for r in conn.execute("select id from crm_projects where account_id=%s", (l["id"],)).fetchall():
                conn.execute("insert into account_merge_log(merge_id,action,crm_id,before) values (%s,'deal_moved',%s,%s)",
                             (mid, r["id"], Json({"account_id": l["id"]}))); dm += 1
            conn.execute("update crm_projects set account_id=%s where account_id=%s", (winner_id, l["id"]))
            row = {"id": l["id"], "name": l["name"], "domain": l.get("domain"), "website": l.get("website"),
                   "phone": l.get("phone"), "note": l.get("note"), "history": l.get("history"),
                   "created_at": l["created_at"].isoformat() if l.get("created_at") else None, "company": l.get("company")}
            conn.execute("insert into account_merge_log(merge_id,action,crm_id,before) values (%s,'acct_deleted',%s,%s)",
                         (mid, l["id"], Json(row)))
            conn.execute("delete from crm_accounts where id=%s", (l["id"],))
        conn.commit()
        return {"merge_id": mid, "winner_id": winner_id, "removed": len(losers), "contacts_moved": cm, "deals_moved": dm}
    finally:
        conn.close()


def reverse_account_merge(merge_id: int) -> dict:
    """Undo a merge_accounts() call exactly: re-create the deleted accounts, move contacts + deals back, restore
    the winner's carried-over fields."""
    conn = psycopg.connect(config.require("DATABASE_URL"), row_factory=dict_row, autocommit=False)
    try:
        log = conn.execute("select * from account_merge_log where merge_id=%s order by id", (merge_id,)).fetchall()
        for r in (x for x in log if x["action"] == "acct_deleted"):
            b = r["before"]
            conn.execute("insert into crm_accounts (id,name,domain,website,phone,note,history,created_at,company) "
                         "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do nothing",
                         (b["id"], b["name"], b.get("domain"), b.get("website"), b.get("phone"), b.get("note"),
                          Json(b.get("history") or []), b.get("created_at"), b.get("company")))
        for r in (x for x in log if x["action"] == "contact_moved"):
            conn.execute("update crm_master set account_id=%s, updated_at=now() where id=%s", (r["before"]["account_id"], r["crm_id"]))
        for r in (x for x in log if x["action"] == "deal_moved"):
            conn.execute("update crm_projects set account_id=%s where id=%s", (r["before"]["account_id"], r["crm_id"]))
        for r in (x for x in log if x["action"] == "winner_enriched"):
            b = r["before"]
            conn.execute("update crm_accounts set domain=%s, website=%s, phone=%s, note=%s, history=%s where id=%s",
                         (b.get("domain"), b.get("website"), b.get("phone"), b.get("note"), Json(b.get("history") or []), r["crm_id"]))
        conn.execute("update account_merges set reversed=true where id=%s", (merge_id,))
        conn.commit()
        return {"reversed": merge_id}
    finally:
        conn.close()

# inbound-email classifications that become CRM contacts (mirrors engine._INBOX_CRM; duplicated here to
# avoid a circular import). A contact's structured `classification` field is one of these, or null.
# "client" = is_client=true (someone who has been a client at some point — sticky); it wins over any guess.
CLASSIFICATIONS = ["client", "lead", "partner", "support", "freelancer", "vendor", "recruitment", "not_qualified"]
# scrape-derived HISTORICAL labels (Sensa email-history merge). NOT assigned by the inbound classifier,
# but valid to set by hand and to filter on. Kept distinct from the hard is_client flag.
PAST_CLASSIFICATIONS = ["past_client", "past_opportunity"]
ALL_CLASSIFICATIONS = CLASSIFICATIONS + PAST_CLASSIFICATIONS


def set_classification(email: str, classification: str | None) -> dict | None:
    """Set or clear a contact's structured classification. Returns the updated row (None if no such contact
    or an invalid value)."""
    cl = (classification or "").strip().lower() or None
    if cl and cl not in ALL_CLASSIFICATIONS:
        return None
    return db.execute("update crm_master set classification=%s, updated_at=now() "
                      "where lower(email)=lower(%s) returning *", (cl, email))

_MIGRATE = """
alter table crm_master add column if not exists note text;
alter table crm_master add column if not exists history jsonb not null default '[]'::jsonb;
alter table crm_master add column if not exists updated_at timestamptz not null default now();
alter table crm_master add column if not exists newsletter_opt_out boolean not null default false;
alter table crm_master add column if not exists newsletter_bounced boolean not null default false;
alter table crm_master add column if not exists waitlist boolean not null default false;
alter table crm_master add column if not exists classification text;
alter table crm_master add column if not exists market text;
alter table crm_master add column if not exists quote_sent boolean not null default false;
alter table crm_master add column if not exists do_not_market jsonb not null default '[]'::jsonb;
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_MIGRATE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _org(company: str | None) -> str:
    return ORG.get((company or "").lower(), (company or "").title())


class DealNeedsCompany(ValueError):
    """Raised when a deal/opportunity is created without one of YOUR businesses set."""


def _require_business(company: str | None) -> str:
    """A deal/opportunity MUST belong to one of YOUR businesses. Returns the canonical org name, else raises.
    Accepts a slug (sensa) or display name (Sensa); rejects empty, 'all', or any non-business (e.g. a client name)."""
    key = (company or "").strip().lower()
    valid = set(ORG) | {v.lower() for v in ORG.values()}
    if key not in valid:
        raise DealNeedsCompany(
            "An opportunity must be set to one of your businesses (Tabscanner, Sensa, Sky Vision, FilmSpoke "
            "or Snap Rewards). Pick a specific company, not 'All companies'.")
    return _org(company)


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


# ---------- per-company membership (the `organisation` tag) ----------

def set_membership(email: str, company_slug: str, on: bool) -> str | None:
    """Add or remove ONE company tag on a contact's `organisation` field. Touches only that single label;
    never clobbers the other tags. Returns the new organisation value (or None if the contact is missing)."""
    label = _org(company_slug)
    r = db.one("select id, organisation from crm_master where lower(email)=lower(%s) limit 1", (email,))
    if not r:
        return None
    parts = [p.strip() for p in (r.get("organisation") or "").split(",") if p.strip()]
    has = any(p.lower() == label.lower() for p in parts)
    if on and not has:
        parts.append(label)
    elif not on and has:
        parts = [p for p in parts if p.lower() != label.lower()]
    new = ", ".join(parts)
    db.execute("update crm_master set organisation=%s, updated_at=now() where id=%s", (new, r["id"]))
    return new


def contact_company_state(email: str | None = None, id: int | None = None) -> dict:
    """Per-company membership / subscriber / test-group state for a contact, across the LIVE companies list
    (a newly added company appears automatically). subscriber = member AND not globally opted out.
    Loads by id (emailless harvested contacts) or by email (the legacy book)."""
    if id is not None:
        r = db.one("select organisation, newsletter_opt_out, do_not_market, email from crm_master where id=%s", (id,))
    else:
        r = db.one("select organisation, newsletter_opt_out, do_not_market, email from crm_master "
                   "where lower(email)=lower(%s) limit 1", (email,))
    orgs = {p.strip().lower() for p in ((r.get("organisation") if r else "") or "").split(",") if p.strip()}
    opted_out = bool(r and r.get("newsletter_opt_out"))
    dnm = set((r.get("do_not_market") if r else None) or [])
    em = (r.get("email") if r else None) or email
    tg = {row["company_id"] for row in
          db.query("select company_id from newsletter_test_group where active and lower(email)=lower(%s)", (em,))} if em else set()
    comps = []
    for c in db.query("select id, slug, name from companies order by name"):
        member = _org(c["slug"]).lower() in orgs
        comps.append({"slug": c["slug"], "name": c["name"], "id": c["id"],
                      "member": member, "test_group": c["id"] in tg,
                      "subscriber": member and not opted_out, "do_not_market": c["slug"] in dnm})
    return {"opted_out": opted_out, "companies": comps}


def ensure_contact(email: str, company_slug: str, name: str | None = None, subscriber: bool = True) -> str | None:
    """Ensure a crm_master contact exists and is tagged to the company (subscriber-on by default). Used by
    the setup wizard when capturing a company's test group. Carries over existing fields, never clobbers."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    ensure_schema()
    if db.one("select id from crm_master where lower(email)=lower(%s) limit 1", (email,)):
        set_membership(email, company_slug, True)
        return email
    fn, ln = _split_name(name or "")
    db.execute("insert into crm_master (organisation, first_name, last_name, email, website, lead_source, "
               "newsletter_subscriber, newsletter_opt_out, lead_status, stage) "
               "values (%s,%s,%s,%s,%s,%s,'True',%s,%s,%s) on conflict (lower(email)) do nothing",
               (_org(company_slug), fn or None, ln or None, email, email.split("@")[-1], "test group",
                not subscriber, "New", "Engaged"))
    return email


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
        status = "matched"
    else:
        db.execute(
            "insert into crm_master (organisation, first_name, last_name, email, company_name, phone, website, "
            "lead_source, newsletter_subscriber, newsletter_opt_out, waitlist, lead_status, stage, history) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,'True',false,%s,%s,%s,%s) on conflict (lower(email)) do nothing",
            (org, first or None, last or None, email, company_name, phone, dom,
             source, bool(waitlist), "New", "Engaged", Json([ev])))
        status = "added"
    # Notify Rashad of registrations: ONE rolling card PER DAY (resets daily, so he sees exactly who
    # registered today) plus a phone push. Mirrors the inbound-classifier card; priority=normal so it
    # reaches the lock screen. Dedup_key carries the Dubai-local day (UAE = UTC+4, no DST).
    try:
        from . import notifications
        cid_row = db.one("select id from companies where slug=%s", (company,))
        day = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%d")
        who = (f"{first} {last}".strip() or name or email)
        label = "waitlist signup" if waitlist else "registration"
        body = f"{who} ({email})" + (f", {company_name}" if company_name else "") + f" → {org}"
        notifications.notify(
            f"New {org} {label}", body, priority="normal", category="registration",
            dedup_key=f"registration:{company}:{day}",
            company_id=(cid_row["id"] if cid_row else None),
            target_type="contact", target_id=email,
            item={"name": who, "email": email, "company_name": company_name})
    except Exception:  # noqa: BLE001 — notifying must never break the webhook
        pass
    return (status, email)


def add_inbound_contact(reg: dict, company: str, classification: str, stage: str = "Engaged",
                        source: str = "inbound email", newsletter: bool = False,
                        summary: str | None = None, market: str | None = None) -> tuple[str, str | None]:
    """Add/dedup a contact from an INBOUND email (someone emailed a catch-all inbox). Org-tagged, the
    classification logged in history, lead_source set. `newsletter` = is this inbound category eligible
    for the newsletter (leads/customers/partners yes; freelancers/vendors/applicants no). Because the
    audience is OPT-OUT, a NEW non-eligible contact is inserted newsletter_opt_out=true so it stays
    CRM-only. EXISTING contacts are never touched on this flag (preserve their real subscription status).
    `summary` = the email's gist (who/what), saved as the contact's note + logged to history; `market` = a
    sub-classification (industry/market). Both fill-if-blank, never clobber existing context."""
    ensure_schema()
    email = (reg.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return ("skipped-no-email", None)
    name = (reg.get("name") or "").strip()
    first, last = _split_name(name) if name else ("", "")
    org = _org(company)
    note = (summary or "").strip() or None
    mkt = (market or "").strip() or None
    ev_text = f"Inbound email to {org}, classified as: {classification}." + (f" {note}" if note else "")
    ev = {"ts": _now(), "event": "inbound_email", "text": ev_text}
    existing = db.one("select id, organisation from crm_master where lower(email)=%s limit 1", (email,))
    if existing:
        cur = existing.get("organisation") or ""
        new_org = (cur + ", " + org).strip(", ") if (org and org.lower() not in cur.lower()) else (cur or org)
        db.execute("update crm_master set organisation=%s, "
                   "lead_source=coalesce(nullif(btrim(lead_source),''), %s), "
                   "classification=case when %s='client' or classification='client' then 'client' "
                   "                    else coalesce(classification, %s) end, "
                   "market=coalesce(nullif(btrim(market),''), %s), "
                   "note=coalesce(nullif(btrim(note),''), %s), updated_at=now() where id=%s",
                   (new_org, source, classification, classification, mkt, note, existing["id"]))
        log_event(email, "inbound_email", ev_text, company)
        return ("matched", email)
    db.execute(
        "insert into crm_master (organisation, first_name, last_name, email, website, lead_source, "
        "classification, market, note, newsletter_opt_out, lead_status, stage, history) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (lower(email)) do nothing",
        (org, first or None, last or None, email, email.split("@")[-1], source,
         classification, mkt, note, not newsletter, "New", stage, Json([ev])))
    try:    # grouped FYI info-card in the Inbox (one rolling card per company until dismissed)
        from . import notifications
        cid_row = db.one("select id from companies where slug=%s", (company,))
        notifications.notify(f"New {classification} captured", f"{name or email} ({classification}) → {org}",
                             priority="fyi", category="contact", dedup_key=f"inbound:{org}",
                             company_id=(cid_row["id"] if cid_row else None),
                             target_type="contact", target_id=email,
                             item={"name": name or email, "email": email, "cat": classification})
    except Exception:  # noqa: BLE001
        pass
    return ("added", email)


_MIGRATE_DEALS = """
alter table crm_projects add column if not exists contacts jsonb not null default '[]'::jsonb;
alter table crm_projects add column if not exists account_id bigint;
alter table crm_master add column if not exists account_id bigint;
create table if not exists crm_accounts (
  id bigserial primary key, name text not null, domain text, website text, phone text,
  note text, history jsonb not null default '[]'::jsonb, created_at timestamptz default now());
alter table crm_accounts add column if not exists company text;
-- opportunity follow-up automation (Cortex system-wide; cadence is config, never code)
alter table crm_projects add column if not exists automation text;             -- 'auto' | 'manual' | null(off)
alter table crm_projects add column if not exists followup_step int not null default 0;
alter table crm_projects add column if not exists next_followup timestamptz;    -- when the next auto follow-up fires
alter table crm_projects add column if not exists cadence jsonb;               -- per-opportunity override of the company cadence
alter table tasks add column if not exists deal_id bigint;                     -- link a reminder/follow-up card to an opportunity
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


# ---- Anchor harvest = read an anchor's engagers (commenters/reactors) into crm_master. Reads + CRM write
#      only; outward actions stay with outreach-linkedin-sequences. Keyed on the LinkedIn profile URL
#      (email is usually empty for harvested leads). Never overwrites existing fields.

def _norm_linkedin(url):
    """Normalise a profile URL: strip query + trailing slash but PRESERVE CASE (member-id '/in/ACoAA…' URLs
    are case-sensitive and break if lowercased; vanity URLs are already lowercase). Dedupe is case-insensitive
    via lower() in the query. Requires /in/."""
    u = (url or "").strip().split("?")[0].rstrip("/")
    return u if "/in/" in u.lower() else ""


def upsert_anchor_lead(lead: dict, persona: str, region: str, anchor: str, company: str = "filmspoke",
                       segment: str | None = None, audience: str | None = None,
                       platform: str | None = None) -> str:
    """Upsert one harvested anchor lead into crm_master, keyed on the LinkedIn URL. On a re-capture it appends
    a provenance object to history + unions tags and NEVER overwrites a field. Returns inserted|updated|skipped.
    The buyer is STAMPED with its source anchor's segment / audience / platform so the CRM can cleanly separate
    targeted-marketer leads from wide business-owner leads, and one platform's leads from another's."""
    ensure_schema()
    url = _norm_linkedin(lead.get("linkedin"))
    if not url:
        return "skipped"
    prov = {"at": _now(), "anchor": anchor, "persona": persona, "post": lead.get("post", ""),
            "platform": (platform or "linkedin"), "segment": segment, "audience": audience,
            "engagement": (lead.get("engagement") or "")[:600]}
    tags = ["anchor-harvest", f"anchor:{anchor}"[:80]]
    if platform:
        tags.append(f"platform:{platform.lower()}"[:40])
    if segment:
        tags.append(f"segment:{segment.lower()}"[:60])
    if audience:
        tags.append(f"audience:{audience.lower()}"[:60])
    if lead.get("type"):
        tags.append(f"type:{lead['type']}")
    if lead.get("classification"):          # classified at ingest -> already scored
        tags.append("scored")
    existing = db.one("select id, tags from crm_master where lower(linkedin)=lower(%s)", (url,))
    if existing:
        merged = sorted(set(existing.get("tags") or []) | set(tags))
        db.execute("update crm_master set history = history || %s::jsonb, tags = %s::jsonb, updated_at=now() "
                   "where id=%s", (Json([prov]), Json(merged), existing["id"]))
        return "updated"
    fn, ln = _split_name(lead.get("name") or "")
    note = (lead.get("engagement") or "").strip()
    if lead.get("post"):
        note = (note + f"  [post: {lead['post']}]").strip()
    cls = lead.get("classification")
    tier = str(lead["score"]) if lead.get("score") is not None else None
    db.execute(
        "insert into crm_master (organisation, first_name, last_name, job_title, linkedin, location, country, "
        "city, lead_status, lead_source, stage, classification, tier, note, tags, history) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,'new',%s,'Cold',%s,%s,%s,%s::jsonb,%s::jsonb)",
        (_org(company), fn or None, ln or None, lead.get("headline"), url, lead.get("location"),
         lead.get("country"), lead.get("city"),
         f"{_org(company)} LinkedIn Anchor - {persona} ({region})", cls, tier, note or None,
         Json(sorted(set(tags))), Json([prov])))
    return "inserted"


def record_anchor_stats(company_id: int, anchor: str, post: str, engagers: int, buyers: int,
                        vendors: int, scores: list, segment: str | None = None, audience: str | None = None,
                        platform: str | None = None, fresh: bool = False) -> None:
    """Record one post's harvest outcome onto the anchor's grade row (the per-anchor HIT-RATE, captured BEFORE
    any vendor drop). fresh=True on the FIRST post of a fresh harvest of this anchor OVERWRITES the row (so a
    re-harvest in rotation REPLACES last time's numbers, never inflates); subsequent posts of the same run
    accumulate. Also stamps the anchor's segment / audience / platform."""
    db.execute("""create table if not exists social_anchors (
        id bigserial primary key, company_id int, name text, posts int default 0, engagers int default 0,
        buyers int default 0, vendors int default 0, sum_score numeric default 0, last_post text,
        last_harvest timestamptz, segment text, audience text, platform text,
        created_at timestamptz default now(), unique(company_id, name))""")
    for col in ("segment", "audience", "platform"):
        db.execute(f"alter table social_anchors add column if not exists {col} text")
    s = float(sum(x for x in scores if isinstance(x, (int, float)))) if scores else 0.0
    if fresh:                       # first post of this run -> REPLACE the anchor's numbers (rotation-safe)
        db.execute("""insert into social_anchors (company_id, name, posts, engagers, buyers, vendors, sum_score,
            last_post, last_harvest, segment, audience, platform)
            values (%s,%s,1,%s,%s,%s,%s,%s,now(),%s,%s,%s)
            on conflict (company_id, name) do update set
              posts=1, engagers=excluded.engagers, buyers=excluded.buyers, vendors=excluded.vendors,
              sum_score=excluded.sum_score, last_post=excluded.last_post, last_harvest=now(),
              segment=excluded.segment, audience=excluded.audience, platform=excluded.platform""",
            (company_id, anchor, engagers, buyers, vendors, s, post, segment, audience, platform))
    else:                           # later posts of the same run -> accumulate onto this run's row
        db.execute("""insert into social_anchors (company_id, name, posts, engagers, buyers, vendors, sum_score,
            last_post, last_harvest, segment, audience, platform)
            values (%s,%s,1,%s,%s,%s,%s,%s,now(),%s,%s,%s)
            on conflict (company_id, name) do update set
              posts=social_anchors.posts+1,
              engagers=social_anchors.engagers+excluded.engagers,
              buyers=social_anchors.buyers+excluded.buyers,
              vendors=social_anchors.vendors+excluded.vendors,
              sum_score=social_anchors.sum_score+excluded.sum_score,
              last_post=excluded.last_post, last_harvest=now(),
              segment=coalesce(excluded.segment, social_anchors.segment),
              audience=coalesce(excluded.audience, social_anchors.audience),
              platform=coalesce(excluded.platform, social_anchors.platform)""",
            (company_id, anchor, engagers, buyers, vendors, s, post, segment, audience, platform))


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


ALL_COMPANY_SLUGS = ["tabscanner", "sensa", "skyvision", "filmspoke", "snaprewards"]


def set_do_not_market(email: str, company: str | None, on: bool) -> dict | None:
    """DO NOT MARKET, PER COMPANY. `do_not_market` is a list of company SLUGS the contact is suppressed for
    (off ALL outbound marketing for that company: newsletter + Instantly + automation; a manual 1:1 reply is
    still fine). `company` = one slug (per-company) or ''/None/'all' (across EVERY company). on=add, off=remove."""
    ensure_schema()
    c = db.one("select id, do_not_market from crm_master where lower(email)=lower(%s)", (email,))
    if not c:
        return None
    cur = set(c["do_not_market"] or [])
    targets = set(ALL_COMPANY_SLUGS) if (not company or company in ("all", "")) else {company}
    cur = (cur | targets) if on else (cur - targets)
    db.execute("update crm_master set do_not_market=%s, updated_at=now() where id=%s", (Json(sorted(cur)), c["id"]))
    scope = "ALL companies" if (not company or company == "all") else company
    log_event(email, "do_not_market", f"Do-not-market {'ON' if on else 'removed'} for {scope}")
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


# ---- Opportunity follow-up automation — SYSTEM-WIDE. The CADENCE is config (a per-company override on the
#      profile, else this default), never code; the engine just runs whatever sequence it is handed. ----------
DEFAULT_CADENCE = {
    "skip_weekends": True,
    "steps": [
        {"after_days": 3, "repeat": 4, "action": "chase"},      # 4 chases, 3 days apart
        {"after_days": 14, "repeat": 2, "action": "checkin"},   # then 2 fortnightly check-ins
        {"after_days": 14, "repeat": 1, "action": "lost"},      # then mark the opportunity Lost
    ],
}


def _slug_for_org(label: str | None) -> str:
    for slug, lbl in ORG.items():
        if lbl == label:
            return slug
    return (label or "").lower()


def get_cadence(company_org_label: str | None) -> dict:
    """The follow-up cadence for a company — its own override on the profile (`followup_cadence`), else the
    system default. Behaviour lives in CONFIG so any company can have its own sequence without touching code."""
    try:
        from . import profile, store
        co = store.get_company_by_slug(_slug_for_org(company_org_label))
        cad = (profile.get(co["id"]) or {}).get("followup_cadence") if co else None
        if cad and cad.get("steps"):
            return cad
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_CADENCE


def cadence_points(cadence: dict) -> list[dict]:
    """Flatten the cadence steps into individual fire-points, each carrying the gap (after_days) before it."""
    pts: list[dict] = []
    for s in (cadence.get("steps") or []):
        for _ in range(int(s.get("repeat", 1))):
            pts.append({"action": s.get("action", "chase"), "after_days": int(s.get("after_days", 3))})
    return pts


def _roll_weekend(dt: datetime, skip: bool) -> datetime:
    while skip and dt.weekday() >= 5:        # 5=Sat, 6=Sun -> roll to Monday
        dt += timedelta(days=1)
    return dt


def _schedule_point(cadence: dict, idx: int, base: datetime | None = None) -> datetime | None:
    pts = cadence_points(cadence)
    if idx >= len(pts):
        return None
    base = base or datetime.now(timezone.utc)
    return _roll_weekend(base + timedelta(days=pts[idx]["after_days"]), bool(cadence.get("skip_weekends")))


def start_opportunity_followups(deal_id: int) -> dict | None:
    """Put an opportunity into AUTO and arm its first follow-up per the company cadence."""
    p = db.one("select * from crm_projects where id=%s", (deal_id,))
    if not p:
        return None
    when = _schedule_point(get_cadence(p["company"]), 0)
    db.execute("update crm_projects set automation='auto', followup_step=0, next_followup=%s, updated_at=now() where id=%s",
               (when, deal_id))
    return db.one("select * from crm_projects where id=%s", (deal_id,))


def set_opportunity_automation(deal_id: int, mode: str | None) -> dict | None:
    """'auto' re-arms the cadence; 'manual'/None stop it (manual reminders still work either way)."""
    if mode == "auto":
        return start_opportunity_followups(deal_id)
    db.execute("update crm_projects set automation=%s, next_followup=null, updated_at=now() where id=%s",
               ("manual" if mode == "manual" else None, deal_id))
    return db.one("select * from crm_projects where id=%s", (deal_id,))


def advance_followup(deal_id: int) -> dict | None:
    """Engine calls this when an auto opportunity's next_followup is due: returns the action to fire
    ('chase'/'checkin') and arms the NEXT step, or marks the opportunity Lost when the sequence ends."""
    p = db.one("select * from crm_projects where id=%s", (deal_id,))
    if not p or p.get("automation") != "auto":
        return None
    cad = get_cadence(p["company"])
    pts = cadence_points(cad)
    i = p.get("followup_step") or 0
    action = pts[i]["action"] if i < len(pts) else "lost"
    if action == "lost":
        set_project_stage(deal_id, LOST_STAGE)
        db.execute("update crm_projects set automation=null, next_followup=null, updated_at=now() where id=%s", (deal_id,))
        return {"action": "lost", "done": True, "step": i}
    nxt = _schedule_point(cad, i + 1)
    db.execute("update crm_projects set followup_step=%s, next_followup=%s, updated_at=now() where id=%s",
               (i + 1, nxt, deal_id))
    return {"action": action, "done": False, "step": i}


def _deal_name_from_inquiry(c: dict) -> str:
    """A short, punchy deal name from the enquiry context — '<Company> - <project/inquiry type>'."""
    name = ((c.get("first_name") or "") + " " + (c.get("last_name") or "")).strip()
    comp = (c.get("company_name") or name or "Lead").strip()
    note = (c.get("note") or "")[:600]
    try:
        from . import provider
        out = provider.think(
            "Generate a SHORT, punchy CRM deal name from a sales enquiry, format '<Company> - <project/inquiry "
            "type>' (e.g. 'Noon - brand launch film', 'RAK Ceramics - product video'). Max ~6 words, no quotes, "
            "no trailing punctuation. Use the company name + the kind of work. If the work is unclear, '<Company> - enquiry'.",
            f"Company: {comp}\nEnquiry: {note}", model="claude-haiku-4-5", max_tokens=24)
        t = (out or "").strip().strip('"').splitlines()[0].strip()[:80]
        if t:
            return t
    except Exception:  # noqa: BLE001
        pass
    return f"{comp} - enquiry"


def qualify_opportunity(email: str, company_slug: str, title: str | None = None) -> dict | None:
    """Qualify a lead: create an Opportunity linked to their organisation, attach the contact as primary, and
    start the Auto follow-up cadence. This is the decision point — an opportunity only exists once qualified."""
    c = db.one("select * from crm_master where lower(email)=lower(%s)", (email,))
    if not c:
        return None
    title = (title or _deal_name_from_inquiry(c)).strip()
    d = create_deal(company_slug, title, stage="Opportunity", account_id=c.get("account_id"))
    add_deal_contact(d["id"], email, primary=True)
    if (c.get("classification") or "") == "not_qualified":
        set_classification(email, "lead")        # re-qualifying clears a prior not-qualified flag
    db.execute("delete from settings where key like %s", (f"lead_fu:%:{email.lower()}",))  # the lead is now an opportunity
    q = db.setting_get(f"qual:email:{email.lower()}") or {}
    if q.get("ball_in_our_court"):                # they asked US for a proposal/quote -> we owe them: don't auto-chase
        db.execute("update crm_projects set automation='manual', next_followup=null, updated_at=now() where id=%s", (d["id"],))
        try:
            from . import reminders, store
            cidx = (store.get_company_by_slug(company_slug) or {}).get("id")
            reminders.create(f"Prepare and send the proposal — {title}",
                             _roll_weekend(datetime.now(timezone.utc) + timedelta(days=1), True),
                             company_id=cidx, target_type="deal", target_id=d["id"], priority="high")
        except Exception:  # noqa: BLE001 — the reminder is a convenience; never fail the qualify
            pass
    else:                                          # we're waiting on THEM -> start the Auto chase cadence
        start_opportunity_followups(d["id"])
    log_event(email, "qualified", f"Qualified -> opportunity #{d['id']}: {title}")
    return db.one("select * from crm_projects where id=%s", (d["id"],))


def disqualify(email: str) -> dict | None:
    """Mark a lead NOT qualified — no opportunity is created. Reversible (re-qualify any time)."""
    r = set_classification(email, "not_qualified")
    if r:
        log_event(email, "not_qualified", "Marked not qualified")
    db.execute("delete from settings where key like %s", (f"lead_fu:%:{email.lower()}",))  # stop chasing a disqualified lead
    return r


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
    org = _require_business(company)
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
    org = _require_business(company)
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
