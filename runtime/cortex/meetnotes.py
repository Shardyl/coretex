"""Gemini meeting notes -> the sales/project process.

Google Meet's Gemini notetaker saves notes as a Google Doc attached to the calendar event. This
module sweeps the booking calendar for finished meetings whose notes exist, reads each doc once
(the hello@ token has drive.readonly), distils it (summary, decisions, action items — the model
summarises, code stamps ids/dates), matches it to the CRM by the external attendees, and stores it
in `meeting_notes` — from where it feeds CRM history and the drafters' conversation memory, so
follow-ups reflect what was actually said on the call. Sensa + SkyVision share the pipeline.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import crm, db, provider, store

_MIGRATE = """
create table if not exists meeting_notes (
  id bigserial primary key,
  event_id text not null unique,
  file_id text not null,
  company_id bigint,
  deal_id bigint,
  title text,
  starts_at timestamptz,
  attendees jsonb not null default '[]'::jsonb,
  summary text not null default '',
  created_at timestamptz not null default now()
);
"""

from .identity import NON_CLIENT_DOMAINS as _OWN   # single definition (identity.py)


def ensure_schema() -> None:
    with db.connect() as c:
        c.execute(_MIGRATE)


def _tok() -> str:
    from . import calendar as gcal
    return gcal._token("sensa")


def _doc_text(file_id: str, tok: str) -> str:
    r = urllib.request.urlopen(urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(file_id)}/export?mimeType=text/plain",
        headers={"Authorization": f"Bearer {tok}"}, ), timeout=60)
    return r.read().decode("utf-8", "ignore")


def _distil(title: str, text: str) -> str:
    return provider.think(
        "You distil MEETING NOTES for a production company's CRM. From the notes, write a tight brief "
        "(under 250 words, plain prose + short dashes for action items): what the meeting was about, what "
        "was DECIDED, what each side COMMITTED to (who does what, by when — only deadlines actually stated), "
        "open questions, and the agreed next step. Facts from the notes only — never invent.",
        f"Meeting: {title}\n\nNOTES:\n{text[:24000]}", fast=True, max_tokens=800, purpose="meetnotes-distil")


def sweep(days_back: int = 7, min_gap_minutes: int = 60, backfill: bool = False) -> dict:
    """Hourly from the engine loop: process finished meetings with Gemini notes, once each.

    backfill=True reaches back over OLD meetings to recover the memory only. It still stores the notes
    and the deal-timeline history, but it never extracts commitments into live reminders and never
    drafts a post-meeting follow-up: an email thanking someone for a call held in May, or a reminder
    dated from a promise made four months ago, would be worse than the gap it is filling."""
    import time
    last = db.setting_get("meetnotes_sweep_ts") or 0
    if time.time() - float(last) < min_gap_minutes * 60:
        return {"skipped": True}
    db.setting_set("meetnotes_sweep_ts", time.time())
    ensure_schema()
    from . import calendar as gcal
    tok = _tok()
    cals = ["primary"]
    main = db.setting_get("calendar_id:sensa")
    if main:
        cals.append(main)
    t0 = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    t1 = datetime.now(timezone.utc).isoformat()   # only FINISHED meetings have meaningful notes
    made = 0
    for cal in cals:
        try:
            ev = json.load(urllib.request.urlopen(urllib.request.Request(
                f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal)}/events"
                f"?timeMin={urllib.parse.quote(t0)}&timeMax={urllib.parse.quote(t1)}&singleEvents=true"
                "&orderBy=startTime&maxResults=100",
                headers={"Authorization": f"Bearer {tok}"}), timeout=30))
        except Exception:  # noqa: BLE001
            continue
        for e in ev.get("items", []):
            note = next((a for a in (e.get("attachments") or [])
                         if "gemini" in (a.get("title") or "").lower() and a.get("fileId")), None)
            if not note:
                continue
            if db.one("select id from meeting_notes where event_id=%s", (e["id"],)):
                continue
            try:
                text = _doc_text(note["fileId"], tok)
                if len(text.strip()) < 200:
                    continue                      # notes doc exists but is still empty/stub
                summary = _distil(e.get("summary") or "Meeting", text)
            except Exception:  # noqa: BLE001 — skip this one, retry next sweep
                continue
            ext = [a.get("email", "").lower() for a in (e.get("attendees") or [])
                   if a.get("email") and a["email"].split("@")[-1].lower() not in _OWN]
            company_id = deal_id = None
            for em in ext:                        # first attendee on an active deal decides the home
                for slug in ("sensa", "skyvision", "tabscanner", "filmspoke", "snaprewards"):
                    ds = crm.active_deals_for_email(em, slug)
                    if ds:
                        co = store.get_company_by_slug(slug)
                        company_id, deal_id = (co or {}).get("id"), ds[0]["id"]
                        break
                if deal_id:
                    break
            starts = (e.get("start") or {}).get("dateTime")
            db.execute("insert into meeting_notes (event_id, file_id, company_id, deal_id, title, starts_at, "
                       "attendees, summary) values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (event_id) do nothing",
                       (e["id"], note["fileId"], company_id, deal_id, e.get("summary"), starts,
                        json.dumps(ext), summary))
            for em in ext:                        # meeting lands on each external attendee's CRM history
                try:
                    crm.log_event(em, "meeting_notes", f"Meeting: {e.get('summary')} — notes captured", None)
                except Exception:  # noqa: BLE001
                    pass
            if deal_id:                           # pipeline loop: meeting -> deal timeline + our commitments
                try:
                    from . import pipeline
                    pipeline.record_meeting(deal_id, company_id, e.get("summary") or "Meeting", summary,
                                            commitments=not backfill)
                except Exception:  # noqa: BLE001
                    pass
            if not backfill:                      # the meeting is the midpoint, not the end: draft the follow-up
                try:
                    _spawn_post_meeting_followup(e, ext, company_id, deal_id, summary)
                except Exception:  # noqa: BLE001
                    pass
            made += 1
    return {"captured": made}


def _spawn_post_meeting_followup(event: dict, ext: list, company_id, deal_id, summary: str) -> None:
    """Notes captured and the client hasn't written since the meeting -> draft the post-meeting follow-up
    as an approval card: thanks, a short recap of what was agreed, and the committed next step (for a sales
    meeting usually confirming the proposal/quotation is in preparation with the scope from the notes)."""
    contact = next((em for em in ext if db.one(
        "select id from crm_master where lower(email)=lower(%s)", (em,))), None)
    if not contact:
        return
    if company_id is None:                        # no deal matched: the contact's latest card names the company
        t = db.one("select company_id from tasks where kind='email_reply' and "
                   "lower(request->'inquiry'->>'email')=lower(%s) order by id desc limit 1", (contact,))
        company_id = (t or {}).get("company_id")
    if not company_id:
        return
    started = (event.get("start") or {}).get("dateTime")
    # they already emailed us after the meeting (a reply card exists), or a card is open -> the email flow
    # has it; a second proactive card would double up
    if db.one("select id from tasks where company_id=%s and kind='email_reply' and "
              "lower(request->'inquiry'->>'email')=lower(%s) and "
              "(status in ('new','drafting','awaiting_approval','awaiting_correction') "
              " or created_at > %s::timestamptz) limit 1", (company_id, contact, started)):
        return
    skill = store.get_skill_by_key(company_id, "sales-first-response")
    if not skill:
        return
    c = db.one("select first_name, last_name from crm_master where lower(email)=lower(%s)", (contact,))
    name = (((c or {}).get("first_name") or "") + " " + ((c or {}).get("last_name") or "")).strip()
    req = {"brief": (f"POST-MEETING FOLLOW-UP after '{event.get('summary')}'. We just met; they have not "
                     "emailed since. The POST-MEETING standing rules on the sales-followup skill govern "
                     "what this email does.\nMEETING NOTES (distilled):\n" + summary[:3000]),
           "inquiry": {"name": name or contact.split("@")[0], "email": contact,
                       "subject": f"Following up: {event.get('summary')}"},
           "followup": "post-meeting"}
    if deal_id:
        req["deal_id"] = deal_id
    store.create_card(company_id, skill["id"], "email_reply", req, contact=contact, deal_id=deal_id)


def latest_for_contact(company_id: int, email: str) -> dict | None:
    """The most recent distilled meeting involving this contact — drafting context."""
    ensure_schema()
    return db.one("select title, starts_at, summary from meeting_notes where attendees ? %s "
                  "and (company_id is null or company_id=%s) order by starts_at desc nulls last limit 1",
                  ((email or "").lower(), company_id))


# ---------- THE EMAIL PATH: read the notes where Gemini actually delivers them ----------
#
# The calendar was the wrong source. It carries the notes only as an attachment on the event, so the
# notes die with the event: the ECBD board-meeting call on 1 Sep 2026 ran fine, Gemini wrote it up,
# and Cortex could never have seen it because the event had been cancelled hours earlier.
#
# Gemini also EMAILS the notes to every participant, from gemini-notes@google.com, with the whole
# write-up in the body. That mail lands in three of our mailboxes and cannot be deleted out from under
# us, so it is now the primary source. The calendar sweep stays as the secondary.

_NOTES_SENDER = "gemini-notes@google.com"
_NOTES_SUBJ_TIME = re.compile(
    r"Notes:\s*(?:Meeting\s+)?(?P<rest>.+?)\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s*(?P<yr>\d{4})"
    r"\s+at\s+(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>AM|PM)", re.I)
# Gemini also sends DATE-ONLY subjects: 'Notes: "Call with Mai" Sep 1, 2026'. Before this was handled the
# time regex missed, which (a) dropped the dedup key back to the per-mailbox message id, so the same call
# was stored once per mailbox, and (b) skipped the attendee lookup, so the notes never attached to a deal
# and drafting could not see them (MAH Gold, 1 Sep 2026).
_NOTES_SUBJ_DAY = re.compile(
    r"Notes:\s*(?:Meeting\s+)?(?P<rest>.+?)\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s*(?P<yr>\d{4})",
    re.I)


def _subject_when(subject: str):
    """(start, has_time). A precise start when the subject carries one; else that DAY at midnight GST with
    has_time False, so callers can widen the window instead of giving up entirely."""
    dt = _subject_start(subject)
    if dt:
        return dt, True
    m = _NOTES_SUBJ_DAY.search(subject or "")
    if not m:
        return None, False
    try:
        return datetime.strptime(f"{m.group('day')} {m.group('mon')} {m.group('yr')}",
                                 "%d %b %Y").replace(tzinfo=_GST_TZ()), False
    except Exception:  # noqa: BLE001
        return None, False


def _notes_key(subject: str, start, has_time: bool, fallback: str) -> str:
    """ONE row per meeting. The same notes email lands in every monitored mailbox, so the key must come
    from the MEETING, never the message id - keying on the message id is what produced triplicates."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", (subject or "").lower()).strip("-")[:60]
    if start and has_time:
        return "gemini-notes:" + start.isoformat()
    if start:
        return "gemini-notes:" + start.date().isoformat() + ":" + slug
    return "gemini-notes:subj:" + (slug or fallback)


