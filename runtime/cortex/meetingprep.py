"""PRE-MEETING BRIEF — one page of real intelligence, 24 hours before a real meeting.

Rashad runs meetings across five businesses; without help he walks into them cold. This watches the
merged calendars, and when a meeting with someone outside the company is a day out it assembles what
Cortex already knows (the thread, the deal timeline, past meeting notes, what we quoted, our own
relevant films) plus live web research on the company, the people and the actual enquiry, and writes
a fixed one-page brief he can read in two minutes.

Owner-approved model exception (31 Aug 2026): this runs on Opus 5 with server-side web search — it
fires ONCE per meeting, a handful of times a week, and the whole point is that the insight is good.
At roughly $0.50 a brief the cost is not the constraint.

The two sections he asked for by name:
  * SCOPING QUESTIONS — the questions that de-risk the project, drawn from THIS enquiry, never
    generic discovery filler. This is the part that earns its keep.
  * WARM OPENERS — friendly, human, NOT corporate: where someone is from, their background, the
    local angle. Mixed personal and business, so the meeting starts like a conversation.

Safety invariants live here in code (never in the editable skill): what may be researched about a
person, dated claims only, and real links only. Tone, shape and emphasis live in the `meeting-prep`
skill craft, which Rashad edits.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Json

from . import calendar as gcal
from . import config, crm, db, notifications, provider

# The brief names its model OUTRIGHT. The 'opus' tier is not trustworthy here: /etc/cortex/cortex.env
# pins CORTEX_MODEL, so a skill marked 'opus' has silently been resolving to whatever that pin says.
# Rashad approved Opus 5 for this job specifically (31 Aug 2026), so this job asks for it by name.
BRIEF_MODEL = "claude-opus-5"

PREP_BLOCK_MINUTES = 15  # the private prep slot dropped in front of the meeting, carrying the link

LEAD_HOURS = 24          # brief lands a day before the meeting, so the research is still fresh
LOOKAHEAD_HOURS = 30     # how far ahead we read the calendar
_MIN_GAP_SECONDS = 600   # don't hammer the Calendar API from a 60-second loop

# Our own domains — a meeting with only these people is an internal block, not a client meeting.
OUR_DOMAINS = ("sensa.digital", "tabscanner.com", "skyvision.ae", "filmspoke.com",
               "snaprewards.ae", "coretex.uk")

# Personal-research boundary (safety invariant, not editable in the skill): the warm openers may use
# PUBLIC PROFESSIONAL information and general local/cultural colour only. Never private life.
_PERSON_SCOPE = (
    "PEOPLE RESEARCH, strict scope. You may use public professional sources only: LinkedIn-style "
    "career history, current and previous roles, how long they have been there, their professional "
    "background, where they are based, public talks, interviews or company announcements, and "
    "general colour about their city or country. You may NOT research or mention: family, "
    "relationships, children, health, religion, politics, personal finances, home address, personal "
    "social media, or anything from a private account. If you cannot find something real about a "
    "person, say so and offer a local or cultural opener instead. Never invent a biographical detail."
)

DEFAULT_CRAFT = """Write a pre-meeting brief Rashad can read in TWO MINUTES. Around 500 words. Never longer.

Use exactly these sections, in this order, with these headings:

THE MEETING
One line: when, where or how, and who is in the room. For each person: name, role, and how we know them.

THE COMPANY
Three lines maximum. What they actually do, their scale or ownership, and any genuinely recent news.
Every external claim carries its date. Anything older than 18 months is background, not news, and must
be labelled as such.

WHY WE ARE MEETING
The real ask, taken from the actual thread or the calendar invite. Not a guess.

WHERE WE STAND
What we have already sent them, what is outstanding, any money owed either way, and what we last
promised. If we owe them something, say it first.

WHAT THEIR ASK LIKELY MEANS
The intelligence layer. What is probably driving this request, what it implies about scope, budget or
timeline, and what to be ready for. Reason from evidence and label what is inference.

SCOPING QUESTIONS
Maximum six. Specific to THIS client and THIS enquiry, chosen to de-risk the project: volumes,
formats, integration, rights, approvers, timeline, budget band, what success looks like. Never generic
discovery questions. These are the questions that stop a project going wrong later.

