"""PERSONAL TOUCH: WhatsApp and calls beside the email track (owner, 13 Oct 2026).

"There is a lot of merit in me asking these people how they're doing, whether they want to hook up for a coffee,
trying to get relationships on WhatsApp and calls." Cortex never messages them on WhatsApp and never calls; it
makes sure the owner does, and keeps the deal record straight when he has:

  1. START: when a new Sensa / Sky Vision lead becomes an opportunity (the first reply is drafted at the same moment),
     the number is read from their signature or form (CODE, Google's libphonenumber; a model never supplies a digit),
     saved on the CRM contact and the deal (never overwriting one), the person is added to his personal Google
     Contacts tagged "<Name> (Sensa enquiry, 13 Oct)" so WhatsApp on his phone knows them, and the first reminder
     fires at once: "WhatsApp or call Sarah Khan, Emaar" with tap links. A WhatsApp enquiry starts when its
     acknowledgement is sent (it is already in his contacts by then).
  2. HE LOGS what happened as a note on the opportunity (cockpit, Talk, or a WhatsApp message to Cortex). on_note
     asks ONE narrow question of his words (did contact happen, and is a next contact stated); code resolves the
     date and moves the next touch there. A logged contact is a `touch_made` event, which the email gate counts as
     contact, so the email chase does not land two days after he spoke to them.
  3. RHYTHM otherwise: the next touch after `rhythm()` days (profile `touch_rhythm`, default 7, 21, then 30 repeating)
     while the deal is in a sales stage. Leaving the sales stages cancels the pending touch (obligations.on_stage);
     a closed deal cancels it like every Cortex reminder.

One pending touch reminder per deal (created_by 'cortex-touch'); the track's facts (number, name, step) are in
setting touch:<deal_id>.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import db

CREATOR = "cortex-touch"
DEFAULT_RHYTHM = [7, 21, 30]
_QUOTED = re.compile(r"^\s*(?:On .{5,200}wrote:|From:\s|-{2,}\s*Original Message|_{5,}|>)", re.M | re.I)
_LABEL_MOBILE = re.compile(r"(?:mob(?:ile)?|cell|whats\s?app|\bm\b|\bwa\b)\s*[:.]?\s*$", re.I)


def companies() -> list:
    """Which of our businesses run the touch track: data, not code (owner: Sensa, and Sky Vision)."""
    return db.setting_get("touch_companies") or ["sensa", "skyvision"]


def rhythm(company_id) -> list:
    try:
        from . import profile
        r = (profile.get(company_id) or {}).get("touch_rhythm")
        if isinstance(r, list) and r and all(isinstance(x, (int, float)) and x > 0 for x in r):
            return [int(x) for x in r]
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_RHYTHM


def _gap(company_id, step: int) -> int:
    r = rhythm(company_id)
    return r[min(step, len(r) - 1)]


# ---------------------------------------------------------------- the number

def numbers_in(text: str, region: str = "AE") -> list[dict]:
    """Every real phone number in the text above any quoted reply, in order: {e164, mobile, labelled}. Toll-free and
    premium numbers are left out (nobody is on WhatsApp at 800). A quotation number or a date is never a number here:
    libphonenumber only accepts a valid number for a real numbering plan."""
    import phonenumbers as pn
    t = text or ""
    m = _QUOTED.search(t)
    if m:
        t = t[:m.start()]
    out, seen = [], set()
    for match in pn.PhoneNumberMatcher(t, region, leniency=pn.Leniency.VALID):
        n = match.number
        kind = pn.number_type(n)
        if kind in (pn.PhoneNumberType.TOLL_FREE, pn.PhoneNumberType.PREMIUM_RATE, pn.PhoneNumberType.SHARED_COST):
            continue
        e164 = pn.format_number(n, pn.PhoneNumberFormat.E164)
        if e164 in seen:
            continue
        seen.add(e164)
        out.append({"e164": e164, "mobile": kind in (pn.PhoneNumberType.MOBILE, pn.PhoneNumberType.FIXED_LINE_OR_MOBILE),
                    "labelled": bool(_LABEL_MOBILE.search(t[max(0, match.start - 14):match.start]))})
    return out


def pick(numbers: list[dict]) -> dict | None:
    """The number to WhatsApp: a mobile labelled mobile/WhatsApp, else any mobile, else the first number (call only)."""
    for test in (lambda n: n["mobile"] and n["labelled"], lambda n: n["mobile"], lambda n: True):
        hit = next((n for n in numbers if test(n)), None)
        if hit:
            return hit
    return None


def normalise(phone: str) -> dict | None:
    """One stored number as {e164, mobile, labelled}, or None when it is not a valid number."""
    got = numbers_in(phone or "")
    return got[0] if got else None


def _find_number(email: str, deal: dict, texts) -> dict | None:
    for t in texts or []:
        n = pick(numbers_in(t or ""))
        if n:
            return n
    if email:
        c = db.one("select phone from crm_master where lower(email)=lower(%s)", (email,)) or {}
        n = normalise(c.get("phone") or "")
        if n:
            return n
    for c in (deal.get("contacts") or []):
        n = normalise(c.get("phone") or "")
        if n:
            return n
    return None


def _save_number(deal: dict, email: str, num: dict) -> None:
    """On the CRM contact and the deal's contact entry, only where none is held: a number he typed is never replaced."""
    if email:
        db.execute("update crm_master set phone=%s where lower(email)=lower(%s) and coalesce(phone,'')=''",
                   (num["e164"], email))
    lst = deal.get("contacts") or []
    changed = False
    for c in lst:
        if email and (c.get("email") or "").lower() == email.lower() and not c.get("phone"):
            c["phone"], changed = num["e164"], True
    if changed:
        from psycopg.types.json import Json
        db.execute("update crm_projects set contacts=%s where id=%s", (Json(lst), deal["id"]))