def _subject_start(subject: str) -> datetime | None:
    """The meeting's start time, out of Gemini's subject line. Code parses it; nothing guesses a date."""
    m = _NOTES_SUBJ_TIME.search(subject or "")
    if not m:
        return None
    try:
        hh = int(m.group("h")) % 12 + (12 if m.group("ap").upper() == "PM" else 0)
        return datetime.strptime(
            f"{m.group('day')} {m.group('mon')} {m.group('yr')} {hh:02d}:{m.group('m')}",
            "%d %b %Y %H:%M").replace(tzinfo=_GST_TZ())
    except Exception:  # noqa: BLE001
        return None


def _GST_TZ():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Asia/Dubai")


def _attendees_at(start: datetime, whole_day: bool = False) -> tuple[list[str], str]:
    """Who was in that meeting, from the calendars — INCLUDING cancelled events, which is the whole
    point: the event may have been deleted and the meeting still happened. `whole_day` widens the window
    to that calendar day, for notes whose subject carried a date but no time. Returns (emails, title)."""
    from . import calendar as gcal
    if whole_day:
        lo = start.replace(hour=0, minute=0, second=0, microsecond=0)
        hi = lo + timedelta(days=1)
    else:
        lo, hi = start - timedelta(minutes=45), start + timedelta(minutes=45)
    for entry in (db.setting_get("availability_calendars") or []):
        co, cal = entry.get("company") or "sensa", entry.get("id") or "primary"
        try:
            tok = gcal._token(co)
            q = urllib.parse.urlencode({"timeMin": lo.isoformat(), "timeMax": hi.isoformat(),
                                        "singleEvents": "true", "showDeleted": "true", "maxResults": "25"})
            items = json.load(urllib.request.urlopen(urllib.request.Request(
                f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal)}/events?{q}",
                headers={"Authorization": f"Bearer {tok}"}), timeout=30)).get("items") or []
        except Exception:  # noqa: BLE001
            continue
        for ev in items:
            ext = [(a.get("email") or "").lower() for a in (ev.get("attendees") or [])
                   if a.get("email") and (a["email"].split("@")[-1].lower() not in _OWN)]
            if ext:
                return ext, (ev.get("summary") or "")
    return [], ""


