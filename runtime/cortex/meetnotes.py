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

_OWN = ("sensa.digital", "skyvision.film", "tabscanner.com", "snap-rewards.com", "filmspoke.ai",
        "coretex.uk", "sensa.film", "sensafilms.com", "gmail.com")   # gmail: rashad's personal attends too


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


def sweep(days_back: int = 7, min_gap_minutes: int = 60) -> dict:
    """Hourly from the engine loop: process finished meetings with Gemini notes, once each."""
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
                    pipeline.record_meeting(deal_id, company_id, e.get("summary") or "Meeting", summary)
                except Exception:  # noqa: BLE001
                    pass
            try:                                  # the meeting is the midpoint, not the end: draft the follow-up
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
                     "emailed since. Draft the follow-up: thank them for the meeting, recap in one or two "
                     "lines what was agreed (from the notes below), and commit to the agreed next step — "
                     "when the notes point to a proposal/quotation, confirm it is being prepared with the "
                     "scope discussed (never invent prices or dates). Warm, brief, no sales pressure.\n"
                     "MEETING NOTES (distilled):\n" + summary[:3000]),
           "inquiry": {"name": name or contact.split("@")[0], "email": contact,
                       "subject": f"Following up: {event.get('summary')}"},
           "followup": "post-meeting"}
    if deal_id:
        req["deal_id"] = deal_id
    store.create_task(company_id, skill["id"], "email_reply", req)


def latest_for_contact(company_id: int, email: str) -> dict | None:
    """The most recent distilled meeting involving this contact — drafting context."""
    ensure_schema()
    return db.one("select title, starts_at, summary from meeting_notes where attendees ? %s "
                  "and (company_id is null or company_id=%s) order by starts_at desc nulls last limit 1",
                  ((email or "").lower(), company_id))