# ---------------------------------------------------------------- the track

def _state(deal_id) -> dict:
    return db.setting_get(f"touch:{int(deal_id)}") or {}


def _who(st: dict) -> str:
    return ", ".join(x for x in (st.get("name") or "", st.get("org") or "") if x) or (st.get("phone") or "them")


def _schedule(company_id, deal_id, due: datetime, st: dict) -> dict:
    from . import reminders
    db.execute("update reminders set status='cancelled' where status in ('pending','snoozed') and created_by=%s "
               "and target_type='deal' and target_id=%s", (CREATOR, str(int(deal_id))))
    verb = "WhatsApp or call" if st.get("mobile") else ("Call" if st.get("phone") else "Get in touch with")
    return reminders.create(f"{verb} {_who(st)}", due, company_id=company_id, target_type="deal",
                            target_id=int(deal_id), created_by=CREATOR)


def start(co: dict, deal_id, *, email: str = "", name: str = "", org: str = "", texts=(), phone: str = "",
          push_contact: bool = True, source: str = "") -> dict:
    """Begin the personal-touch track on a new opportunity. Idempotent: a deal starts once."""
    from . import pipeline, stages
    if not co or (co.get("slug") or "") not in companies() or not str(deal_id or "").isdigit():
        return {"skipped": "not a touch company"}
    deal = db.one("select id, title, stage, contacts, contact_email from crm_projects where id=%s", (int(deal_id),))
    if not deal or deal.get("stage") not in stages.SALES:
        return {"skipped": "not an open opportunity"}
    if _state(deal["id"]):
        return {"skipped": "already started"}
    email = (email or deal.get("contact_email") or "").strip().lower()
    if not org:
        a = db.one("select a.name from crm_projects p join crm_accounts a on a.id=p.account_id where p.id=%s",
                   (deal["id"],)) or {}
        org = a.get("name") or ""
    num = normalise(phone) if phone else None
    num = num or _find_number(email, deal, texts)
    if not num:
        # NO NUMBER, NO TRACK (owner, 13 Oct 2026: "if you were able to scrape the contact's phone number, then you
        # should add them"). A reminder to call someone with no number is noise; the email track carries them.
        pipeline.log_deal(deal["id"], "note", "Personal touch not started: no phone number in their signature, "
                                              "form or the CRM. Add one to the contact and ask Talk to start it.")
        return {"skipped": "no number"}
    # ONE PERSON, ONE WEEKLY TOUCH: Farheen is on two Shama deals and Mai on two MAH Gold deals; a check-in is with
    # the person, so a second deal with the same number joins nothing (its notes still reach the person's track).
    other = db.one("select key from settings where key like 'touch:%%' and key <> %s and value->>'phone' = %s limit 1",
                   (f"touch:{deal['id']}", num["e164"]))
    if other:
        db.setting_set(f"touch_alias:{deal['id']}", int(other["key"].split(":")[1]))
        pipeline.log_deal(deal["id"], "note", f"Personal touch: {num['e164']} is already checked in with weekly on "
                                              f"opportunity #{other['key'].split(':')[1]}.")
        return {"skipped": "same person on " + other["key"]}
    _save_number(deal, email, num)
    st = {"name": (name or "").strip(), "org": (org or "").strip(), "email": email, "step": 0,
          "phone": (num or {}).get("e164") or "", "mobile": bool((num or {}).get("mobile")),
          "started": datetime.now(timezone.utc).isoformat()}
    db.setting_set(f"touch:{deal['id']}", st)
    pushed = ""
    if num and push_contact:
        try:
            from . import contacts
            r = contacts.add_enquiry(num["e164"], name=st["name"], company=st["org"], brand=co.get("name") or "",
                                     note=f"Opportunity #{deal['id']}: {deal['title']}", source=source)
            pushed = "added to your phone contacts" if r.get("created") else (
                "already in your phone contacts" if r.get("exists") else f"not added to contacts ({r.get('skipped') or r.get('error')})")
        except Exception as e:  # noqa: BLE001 - the CRM and the deal are the record
            pushed = f"not added to contacts ({str(e)[:80]})"
    pipeline.log_deal(deal["id"], "touch_started", (
        f"Personal touch started for {_who(st)}: " + (f"{'mobile' if st['mobile'] else 'phone'} {st['phone']}"
                                                      + (f", {pushed}" if pushed else "") if st["phone"]
                                                      else "no number found in their signature or the CRM")))
    _schedule(co["id"], deal["id"], datetime.now(timezone.utc), st)
    return {"started": True, "phone": st["phone"], "mobile": st["mobile"], "contacts": pushed}