WARM OPENERS
Four. Two personal and two business. The personal ones are friendly and human, not corporate: where
they are from, their career path, something about their city, a genuine point of connection. Written
as Rashad would actually say them out loud, not as bullet points of research. The business ones are
light touch: something their company has done that is worth a nod.

DO NOT RAISE
Anything sensitive: an unpaid invoice, a project that went badly, someone who has left, a topic that
stalled the last conversation. One line each. Say "nothing" if there is nothing.

OUR RELEVANT WORK
Include this section ONLY if a media library was served to you. Then give up to three of our films
that genuinely fit this client, with their real links exactly as served. Never invent a link. If no
library was served, leave the section out entirely rather than apologising for its absence.

SOURCES
The URLs you actually used, one per line.

Rules of the house: no em dashes or en dashes in the body copy, use commas or colons. No emoji.
British spelling. Plain sentences. If a fact is not in the context you were served and you did not
find it in a search, leave it out rather than smoothing over the gap.

FORMAT. Start with the first heading. No preamble, no "here is your brief", no sign-off, no
horizontal rules, no prepared-on or confidential line, no bold or markdown decoration. Headings on
their own line in plain capitals exactly as written above. The cockpit styles them. Under each
heading, short plain lines. 500 words for the whole brief. If you are running long, cut the
commentary, never the questions."""


# ---- the signed link to the standalone brief page (coretex.uk/brief/<id>?k=...) ----
#
# Lives here rather than in api.py so the ENGINE can build a link without importing the web app.
# Same secret the API session tokens use, so the key is unguessable and grants nothing but this brief.
PUBLIC_BASE = (config.get("CORTEX_PUBLIC_BASE") or "https://coretex.uk").rstrip("/")


def _secret() -> bytes:
    s = db.setting_get("api_secret")
    if not s:
        s = secrets.token_hex(32)
        db.setting_set("api_secret", s)
    return s.encode()


def brief_key(tid: int) -> str:
    sig = hmac.new(_secret(), f"brief:{int(tid)}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")[:32]


def brief_url(tid: int) -> str:
    return f"{PUBLIC_BASE}/brief/{int(tid)}?k={brief_key(tid)}"


def ensure_schema() -> None:
    db.execute("""
      create table if not exists meeting_briefs (
        id           bigserial primary key,
        event_id     text unique not null,
        company_id   bigint references companies(id),
        calendar_co  text,
        title        text,
        starts_at    timestamptz,
        attendees    jsonb not null default '[]'::jsonb,
        deal_id      bigint,
        task_id      bigint,
        created_at   timestamptz not null default now()
      )""")


def ensure_skills() -> None:
    """The uniform-roster rule: the skill exists for EVERY company, seeded with the default craft.
    Idempotent, and it never overwrites craft or rules Rashad has since edited."""
    for co in db.query("select id from companies where kind='owned' order by id"):
        db.execute(
            "insert into skills (company_id, skill_key, name, craft, category, department, manager, "
            "model, authority, stakes) values (%s,'meeting-prep',%s,%s,%s,%s,%s,'opus','ask','low') "
            "on conflict (company_id, skill_key) do nothing",
            (co["id"], "Pre-meeting brief", DEFAULT_CRAFT, "Convert", "Sales & Inquiries",
             "Sales manager"))


def _external(attendees: list[dict]) -> list[dict]:
    """The people who are not us. A meeting with none of these is an internal block."""
    out = []
    for a in attendees or []:
        em = (a.get("email") or "").lower()
        if not em or a.get("self"):
            continue
        if any(em.endswith("@" + d) or em.endswith("." + d) for d in OUR_DOMAINS):
            continue
        out.append(a)
    return out


def _company_for(slug: str) -> dict:
    return db.one("select * from companies where slug=%s", (slug,)) or {}


def _skill(company_id) -> dict:
    return db.one("select * from skills where company_id=%s and skill_key='meeting-prep'",
                  (company_id,)) or {}


def _context(company: dict, event: dict, guests: list[dict]) -> tuple[dict, int | None]:
    """Everything Cortex already knows about these people, assembled before any research happens.
    Fail-soft shelf by shelf: a missing shelf makes the brief thinner, never absent."""
    ctx: dict = {}
    cid, slug = company.get("id"), company.get("slug")
    emails = [g["email"] for g in guests]

    try:    # the real correspondence, so the brief references what was actually said
        from .engine import _deal_thread_context
        threads = []
        for em in emails[:3]:
            t = _deal_thread_context(company, em, limit=4)
            if t:
                threads.append(f"### correspondence with {em}\n{t}")
        if threads:
            ctx["thread_history"] = "\n\n".join(threads)
    except Exception:  # noqa: BLE001
        pass

    deal_id = None
    try:
        for em in emails:
            ds = crm.active_deals_for_email(em, slug)
            if ds:
                deal_id = ds[0]["id"]
                ctx["deals_on_file"] = "\n".join(
                    f"- #{d['id']} {d.get('title') or ''} | stage {d.get('stage') or ''} | "
                    f"{d.get('currency') or ''} {d.get('value') or ''}" for d in ds)
                break
        if deal_id:
            from . import pipeline
            tl = pipeline.deal_context(int(deal_id))
            if tl:
                ctx["deal_timeline"] = tl
    except Exception:  # noqa: BLE001
        pass

    try:    # what was said the LAST time we sat down with them
        from . import meetnotes
        notes = []
        for em in emails[:3]:
            mn = meetnotes.latest_for_contact(cid, em)
            if mn and mn.get("summary"):
                when = mn["starts_at"].strftime("%d %b %Y") if mn.get("starts_at") else ""
                notes.append(f"{mn.get('title') or 'Meeting'} ({when}):\n{mn['summary'][:1500]}")
        if notes:
            ctx["previous_meetings"] = "\n\n".join(notes)
    except Exception:  # noqa: BLE001
        pass

    try:    # who they are in the CRM, plus any notes the team saved on them
        rows = db.query(
            "select first_name, last_name, email, job_title, company_name, linkedin, location, "
            "history from crm_master where lower(email) = any(%s)", ([e.lower() for e in emails],))
        if rows:
            who = []
            for r in rows:
                nm = " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x).strip()
                line = f"- {nm or r['email']}"
                if r.get("job_title"):
                    line += f", {r['job_title']}"
                if r.get("company_name"):
                    line += f" at {r['company_name']}"
                if r.get("location"):
                    line += f" | based {r['location']}"
                if r.get("linkedin"):
                    line += f" | {r['linkedin']}"
                hist = r.get("history") if isinstance(r.get("history"), list) else []
                for h in [h for h in hist if h.get("event") == "note"][-3:]:
                    line += f"\n    note {h.get('ts', '')[:10]}: {h.get('text', '')[:250]}"
                who.append(line)
            ctx["crm_contacts"] = "\n".join(who)
    except Exception:  # noqa: BLE001
        pass

    try:    # REAL portfolio links — the only links the brief may offer as our work
        rows = db.query(
            "select distinct on (cat) cat, title, watch_url from ("
            "  select title, watch_url, rating, views, jsonb_array_elements_text(categories) cat"
            "  from media_assets where company_id=%s and coalesce(watch_url,'')<>'' and status='live'"
            "  and privacy in ('public','unlisted')) x "
            "where cat not in ('version-variant','internal-test') "
            "order by cat, rating desc nulls last, views desc nulls last", (cid,))
        if rows:
            ctx["media_library"] = "\n".join(
                f"- {r['title']} [{r['cat']}]: {r['watch_url']}" for r in rows)
    except Exception:  # noqa: BLE001
        pass

    return ctx, deal_id


def _system(company: dict, skill: dict) -> str:
    from . import profile
    craft = (skill.get("craft") or "").strip() or DEFAULT_CRAFT
    rules = skill.get("rules") or []
    bits = [
        f"You are writing a private pre-meeting brief for Rashad Alsafar, founder of "
        f"{company.get('name') or 'the company'}. He alone reads it, minutes before the meeting.",
        "", craft, "", _PERSON_SCOPE, "",
        "HARD RULES (these override anything above):",
        "- Never invent a fact. No invented dates, numbers, prices, headcounts, job titles or news.",
        "- Every external claim carries a date and is traceable to a source you list.",
        "- Only offer links that appear in the media library served to you, or pages you actually "
        "found in search. Never construct a URL.",
        "- If the internal context contradicts something you found online, trust the internal "
        "context and say the two disagree.",
        "- Say plainly when you could not find something. A short honest brief beats a padded one.",
    ]
    if rules:
        bits += ["", "Rules Rashad has taught this skill:"] + [
            f"- {r.get('rule') if isinstance(r, dict) else r}" for r in rules[:20]]
    try:
        p = profile.get(company.get("id"))
        if p:
            bits += ["", "Who we are:", json.dumps(p, default=str)[:2500]]
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(bits)


def _user(event: dict, guests: list[dict], ctx: dict) -> str:
    when = event["starts_at"].astimezone(gcal._GST)
    lines = [
        "THE MEETING AS BOOKED",
        f"Title: {event.get('title') or '(untitled)'}",
        f"When: {when.strftime('%A %d %B %Y at %H:%M')} Dubai time",
        f"Where: {event.get('location') or event.get('hangout') or 'not stated'}",
        "Who (outside our company):",
    ]
    for g in guests:
        lines.append(f"  - {g.get('name') or ''} <{g['email']}>"
                     + (" (optional)" if g.get("optional") else ""))
    if event.get("description"):
        lines += ["Invite notes:", event["description"][:2000]]
    for key, label in (("crm_contacts", "WHO THESE PEOPLE ARE (our CRM)"),
                       ("deals_on_file", "DEALS ON FILE"),
                       ("deal_timeline", "DEAL TIMELINE"),
                       ("previous_meetings", "WHAT WAS SAID LAST TIME"),
                       ("thread_history", "THE ACTUAL CORRESPONDENCE"),
                       ("media_library", "OUR MEDIA LIBRARY (the only links you may offer as our work)")):
        if ctx.get(key):
            lines += ["", f"=== {label} ===", str(ctx[key])[:6000]]
    lines += ["", "Research the company, the people and the subject of this meeting, then write the "
                  "brief exactly in the section order given. Keep it to a two-minute read."]
    return "\n".join(lines)


def brief_for(event: dict, *, dry_run: bool = False) -> dict:
    """Research and write the brief for one calendar event."""
    guests = _external(event.get("attendees") or [])
    company = _company_for(event.get("company") or "sensa")
    if not company:
        return {"skipped": "unknown company"}
    skill = _skill(company["id"])
    ctx, deal_id = _context(company, event, guests)
    tier = (skill.get("model") or "").strip()
    model = provider.resolve_model(tier) if tier in ("sonnet", "haiku") else BRIEF_MODEL
    text = provider.think_research(
        _system(company, skill), _user(event, guests, ctx),
        model=model, max_tokens=3000, max_searches=10,
        purpose="meeting-brief", company=company.get("slug"))
    text = re.sub(r"[–—]", ",", text)     # house rule: no em/en dashes in body copy
    if dry_run:
        return {"draft": text, "context_served": sorted(ctx.keys()), "model": model,
                "company": company.get("slug"), "deal_id": deal_id}

    when = event["starts_at"].astimezone(gcal._GST)
    who = ", ".join(g.get("name") or g["email"] for g in guests) or "external guests"
    title = f"Meeting brief: {event.get('title') or who} ({when.strftime('%a %d %b, %H:%M')})"
    # Inserted READY, not 'new': a 'new' card is picked up by the worker and re-drafted, which would
    # throw this research away. The brief is already written, so it lands straight in the Inbox.
    task = db.execute(
        "insert into tasks (company_id, skill_id, kind, request, draft, status, title, deal_id) "
        "values (%s,%s,'meeting_brief',%s,%s,'awaiting_approval',%s,%s) returning *",
        (company["id"], (skill or {}).get("id"), Json({
            "title": title, "brief": text, "meeting": {
                # read-only: this card DESCRIBES the meeting, it does not own it. `invited` is stamped
                # so nothing downstream can ever mistake it for a guest-less slot the card pre-booked.
                "invited": True, "readonly": True,
                "event_id": event.get("event_id"), "title": event.get("title"),
                "start": event["starts_at"].isoformat(), "location": event.get("location"),
                "hangout": event.get("hangout"), "attendees": guests},
            "deal_id": deal_id, "context_served": sorted(ctx.keys())}),
         text, title, deal_id))
    db.execute("insert into meeting_briefs (event_id, company_id, calendar_co, title, starts_at, "
               "attendees, deal_id, task_id) values (%s,%s,%s,%s,%s,%s,%s,%s) "
               "on conflict (event_id) do update set task_id=excluded.task_id",
               (event.get("event_id"), company["id"], event.get("company"), event.get("title"),
                event["starts_at"], json.dumps(guests), deal_id, task["id"]))
    # The brief has to be reachable without opening the cockpit, so it gets its own signed page.
    link = ""
    try:
        link = brief_url(task["id"])
    except Exception:  # noqa: BLE001 — the card still works without a link
        pass

    # A PRIVATE PREP BLOCK, never the shared invite. Writing the link into the meeting event itself
    # would publish our own intelligence to every attendee, the client included: an invite description
    # is visible to all guests. So the link goes on a 15-minute block that belongs to Rashad alone,
    # sitting just before the meeting, which also puts the prep time in his day.
    prep_id = ""
    if link:
        try:
            prep = gcal.create_event(
                event.get("company") or "sensa",
                calendar_id=event.get("calendar_id") or "primary",
                start=event["starts_at"] - timedelta(minutes=PREP_BLOCK_MINUTES),
                minutes=PREP_BLOCK_MINUTES,
                summary=f"Prep: {event.get('title') or who}",
                description="Cortex pre-meeting brief:" + chr(10) + link + chr(10) + chr(10)
                            + "Private. Do not forward.")
            prep_id = prep.get("id") or ""
        except Exception:  # noqa: BLE001
            pass

    if deal_id and link:
        try:
            from . import pipeline
            pipeline.log_deal(int(deal_id), "brief",
                              f"Pre-meeting brief written for {when.strftime('%a %d %b %H:%M')}: {link}")
        except Exception:  # noqa: BLE001
            pass

    if link or prep_id:
        db.execute("update tasks set request = request || %s::jsonb where id=%s",
                   (Json({"brief_url": link, "prep_event_id": prep_id}), task["id"]))

    notifications.notify(
        title, f"{who}, {when.strftime('%A %H:%M')}. Read it: {link}" if link
        else f"{who}, {when.strftime('%A %H:%M')}. Brief ready to read.",
        priority="normal", category="brief", company_id=company["id"],
        target_type="task", target_id=task["id"])
    return {"task_id": task["id"], "company": company.get("slug"), "deal_id": deal_id,
            "url": link, "prep_event": prep_id}


_last_run = [0.0]


def sweep(force: bool = False) -> dict:
    """Every readable meeting starting inside the lead window gets exactly one brief."""
    import time
    if not force and time.time() - _last_run[0] < _MIN_GAP_SECONDS:
        return {"skipped": "throttled"}
    _last_run[0] = time.time()
    ensure_schema()
    ensure_skills()
    cutoff = datetime.now(timezone.utc) + timedelta(hours=LEAD_HOURS)
    made, skipped = [], []
    for ev in gcal.upcoming_events(hours=LOOKAHEAD_HOURS):
        if ev["starts_at"] > cutoff:
            continue                                   # still more than a day out, leave it
        if not _external(ev.get("attendees") or []):
            skipped.append((ev.get("title"), "internal"))
            continue
        if db.one("select 1 from meeting_briefs where event_id=%s", (ev.get("event_id"),)):
            continue
        try:
            made.append(brief_for(ev).get("task_id"))
        except Exception as e:  # noqa: BLE001 — one bad meeting never stops the rest
            skipped.append((ev.get("title"), str(e)[:120]))
    return {"briefed": made, "skipped": skipped}