def sweep_email(days_back: int = 2, min_gap_minutes: int = 10, backfill: bool = False) -> dict:
    """Gemini's own notes emails -> meeting_notes. Primary path; survives a deleted calendar event."""
    import time
    last = db.setting_get("meetnotes_email_ts") or 0
    if min_gap_minutes and time.time() - float(last) < min_gap_minutes * 60:
        return {"skipped": True}
    db.setting_set("meetnotes_email_ts", time.time())
    ensure_schema()
    from . import gmail
    made, seen_keys = [], set()
    for entry in (db.setting_get("inbox_registry") or []):
        rt = entry.get("rt_key")
        if not rt or not db.setting_get(rt):
            continue
        try:
            tok = gmail._token_for(rt, "gmail", entry.get("slug"))
            res = gmail._get(tok, "messages", {"q": f"newer_than:{days_back}d from:{_NOTES_SENDER}",
                                               "maxResults": 20})
        except Exception:  # noqa: BLE001
            continue
        for ref in (res.get("messages") or []):
            try:
                full = gmail._get(tok, "messages/" + ref["id"], {"format": "full"})
                hdr = {h["name"]: h["value"] for h in (full.get("payload") or {}).get("headers", [])}
                subject = hdr.get("Subject", "")
                body = gmail._plain_body(full.get("payload") or {})
                if len(body.strip()) < 200:
                    continue
                start, has_time = _subject_when(subject)
                key = _notes_key(subject, start, has_time, ref["id"])
                if key in seen_keys or db.one("select 1 from meeting_notes where event_id=%s", (key,)):
                    seen_keys.add(key)
                    continue
                emails, ev_title = _attendees_at(start, whole_day=not has_time) if start else ([], "")
                title = ev_title or subject.replace("Notes:", "").strip()
                summary = _distil(title, body)
                company_id = deal_id = None
                for em in emails:
                    for slug in ("sensa", "skyvision", "tabscanner", "filmspoke", "snaprewards"):
                        ds = crm.active_deals_for_email(em, slug)
                        if ds:
                            co = store.get_company_by_slug(slug)
                            company_id, deal_id = (co or {}).get("id"), ds[0]["id"]
                            break
                    if deal_id:
                        break
                db.execute(
                    "insert into meeting_notes (event_id, file_id, company_id, deal_id, title, starts_at,"
                    " attendees, summary) values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (event_id) do nothing",
                    (key, ref["id"], company_id, deal_id, title, start, json.dumps(emails), summary))
                for em in emails:
                    try:
                        crm.log_event(em, "meeting_notes", f"Meeting: {title} — notes captured", None)
                    except Exception:  # noqa: BLE001
                        pass
                if deal_id:
                    from . import pipeline
                    pipeline.record_meeting(deal_id, company_id, title, summary, commitments=not backfill)
                seen_keys.add(key)
                made.append({"key": key, "deal": deal_id, "title": title[:60]})
            except Exception as e:  # noqa: BLE001 — one bad message never stops the rest
                print(f"[meetnotes-email] {type(e).__name__}: {e}", flush=True)
                continue
    return {"captured": made}
