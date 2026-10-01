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

import re
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
    """The business label DEALS are stored under (crm_projects.company: 'Sensa', 'Sky Vision'), not the company's
    display name. This returned companies.name ('Sensa Productions'), which matches no deal, so the live-work guard
    and the history block silently found nothing: nurture touches went to MAH Gold, HBMSU, Merck and SEHA on
    29 Sep 2026 while they had live deals (found 1 Oct 2026)."""
    r = db.one("select slug, name from companies where id=%s", (company_id,))
    if not r:
        return None
    try:
        from . import crm
        return crm._org(r.get("slug"))
    except Exception:  # noqa: BLE001
        return r.get("name")


def has_live_work(account_id: int, company_id: int) -> bool:
    org = _org_name(company_id)
    return bool(db.one(
        "select id from crm_projects where account_id=%s and company=%s and stage = any(%s) limit 1",
        (account_id, org, list(LIVE_STAGES))))


LOST_COOLING_DAYS = 60       # after a deal is marked Lost, the account is left alone this long
RECENT_CONTACT_DAYS = 21     # a meeting, call note or email this recently means no "it has been a while" touch
_WON = ("Booked", "Production", "Recurring", "Delivered", "Final Payment", "Close & review", "Nurture", "Completed")
_CONTACT_EVENTS = ("email_in", "email_out", "email_out_manual", "meeting", "note", "context", "conversation")
# notes Cortex writes itself are bookkeeping, not contact with the client
_SYSTEM_NOTE = re.compile(r"(?:Automatic|Proposal|No |Contact |Cancelled|Draft |Quotation|Moved to|Photography|Filed|"
                          r"Pricing|Scheduled|Reminder|Follow-up|The meeting resumed|Deck|Creative proposal)", re.I)


