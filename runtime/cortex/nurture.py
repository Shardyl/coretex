"""Nurture — the RELATIONSHIP layer (its own section: not an opportunity, not a project).

One row per (client account × our business): a client we've done good work for, kept warm between
projects with a monthly touch (owner: 90 days is too long, 30 is a good hello). HARD GUARD: any live deal or running project with that client
silences the loop automatically (we never nurture someone we're actively working with or pitching);
it resumes by itself when the work closes. Touch emails are normal approval cards on the
sales-followup skill — the repeat-nurture standing rules govern content, this module only keeps
the clock and the roster. Accounts can be enrolled automatically (a project reaching the Nurture
stage) or manually (past clients who predate Cortex).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db, store

# stages that mean "live work exists with this client" -> nurture stays silent
LIVE_STAGES = ("Opportunity", "Quote", "Booked", "Production", "Recurring",
               "Delivered", "Final Payment", "Close & review")

_SCHEMA = """
create table if not exists nurture_accounts (
  id bigserial primary key,
  account_id bigint not null references crm_accounts(id),
  company_id bigint not null references companies(id),
  status text not null default 'active',        -- active | stopped
  cadence_days int not null default 30,
  contact_email text,                            -- preferred person; null = best from the account roster
  next_touch timestamptz,
  last_touch timestamptz,
  enrolled_at timestamptz not null default now(),
  enrolled_from text,                            -- 'deal:<id>' | 'manual'
  cc_emails jsonb,                               -- people who ride cc on every touch (e.g. internal approver)
  note text,
  unique (account_id, company_id)
);
"""


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_SCHEMA)


def _org_name(company_id: int) -> str | None:
    r = db.one("select name from companies where id=%s", (company_id,))
    return (r or {}).get("name")


def has_live_work(account_id: int, company_id: int) -> bool:
    org = _org_name(company_id)
    return bool(db.one(
        "select id from crm_projects where account_id=%s and company=%s and stage = any(%s) limit 1",
        (account_id, org, list(LIVE_STAGES))))


def enrol(account_id: int, company_id: int, *, contact_email: str | None = None,
          enrolled_from: str = "manual", cadence_days: int = 30, note: str = "",
          channel: str = "email") -> dict:
    """Upsert-enrol an account; an existing row keeps its clock (re-enrolling never resets a cadence)."""
    ensure_schema()
    nxt = datetime.now(timezone.utc) + timedelta(days=cadence_days)
    return db.execute(
        "insert into nurture_accounts (account_id, company_id, contact_email, next_touch, enrolled_from, "
        "cadence_days, note, channel) values (%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (account_id, company_id) do update set status='active', "
        "contact_email=coalesce(excluded.contact_email, nurture_accounts.contact_email), "
        "note=coalesce(nullif(excluded.note,''), nurture_accounts.note) returning *",
        (account_id, company_id, contact_email, nxt, enrolled_from, cadence_days, note,
         channel if channel in ("email", "whatsapp") else "email"))


def stop(row_id: int) -> dict | None:
    return db.execute("update nurture_accounts set status='stopped' where id=%s returning *", (row_id,))


def resume(row_id: int) -> dict | None:
    return db.execute("update nurture_accounts set status='active', "
                      "next_touch=coalesce(next_touch, now() + interval '7 days') where id=%s returning *",
                      (row_id,))


def _named(email: str) -> dict | None:
    """The person the owner actually named on the nurture row. Their name must come from THIS record,
    not from whoever the account touched last - Tatweer's most recent contact is their finance inbox,
    so a touch meant for Noor was addressed to '.' (31 Aug 2026)."""
    if not (email or "").strip():
        return None
    return db.one("select first_name, last_name, email from crm_master where lower(email)=lower(%s) "
                  "limit 1", (email.strip(),))


def _best_contact(account_id: int, slug: str = "") -> dict | None:
    """The account's best current person: the most recently updated contact with an email who has NOT
    opted out of this company's marketing.

    `do_not_market` is a jsonb ARRAY of company slugs, not a boolean - comparing it to false raised
    every time, and the sweep's catch-all swallowed it, so NO nurture touch could ever fire (found
    31 Aug 2026, before the first touches were due)."""
    return db.one(
        "select first_name, last_name, email from crm_master where account_id=%s and "
        "coalesce(email,'')<>'' and not coalesce(do_not_market, '[]'::jsonb) @> %s::jsonb "
        "order by updated_at desc nulls last limit 1",
        (account_id, f'["{slug}"]' if slug else '["__none__"]'))


def _history_block(account_id: int, company_id: int) -> str:
    org = _org_name(company_id)
    rows = db.query(
        "select title, stage, coalesce(value,0) v, coalesce(currency,'AED') c, updated_at::date d "
        "from crm_projects where account_id=%s and company=%s order by updated_at desc limit 8",
        (account_id, org))
    return "\n".join(f"- {r['title']} ({r['stage']}, {r['v']:.0f} {r['c']}, last activity {r['d']})"
                     for r in rows) or "(no project rows on record - relationship predates Cortex)"


def sweep() -> dict:
    """Hourly: spawn due nurture touches. A client with live work is skipped and re-checked in 14 days
    (the loop resumes on its own once the work closes). One open touch card per contact at a time —
    store.create_card's conveyor handles that."""
    ensure_schema()
    spawned, held = [], []
    due = db.query("select * from nurture_accounts where status='active' and next_touch <= now()")
    for n in due:
        try:
            if has_live_work(n["account_id"], n["company_id"]):
                db.execute("update nurture_accounts set next_touch = now() + interval '14 days' where id=%s",
                           (n["id"],))
                held.append(n["id"])
                continue
            acc = db.one("select name from crm_accounts where id=%s", (n["account_id"],))
            _slug = (store.get_company(n["company_id"]) or {}).get("slug") or ""
            email = ((n.get("contact_email") or "").strip()
                     or ((_best_contact(n["account_id"], _slug) or {}).get("email")))
            # advance the clock FIRST (reminder doctrine: a failure must never refire every tick)
            db.execute("update nurture_accounts set next_touch = now() + (cadence_days || ' days')::interval, "
                       "last_touch = now() where id=%s", (n["id"],))
            if not email:
                from . import notifications
                notifications.notify(
                    f"Nurture touch due for {(acc or {}).get('name')} but no contact has an email - "
                    "add one to the account.", "Nurture needs a contact", priority="normal",
                    category="reminder", company_id=n["company_id"])
                continue
            if (n.get("channel") or "email") == "whatsapp":
                # WHATSAPP RELATIONSHIPS (owner, 31 Aug 2026): some people are only ever messaged, never
                # emailed. Cortex does not send WhatsApp - it hands Rashad the nudge and a suggested
                # opening line, and he sends it himself.
                from . import notifications, provider
                _c = _named(n.get("contact_email")) or _best_contact(n["account_id"], _slug) or {}
                _who = " ".join(x for x in (_c.get("first_name"), _c.get("last_name")) if x)                     or (acc or {}).get("name") or "them"
                line = None
                try:
                    line = provider.think_json(
                        "Write ONE short, warm WhatsApp opener Rashad can send a past client he has not "
                        "spoken to in a while. Natural texting tone, no marketing, no pitch, under 30 "
                        'words. Return {"text": "<the message>"}.',
                        f"Client: {(acc or {}).get('name')}. Contact: {_who}. "
                        f"Relationship notes: {n.get('note') or 'past client, work delivered'}",
                        model=provider.MODEL_FAST, max_tokens=150, purpose="whatsapp-nudge")
                except Exception:  # noqa: BLE001
                    line = None
                msg = ((line or {}).get("text") or "").strip()
                notifications.notify(
                    f"WhatsApp {_who} to keep the relationship warm."
                    + (f' Suggested: "{msg}"' if msg else ""),
                    f"Message {(acc or {}).get('name')}", priority="normal", category="reminder",
                    company_id=n["company_id"])
                spawned.append(f"whatsapp-nudge:{n['id']}")
                continue
            sk = store.get_skill_by_key(n["company_id"], "sales-followup") \
                or store.get_skill_by_key(n["company_id"], "sales-first-response")
            c = _named(n.get("contact_email")) or _best_contact(n["account_id"], _slug) or {}
            name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) if c.get("email") == email else ""
            t = store.create_card(n["company_id"], sk["id"], "email_reply", {
                "brief": (f"NURTURE touch for {(acc or {}).get('name')} - a past client we are keeping warm "
                          "between projects. The REPEAT-NURTURE standing rules on sales-followup govern this "
                          "email (warm, no pressure, remind of the work, door open for anything coming up).\n"
                          "OUR HISTORY WITH THEM:\n" + _history_block(n["account_id"], n["company_id"])
                          + (f"\nNOTE ON THE RELATIONSHIP: {n['note']}" if n.get("note") else "")),
                "inquiry": {"name": name, "email": email, "message": ""},
                **({"cc_extra": [e for e in (n.get("cc_emails") or []) if "@" in str(e)]}
                   if n.get("cc_emails") else {}),
                "followup": "nurture",
                "system_note": "Quarterly account-level nurture touch (no live work with this client)."},
                contact=email)
            if t:
                spawned.append(t["id"])
        except Exception:  # noqa: BLE001 - one bad row never blocks the sweep
            continue
    return {"spawned": spawned, "held_live_work": held}


def listing(company_id=None) -> list[dict]:
    ensure_schema()
    where, params = "", ()
    if company_id is not None:
        cids = list(company_id) if isinstance(company_id, (list, tuple)) else [company_id]
        where, params = "where n.company_id = any(%s)", (cids,)
    rows = db.query(f"""
        select n.*, a.name as account_name, co.name as business,
          (select count(*) from crm_master m where m.account_id=n.account_id and coalesce(m.email,'')<>'') contacts,
          (select count(*) from crm_projects p where p.account_id=n.account_id and p.company=co.name
             and p.stage in ('Booked','Production','Recurring','Delivered','Final Payment','Close & review','Nurture')) won_projects,
          (select coalesce(sum(p.value),0) from crm_projects p where p.account_id=n.account_id and p.company=co.name
             and p.stage in ('Booked','Production','Recurring','Delivered','Final Payment','Close & review','Nurture')) won_value
        from nurture_accounts n
        join crm_accounts a on a.id=n.account_id join companies co on co.id=n.company_id
        {where} order by n.next_touch nulls last""", params)
    for r in rows:
        r["live_work"] = has_live_work(r["account_id"], r["company_id"])
    return rows