def fire(r: dict) -> int | None:
    """A touch reminder is due: the owner's notice, with tap links, then the next touch on the rhythm."""
    from . import notifications, stages
    try:
        deal_id = int(r.get("target_id"))
    except (TypeError, ValueError):
        return None
    deal = db.one("select id, title, stage, history from crm_projects where id=%s", (deal_id,))
    if not deal or deal.get("stage") not in stages.SALES:
        return None
    st = _state(deal_id)
    notes = [e for e in (deal.get("history") or []) if e.get("event") == "context" and e.get("text")]
    lines = [f"Opportunity: {deal['title']}."]
    if notes:
        lines.append("Last note: " + notes[-1]["text"][:240])
    digits = re.sub(r"\D", "", st.get("phone") or "")
    if digits and st.get("mobile"):
        lines.append(f"WhatsApp: https://wa.me/{digits}")
    if digits:
        lines.append(f"tel:+{digits}")
    else:
        lines.append("No number on file: add one to the contact, or reply by email only.")
    lines.append("Log what was said as a note on the opportunity; the next touch follows it.")
    # ONE CARD FOR THE DAY'S CALLS (owner, 15 Oct 2026: seven separate "WhatsApp or call" notices was noise): each
    # touch is a line on "Calls and WhatsApps today", with its own call and WhatsApp buttons and the opportunity.
    day = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=4))).date().isoformat()
    item = {"name": r["title"].replace("WhatsApp or call ", "").replace("Call ", ""), "cat": "call",
            "deal_id": deal_id, "deal": deal["title"][:80],
            **({"wa": f"https://wa.me/{digits}"} if digits and st.get("mobile") else {}),
            **({"tel": f"tel:+{digits}"} if digits else {})}
    n = notifications.notify("Calls and WhatsApps today",
                             "Log what was said as a note on each opportunity; the next touch follows it.",
                             priority="normal", category="calls", company_id=r.get("company_id"),
                             dedup_key=f"touch-day:{r.get('company_id')}:{day}", item=item)
    st["step"] = int(st.get("step") or 0) + 1
    db.setting_set(f"touch:{deal_id}", st)
    _schedule(r.get("company_id"), deal_id, datetime.now(timezone.utc) + timedelta(days=_gap(r.get("company_id"), st["step"] - 1)), st)
    return (n or {}).get("id")