def _account_state(account_id: int, company_id: int) -> dict:
    """What the account's own deals say right now: when we were last in touch (any email, meeting or owner note on
    any of its deals), when a deal was last marked Lost, whether we have ever WON work with them, and the recent
    notes. Card 1020 (Brandgate, 30 Sep 2026) was drafted two days after their deal was marked Lost and the owner
    had met them in person: nurture resumed the moment the live deal closed and knew none of it."""
    org = _org_name(company_id)
    rows = db.query("select id, title, stage, history, note from crm_projects where account_id=%s and company=%s "
                    "order by updated_at desc limit 8", (account_id, org))
    last_contact = lost_at = None
    notes = []
    for r in rows:
        for ev in (r.get("history") or []):
            ts, kind = str(ev.get("ts") or ""), str(ev.get("event") or "")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            text = str(ev.get("text") or "")
            system = kind == "note" and bool(_SYSTEM_NOTE.match(text))
            if kind in _CONTACT_EVENTS and not system and (last_contact is None or when > last_contact):
                last_contact = when
            if kind == "stage_change" and text.strip().endswith("-> Lost") and (lost_at is None or when > lost_at):
                lost_at = when
            if kind in ("note", "context", "conversation") and text and not system:
                notes.append((when, r["title"], text[:400]))
    notes.sort(key=lambda x: x[0], reverse=True)
    meetings = []
    try:   # what was actually said in meetings with them (owner, 1 Oct 2026): the distilled briefs, not the transcripts
        ids = [int(r["id"]) for r in rows]
        _seen: list = []
        _titles: list = []
        if ids:
            for m in db.query("select title, starts_at, summary from meeting_notes where deal_id = any(%s) and "
                              "coalesce(summary,'') <> '' order by starts_at desc nulls last, id desc limit 8", (ids,)):
                # one meeting is often on file twice (Gemini's email and the calendar doc): keep the first of each
                w = set(re.findall(r"[a-z]{4,}", str(m["summary"]).lower()))
                tk = re.sub(r"[^a-z]", "", str(m.get("title") or "").lower())[:40]
                if any(len(w & k) / max(1, min(len(w), len(k))) > 0.6 for k in _seen) or any(
                        tk and o and (tk.startswith(o) or o.startswith(tk)) for o in _titles):
                    continue
                _seen.append(w)
                _titles.append(tk)
                meetings.append((m.get("starts_at"), m.get("title") or "Meeting", str(m["summary"])[:1200]))
                if len(meetings) >= 3:
                    break
    except Exception:  # noqa: BLE001
        meetings = []
    return {"meetings": meetings, "last_contact": last_contact, "lost_at": lost_at,
            "won": any(r.get("stage") in _WON for r in rows),
            "has_deals": bool(rows),       # no deal rows at all = a past client who predates Cortex (manual enrolment)
            "lost_titles": [r["title"] for r in rows if r.get("stage") == "Lost"],
            "notes": notes[:6]}


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
            st = _account_state(n["account_id"], n["company_id"])
            now = datetime.now(timezone.utc)
            wait_until = None
            if st["lost_at"] and now - st["lost_at"] < timedelta(days=LOST_COOLING_DAYS):
                wait_until = st["lost_at"] + timedelta(days=LOST_COOLING_DAYS)       # a job just lost: leave them be
            if st["last_contact"] and now - st["last_contact"] < timedelta(days=RECENT_CONTACT_DAYS):
                _rc = st["last_contact"] + timedelta(days=RECENT_CONTACT_DAYS)        # we were just in touch
                wait_until = max(wait_until, _rc) if wait_until else _rc
            if wait_until:
                db.execute("update nurture_accounts set next_touch=%s where id=%s", (wait_until, n["id"]))
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
            try:   # NURTURE GATE: the thread and the owner's notes decide whether a touch makes sense now
                from . import engine as _eng, provider as _pv
                _co = store.get_company(n["company_id"]) or {}
                _msgs = _eng._deal_thread_msgs(_co, email, limit=4)
                _recent = "\n---\n".join(
                    f"[{x.get('date') or ''} | from {x.get('email') or x.get('from') or ''}] "
                    + re.sub(r"\s+", " ", x.get("body") or x.get("snippet") or "")[:700] for x in (_msgs or [])[:4])
                _g = _pv.think_json(
                    "A monthly relationship-nurture email is due for a contact we are not currently working with. Read "
                    "the newest emails and the owner's notes. Should we send it NOW? No when: we were in touch with them "
                    "in the last few weeks; they just turned us down or a job was just lost; they asked for space or "
                    "gave a date they will come back; their last message is waiting for OUR answer. "
                    'Return {"send": true|false, "wait_days": <14-90>, "why": "<one line>"}. When unsure, send.',
                    "TODAY: " + now.strftime("%d %b %Y") + "\nNEWEST EMAILS:\n" + (_recent or "(none)")
                    + "\n\nOWNER NOTES:\n" + "\n".join(f"- {w:%d %b %Y} {x}" for w, _t, x in st["notes"])
                    + "\n\nMEETINGS:\n" + "\n".join(
                        f"- {(w.strftime('%d %b %Y') if w else 'undated')} {t}: {x[:400]}" for w, t, x in st["meetings"]),
                    model=_pv.MODEL_ROUTER, purpose="nurture-gate", company=_slug)
                if isinstance(_g, dict) and _g.get("send") is False:
                    _wd = max(14, min(90, int(_g.get("wait_days") or 30)))
                    db.execute("update nurture_accounts set next_touch = now() + (%s || ' days')::interval where id=%s",
                               (str(_wd), n["id"]))
                    held.append(n["id"])
                    continue
            except Exception:  # noqa: BLE001 - on any doubt the touch is drafted as before
                pass
            sk = store.get_skill_by_key(n["company_id"], "sales-followup") \
                or store.get_skill_by_key(n["company_id"], "sales-first-response")
            c = _named(n.get("contact_email")) or _best_contact(n["account_id"], _slug) or {}
            name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) if c.get("email") == email else ""
            t = store.create_card(n["company_id"], sk["id"], "email_reply", {
                "brief": (f"NURTURE touch for {(acc or {}).get('name')} - "
                          + ("a past client we are keeping warm between projects. "
                             if (st["won"] or not st["has_deals"]) else
                             "a contact we are staying in touch with. We have NOT done paid work for them yet: never "
                             "write as if they are a client or as if we delivered something for them. ")
                          + "The REPEAT-NURTURE standing rules on sales-followup govern this "
                          "email (warm, no pressure, door open for anything coming up).\n"
                          "OUR HISTORY WITH THEM:\n" + _history_block(n["account_id"], n["company_id"])
                          + ("\nLOST WORK IS CLOSED: " + "; ".join(st["lost_titles"][:3]) + ". Never offer to revisit, "
                             "re-quote or re-scope a lost job and never ask whether it is still moving; at most one "
                             "plain acknowledgement, then look forward." if st["lost_titles"] else "")
                          + ("\nWHAT WE KNOW (the owner's own notes on their deals, newest first; treat as fact, and "
                             "never say it has been a while if these show recent contact):\n"
                             + "\n".join(f"- {w:%d %b %Y} [{t[:50]}] {x}" for w, t, x in st["notes"]) if st["notes"] else "")
                          + ("\nMEETINGS WITH THEM (distilled from the real meeting notes, newest first; ground the "
                             "email in what was actually discussed and agreed, never contradict it, and do not recap "
                             "the meeting back to them):\n"
                             + "\n".join(f"- {(w.strftime('%d %b %Y') if w else 'undated')} {t}:\n{x}" for w, t, x in st["meetings"])
                             if st["meetings"] else "")
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