_NOTE_Q = ("This is the owner's own note on a sales opportunity. Answer narrow questions about it, from its words "
           "only. contacted: true if it records that he (or we) actually spoke, called, messaged on WhatsApp or met the "
           "client; false for anything else (a budget, a preference, a fact). next: the exact words that say WHEN to "
           "follow up or contact them next ('next Tuesday', 'after Eid', 'in two weeks', 'chase Thursday'), or null if "
           "the note does not say. channel: 'email' if that next follow-up is an email or a chase, 'call' if it is a "
           "call, WhatsApp or meeting, 'any' if it does not say. every_days: a number only if the note sets a "
           "repeating frequency ('chase every 3 days' = 3, 'weekly' = 7), else null. "
           'Return {"contacted": true|false, "next": "<their words>"|null, "channel": "email"|"call"|"any", '
           '"every_days": <int>|null}.')


def on_note(deal_id, text: str) -> str:
    """The owner logged a note on the opportunity: a contact made is recorded, and the next touch moves to the date
    the note states (or the rhythm from now when contact was made). Returns a line to tell him, or ''."""
    from . import engine, pipeline, provider, stages, store
    if not str(deal_id or "").isdigit() or not (text or "").strip():
        return ""
    try:
        r = provider.think_json(_NOTE_Q, text[:2000], model=provider.MODEL_ROUTER, max_tokens=100, purpose="touch-note")
    except Exception:  # noqa: BLE001
        return ""
    r = r if isinstance(r, dict) else {}
    said = []
    # THE EMAIL CHASE FOLLOWS HIS NOTE TOO (owner, 13 Oct 2026: "a note or a voice note ... instantly read by Cortex
    # and updated with a chase up or draft frequency"). Code resolves the date; the model only quotes his words.
    if (r.get("next") and r.get("channel") in ("email", "any")) or r.get("every_days"):
        from . import crm, reminders
        try:
            when = reminders.parse_when(str(r["next"])) if r.get("next") else None
            ev = int(r["every_days"]) if str(r.get("every_days") or "").isdigit() and 0 < int(r["every_days"]) <= 90 else None
            if when or ev:
                crm.set_next_chase(int(deal_id), when, ev, by="owner's note", why=text[:160])
                said.append("Next email chase: " + (f"{when.astimezone(timezone(timedelta(hours=4))):%a %d %b}" if when else "")
                            + (f", then every {ev} days" if ev else "") + ".")
        except Exception as e:  # noqa: BLE001
            print(f"[touch] chase from note on deal {deal_id}: {type(e).__name__}: {e}", flush=True)
    if r.get("channel") == "email" and not r.get("contacted"):
        return " ".join(said)              # an email instruction does not move the WhatsApp/call track
    alias = db.setting_get(f"touch_alias:{int(deal_id)}")   # the same person's track lives on their other deal
    if alias and not _state(deal_id):
        deal_id = int(alias)
    st = _state(deal_id)
    deal = db.one("select id, stage, company from crm_projects where id=%s", (int(deal_id),))
    if not st or not deal or deal.get("stage") not in stages.SALES:
        return " ".join(said)
    from . import crm
    co = next((store.get_company_by_slug(s) for s in companies()
               if (deal.get("company") or "").lower() in (s, crm._org(s).lower())), None) or {}
    when = None
    if r.get("next"):
        when = engine._resolve_their_date(co, str(r["next"]), datetime.now(timezone.utc).date().isoformat())
    if r.get("contacted"):
        pipeline.log_deal(int(deal_id), "touch_made", f"Contact made (owner's note): {text[:200]}")
        st["step"] = max(int(st.get("step") or 0), 1)
        db.setting_set(f"touch:{int(deal_id)}", st)
        if not when:                       # spoke to them, no date said: the rhythm runs on from today
            when = datetime.now(timezone.utc) + timedelta(days=_gap(co.get("id"), st["step"] - 1))
    if not when:
        return " ".join(said)
    _schedule(co.get("id"), int(deal_id), when, st)
    return " ".join(said + [f"Next WhatsApp/call reminder: {when.astimezone(timezone(timedelta(hours=4))):%a %d %b}."])


# ---------------------------------------------------------------- retro: every open opportunity (owner, 13 Oct 2026)

_TENDER = re.compile(r"\b(tender|rfq|rfp|rft|eoi)\b", re.I)


def _slug_for(company_label: str) -> str:
    from . import crm
    return next((s for s in companies() if (company_label or "").lower() in (s, crm._org(s).lower())), "")


def _number_from_mail(slug: str, addrs: list) -> tuple:
    """(number, address) from the newest emails these people sent us that carry one in their signature."""
    from . import engine, gmail
    for entry in engine.inbox_registry():
        if entry.get("slug") != slug or not engine._inbox_connected(entry):
            continue
        rt = entry.get("rt_key") or engine._default_rt_key(slug)
        for a in addrs:
            try:
                msgs = gmail.list_recent(limit=5, rt_key=rt, q=f"from:{a} newer_than:365d",
                                         company=engine._inbox_client_company(slug))
            except Exception:  # noqa: BLE001 - one unreadable mailbox never stops the rest
                continue
            for m in msgs:
                n = pick(numbers_in(m.get("body") or ""))
                if n:
                    return n, a
    return None, ""


def backfill(apply: bool = False, per_day: int = 7) -> list[dict]:
    """Put every open Sensa / Sky Vision opportunity with a findable number on the track. Tenders and portal bids
    are left out (nobody WhatsApps a procurement portal). Numbers come from the deal, the CRM, then the signatures
    of the newest emails they sent us. First reminders are spread over the coming working days at 10:00 Dubai,
    `per_day` a day, so the backlog does not land at once. apply=False is the dry run."""
    from . import identity, stages, store
    from zoneinfo import ZoneInfo
    rows = db.query("select id, title, company, stage, contact_email, contacts from crm_projects where stage = any(%s) "
                    "order by id", (list(stages.SALES),))
    plan = []
    for d in rows:
        slug = _slug_for(d.get("company") or "")
        if not slug or _TENDER.search(d.get("title") or "") or _state(d["id"]):
            continue
        addrs = [a for a in dict.fromkeys([(d.get("contact_email") or "").lower()] +
                                          [(c.get("email") or "").lower() for c in (d.get("contacts") or [])])
                 if a and "@" in a and not identity.is_internal(a)]
        num, who = _find_number(addrs[0] if addrs else "", d, []), (addrs[0] if addrs else "")
        src = "deal or CRM"
        if not num:
            for a in addrs[1:]:
                num = _find_number(a, {"contacts": []}, [])
                if num:
                    who = a
                    break
        if not num and addrs:
            num, who = _number_from_mail(slug, addrs)
            src = "email signature"
        plan.append({"deal": d["id"], "title": d["title"], "company": slug, "email": who,
                     "phone": (num or {}).get("e164") or "", "mobile": bool((num or {}).get("mobile")), "source": src})
    if apply:
        tz = ZoneInfo("Asia/Dubai")
        day, n = datetime.now(tz).date(), 0
        for p in plan:
            if not p["phone"]:
                continue
            c = db.one("select first_name, last_name from crm_master where lower(email)=%s", (p["email"],)) or {}
            name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
            co = store.get_company_by_slug(p["company"])
            r = start(co, p["deal"], email=p["email"], name=name, phone=p["phone"], source="Open opportunity")
            p["result"] = r
            if not r.get("started"):
                continue
            workdays = [day + timedelta(days=i) for i in range(1, 30) if (day + timedelta(days=i)).weekday() < 5]
            d2 = workdays[min(n // max(1, per_day), len(workdays) - 1)]     # Monday to Friday, UAE week
            _schedule(co["id"], p["deal"], datetime(d2.year, d2.month, d2.day, 10, 0, tzinfo=tz), _state(p["deal"]))
            p["first"] = d2.isoformat()
            n += 1
    return plan
