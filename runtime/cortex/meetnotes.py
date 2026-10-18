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
alter table meeting_notes add column if not exists full_text text;   -- Gemini's whole write-up (17 Sep 2026)
alter table meeting_notes add column if not exists summary_doc_id bigint;  -- the client-sendable summary PDF (18 Oct 2026)
"""


def _keep_full(key: str, title: str, text: str, company_id, deal_id, when=None) -> None:
    """THE FULL NOTES ARE KEPT, NOT ONLY THE BRIEF (owner, 17 Sep 2026). The brief (summary) still drives
    email drafts and the commitment reminders; the whole write-up is stored on the row and, when the meeting
    belongs to a deal, filed on that deal as a document so Talk (read_document), the proposal writer and the
    quotation prep read all of it. Storage costs nothing; reading it is a per-use choice."""
    try:
        db.execute("update meeting_notes set full_text=%s where event_id=%s", (text, key))
    except Exception:  # noqa: BLE001
        pass
    if not (deal_id and company_id and text and len(text.strip()) >= 200):
        return
    try:
        from . import engine
        co = store.get_company(company_id)
        day = (when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when or "")[:10]) or "undated"
        safe = re.sub(r"[^A-Za-z0-9 -]", "", title or "Meeting")[:70].strip() or "Meeting"
        engine._file_deal_document(co, int(deal_id), f"Meeting notes - {safe} - {day}.txt", "text/plain",
                                   text.encode("utf-8"), kind="meeting-notes", source="Gemini meeting notes",
                                   ref=f"meetnotes:{key}")
    except Exception as e:  # noqa: BLE001
        print(f"[meetnotes] full notes not filed: {type(e).__name__}: {e}", flush=True)

from .identity import is_internal as _is_us   # single definition of "us" (identity.py)
# A CLIENT ON GMAIL IS STILL A CLIENT (13 Oct 2026): attendees were filtered by NON_CLIENT_DOMAINS, which counts
# gmail.com as ours, so Luna Mansour and iTellu's Gmail addresses vanished from the Learning App meeting. is_internal
# treats a free-mail address as ours only when it IS one of our own mailboxes.

_DOC_RX = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]{20,})")


def _words(t: str) -> set:
    return set(re.findall(r"[a-z]{4,}", (t or "").lower()))


def _family(key: str, title: str, when, deal_id) -> list:
    """Every stored notes row for the SAME meeting: same key (and its resumed sessions), or the same deal, the
    same day and the same title (the email path and the calendar path file one meeting under different keys)."""
    rows = db.query("select * from meeting_notes where event_id=%s or event_id like %s", (key, key + "#%"))
    if deal_id and when:
        day = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)[:10]
        for r in db.query("select * from meeting_notes where deal_id=%s and starts_at::date=%s::date", (int(deal_id), day)):
            if _norm_title(r.get("title") or "") == _norm_title(title or "") and r["id"] not in {x["id"] for x in rows}:
                rows.append(r)
    return rows


def _already_have(fam: list, doc_id: str, text: str) -> bool:
    """The same notes arriving again (another mailbox's copy, or the email and the calendar doc of one session):
    the Google Doc id matches, or, without one, most of its words are already in a stored write-up."""
    if doc_id and any(doc_id == (r.get("file_id") or "") for r in fam):
        return True
    w = _words(text)
    for r in fam:
        o = _words((r.get("full_text") or "") + " " + (r.get("summary") or ""))
        if w and o and len(w & o) / max(1, min(len(w), len(o))) > 0.6:
            return True
    return False


# THE FULL NOTES, NOT THE EMAIL (owner, 13 Oct 2026, card 1212). Gemini's email carries a short summary; the Google
# Doc it links to carries the summary, the details AND the transcript. The email path drafted the Ramadan follow-up
# from the email two seconds after it landed, so "Thursday 11:30 at Galleria Mall, Farheen sends the pin" (in the doc,
# not the email) never reached it, and the calendar path then stored the doc as a SECOND row nobody read. Every
# meeting since September was stored twice that way. Now the email path reads the linked doc itself and stores it
# under the doc id (so the calendar path recognises it), waits while the doc is not readable yet, and only falls back
# to the email text after FULL_NOTES_WAIT_MIN.
FULL_NOTES_WAIT_MIN = 30


def _is_doc_id(file_id: str) -> bool:
    """A Google Doc id (44 characters), as opposed to the Gmail message id a fallback row is stored under."""
    return len(file_id or "") >= 25


def _doc_in_email(payload: dict, body: str) -> str:
    """The notes document the email links to: the plain text rarely carries the URL, the HTML button does."""
    m = _DOC_RX.search(body or "")
    if m:
        return m.group(1)
    from . import gmail
    for href, _label in gmail._links(payload or {}, body or ""):
        m = _DOC_RX.search(href or "")
        if m:
            return m.group(1)
    return ""


def _full_notes(doc_id: str) -> str:
    """The whole notes document, or '' while it cannot be read (not shared yet, still being written)."""
    if not doc_id:
        return ""
    try:
        t = _doc_text(doc_id, _tok())
        return t if len(t.strip()) >= 200 else ""
    except Exception:  # noqa: BLE001
        return ""


def _age_minutes(date_header: str) -> float:
    """Minutes since the email's Date header; very large when it cannot be read (so nothing waits forever)."""
    try:
        from email.utils import parsedate_to_datetime
        return (datetime.now(timezone.utc) - parsedate_to_datetime(date_header)).total_seconds() / 60
    except Exception:  # noqa: BLE001
        return 1e9


def _dubai_day(when) -> str:
    try:
        d = when if hasattr(when, "astimezone") else datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(_GST_TZ()).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _short_copy_of(title: str, when) -> dict | None:
    """A row for the SAME meeting (same title, same Dubai day) stored from the email alone, with no document behind
    it: the fuller notes now arriving replace it rather than becoming a second record."""
    day = _dubai_day(when)
    if not day or not title:
        return None
    for r in db.query("select * from meeting_notes where (starts_at at time zone 'Asia/Dubai')::date = %s::date "
                      "and event_id not like %s order by id", (day, "%#s%")):
        if _norm_title(r.get("title") or "") == _norm_title(title) and not _is_doc_id(r.get("file_id") or ""):
            return r
    return None


def _upgrade_notes(row: dict, file_id: str, text: str, backfill: bool = False) -> bool:
    """Replace a short (email-only) record with the full notes: re-distil, keep the full text on the row and the deal,
    take on the doc id, and rewrite the follow-up still waiting for approval. False when the new text adds nothing."""
    old = (row.get("full_text") or row.get("summary") or "")
    if len(text or "") <= len(old) + 500:
        db.execute("update meeting_notes set file_id=%s where id=%s", (file_id, row["id"]))   # same notes: one record
        return False
    title = row.get("title") or "Meeting"
    summary = _distil(title, text)
    db.execute("update meeting_notes set file_id=%s, summary=%s where id=%s", (file_id, summary, row["id"]))
    _keep_full(row["event_id"], title, text, row.get("company_id"), row.get("deal_id"), row.get("starts_at"))
    if row.get("deal_id") and not backfill:
        from . import pipeline
        pipeline.record_meeting(row["deal_id"], row.get("company_id"), title + " (full notes)", summary,
                                commitments=True)
    if not backfill:
        _refresh_followup(row, summary, "The full meeting notes have arrived (the Gemini document with its transcript); "
                                        "the draft was written from the short email summary.")
    print(f"[meetnotes] row {row['id']} '{title}': short notes replaced by the full document", flush=True)
    return True


def _refresh_followup(row: dict, summary: str, why: str) -> int:
    """The post-meeting email still waiting for approval is rewritten from these notes (brief updated, system
    redraft, the owner's corrections kept). Returns how many cards were refreshed."""
    from . import engine
    emails = [e.lower() for e in (row.get("attendees") or []) if e]
    cards = db.query(
        "select id, request from tasks where kind='email_reply' and request->>'followup'='post-meeting' and status in "
        "('new','drafting','awaiting_approval','awaiting_correction') and created_at >= %s::timestamptz - interval '1 day' "
        "and ((%s::bigint is not null and deal_id=%s) or lower(request->'inquiry'->>'email') = any(%s))",
        (row.get("starts_at") or datetime.now(timezone.utc), row.get("deal_id"), row.get("deal_id"), emails or [""]))
    n = 0
    for c in cards:
        req = dict(c.get("request") or {})
        brief = req.get("brief") or ""
        marker = "MEETING NOTES (distilled):\n"
        req["brief"] = (brief.split(marker)[0] if marker in brief else brief.rstrip() + "\n") + marker + summary[:3000]
        if row.get("id"):   # fuller notes: the attached summary is rebuilt from them too
            _attach_summary_to_request(req, deal_id=row.get("deal_id"), meeting_id=row["id"], rebuild=True)
        store.update_task(c["id"], request=req)
        if engine.refresh_from_notes(c["id"], why):
            n += 1
    return n


def _resumed_session(fam: list, key: str, file_id: str, title: str, when, text: str, emails: list,
                     company_id, deal_id, backfill: bool) -> str:
    """A MEETING THAT RESUMED (owner, 28 Sep 2026: the Shama call broke off because Sarah was not there, and
    everyone rejoined the same link later). The later session's notes are filed as their own row, the brief is
    distilled from BOTH sessions (the later one decides where they differ), the deal gets the outcome and the
    commitments, and the post-meeting email waiting from the first session is redrafted from the combined notes
    (or drafted now if there is none). Returns the new row's key."""
    n = sum(1 for r in fam if (r.get("event_id") or "").startswith(key)) + 1
    k2 = f"{key}#s{n}"
    earlier = "\n\n".join((r.get("full_text") or r.get("summary") or "") for r in sorted(fam, key=lambda r: r["id"]))
    combined = _distil(title, "SESSION 1 (the meeting as first held):\n" + earlier[:12000]
                       + "\n\nLATER SESSION (the SAME meeting, resumed on the same link; where the sessions differ, "
                         "THIS is what was finally decided):\n" + text)
    db.execute("insert into meeting_notes (event_id, file_id, company_id, deal_id, title, starts_at, attendees, summary) "
               "values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (event_id) do nothing",
               (k2, file_id, company_id, deal_id, (title or "Meeting") + " (resumed)", when, json.dumps(emails or []),
                combined))
    _keep_full(k2, (title or "Meeting") + " (resumed)", text, company_id, deal_id, when)
    if deal_id:
        from . import pipeline
        pipeline.record_meeting(deal_id, company_id, (title or "Meeting") + " (resumed session)", combined,
                                commitments=not backfill)
        pipeline.log_deal(int(deal_id), "context", "The meeting resumed on the same link; the later session's "
                                                   "outcome replaces the first one where they differ (Gemini notes).")
    if not backfill:
        try:
            # a SYSTEM redraft, never one filed as the owner's correction (it used to run the owner-correction path,
            # which logged it as his and inferred a rule from it)
            if not _refresh_followup({"starts_at": when, "deal_id": deal_id, "attendees": emails or []}, combined,
                                     "The meeting RESUMED later on the same link and the later session changes the "
                                     "outcome; where the sessions differ, the later one decides."):
                _spawn_post_meeting_followup({"summary": title, "start": {"dateTime": str(when)}}, emails or [],
                                             company_id, deal_id, combined)
        except Exception as e:  # noqa: BLE001
            print(f"[meetnotes] resumed follow-up: {type(e).__name__}: {e}", flush=True)
    return k2


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
        f"Meeting: {title}\n\nNOTES:\n{text[:40000]}", fast=True, max_tokens=800, purpose="meetnotes-distil")


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
            notes = [a for a in (e.get("attachments") or [])
                     if "gemini" in (a.get("title") or "").lower() and a.get("fileId")]
            if not notes:
                continue
            note = None
            for cand in notes:                    # one row per notes doc: a rejoined meeting gets a second doc
                if db.one("select id from meeting_notes where file_id=%s", (cand["fileId"],)):
                    continue
                note = cand
                break
            if not note:
                continue
            try:
                text = _doc_text(note["fileId"], tok)
                if len(text.strip()) < 200:
                    continue                      # notes doc exists but is still empty/stub
            except Exception:  # noqa: BLE001 — skip this one, retry next sweep
                continue
            if db.one("select id from meeting_notes where event_id=%s", (e["id"],)):
                _ext0 = [a.get("email", "").lower() for a in (e.get("attendees") or [])
                         if a.get("email") and not _is_us(a["email"])]
                _fam = _family(e["id"], e.get("summary") or "", (e.get("start") or {}).get("dateTime"), None)
                _first = _fam[0] if _fam else {}
                _fam = _family(e["id"], e.get("summary") or "", _first.get("starts_at"), _first.get("deal_id"))
                if not _already_have(_fam, note["fileId"], text):
                    _resumed_session(_fam, e["id"], note["fileId"], e.get("summary") or "Meeting",
                                     (e.get("start") or {}).get("dateTime"), text, _ext0, _first.get("company_id"),
                                     _first.get("deal_id"), backfill)
                    made += 1
                continue
            _short = _short_copy_of(e.get("summary") or "", (e.get("start") or {}).get("dateTime"))
            if _short:                            # the email path stored it without the document: upgrade, never a twin
                if _upgrade_notes(_short, note["fileId"], text, backfill):
                    made += 1
                continue
            try:
                summary = _distil(e.get("summary") or "Meeting", text)
            except Exception:  # noqa: BLE001 — skip this one, retry next sweep
                continue
            ext = [a.get("email", "").lower() for a in (e.get("attendees") or [])
                   if a.get("email") and not _is_us(a["email"])]
            company_id = deal_id = None
            for em in ext:                        # first attendee on an active deal decides the home
                for slug in ("sensa", "skyvision", "tabscanner", "filmspoke", "snaprewards"):
                    ds = crm.active_deals_for_email(em, slug)
                    if ds:
                        co = store.get_company_by_slug(slug)
                        _b = _bound_deal(e.get("id") or "", e.get("summary") or "", (e.get("start") or {}).get("dateTime") or "", ds)
                        company_id, deal_id = (co or {}).get("id"), (_b or {}).get("id")
                        break
                if deal_id:
                    break
            if not deal_id:                       # the owner linked this meeting to a deal its people are not on yet
                _lc, _ld = _linked_deal(e.get("id") or "", e.get("summary") or "",
                                        (e.get("start") or {}).get("dateTime") or "")
                if _ld:
                    company_id, deal_id = _lc or company_id, _ld
            starts = (e.get("start") or {}).get("dateTime")
            db.execute("insert into meeting_notes (event_id, file_id, company_id, deal_id, title, starts_at, "
                       "attendees, summary) values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (event_id) do nothing",
                       (e["id"], note["fileId"], company_id, deal_id, e.get("summary"), starts,
                        json.dumps(ext), summary))
            _keep_full(e["id"], e.get("summary") or "Meeting", text, company_id, deal_id, starts)
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


def link_meeting(deal_id: int, event_id: str, title: str, starts_at) -> None:
    """THE MEETING IS FOR THIS DEAL (owner, 13 Oct 2026: a Ramadan film call with Farheen, who also has the Qabiala
    launch deal open). Bound by the calendar event id (calendar path) and by title + day (email path)."""
    from . import pipeline
    if event_id:
        db.setting_set(f"meeting_deal:{event_id}", int(deal_id))
    day = _dubai_day(starts_at) or str(starts_at)[:10]       # the meeting's own (Dubai) day, whatever the format
    if title and day:
        db.setting_set(f"meeting_deal_title:{title.strip().lower()}|{day}", int(deal_id))
    pipeline.log_deal(int(deal_id), "note", f"Meeting linked: '{title}' on {str(starts_at)[:16]}; its notes file here.")


def _bound_deal(event_id: str, title: str, starts_at, candidates: list[dict]) -> dict | None:
    """The deal a meeting belongs to: the owner's link first; else, among several open deals, the one whose title
    shares a word with the meeting title; None when that cannot be told (never just the first)."""
    # the day is the meeting's Dubai day: a date-only notes subject arrives as 20:00 UTC the evening BEFORE
    days = [d for d in dict.fromkeys((_dubai_day(starts_at), str(starts_at)[:10])) if d]
    for k in ([f"meeting_deal:{event_id}"] if event_id else []) + \
             [f"meeting_deal_title:{(title or '').strip().lower()}|{d}" for d in days if title]:
        b = db.setting_get(k)
        if b:
            return {"id": int(b)}
    if len(candidates) == 1:
        return candidates[0]
    words = {w for w in re.findall(r"[a-z]{4,}", (title or "").lower())} - {"meeting", "call", "with", "catch"}
    hits = [d for d in candidates if words & set(re.findall(r"[a-z]{4,}", (d.get("title") or "").lower()))]
    return hits[0] if len(hits) == 1 else None


def _linked_deal(event_id: str, title: str, starts_at) -> tuple:
    """(company_id, deal_id) for a meeting the owner LINKED to a deal, when no attendee is on a deal yet (the
    Learning App call: iTellu's people were not on deal 156 when the notes arrived). (None, None) otherwise."""
    b = _bound_deal(event_id, title, starts_at, [])
    if not b:
        return None, None
    p = db.one("select company from crm_projects where id=%s", (b["id"],)) or {}
    for slug in ("sensa", "skyvision", "tabscanner", "filmspoke", "snaprewards"):
        if (p.get("company") or "").lower() in (slug, crm._org(slug).lower()):
            return (store.get_company_by_slug(slug) or {}).get("id"), b["id"]
    return None, b["id"]


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
    # a post-meeting follow-up is a conversation in progress: sales-followup (audit step 2e; first response is
    # only ever the very first reply to a new enquiry)
    skill = store.get_skill_by_key(company_id, "sales-followup") or store.get_skill_by_key(company_id, "sales-first-response")
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
    _attach_summary_to_request(req, deal_id=deal_id, contact=contact)
    store.create_card(company_id, skill["id"], "email_reply", req, contact=contact, deal_id=deal_id)


# ---------- THE MEETING SUMMARY A CLIENT CAN BE SENT (owner, 18 Oct 2026) ----------
#
# Clients ask for the meeting notes. Card 1301 (Heaven) retold the meeting inside the email instead. Gemini's own
# doc has three parts: Quick notes (the summary and next steps), Full notes (Details, with internal asides such as
# the AI models we use and per-video maths) and the Transcript. Owner: send the SUMMARY ONLY. It is cut from
# Gemini's text by CODE (no model retells it, so nothing can be misquoted), laid out as a Sensa PDF, filed on the
# deal and attached to the post-meeting email. The whole record (`_keep_full`, kind meeting-notes, transcript
# included) stays internal: `documents.PRIVATE_KINDS` keeps it off every card.

_QUICK_TAB = re.compile(r"^\W*quick notes\s*$", re.I)
_SUMMARY_HEAD = re.compile(r"^summary\s*$", re.I)
_SUMMARY_STOP = re.compile(
    r"^\W*(?:(?:full notes|transcript|details)\s*$|meeting records\b|want to see more|you should review gemini"
    r"|is the content of this email helpful|how is the quality)", re.I)
_SUMMARY_JUNK = re.compile(
    r"(take a short survey|^tip:|^please rate|^these notes have been sent|^open meeting notes"
    r"|^the content was auto-generated|^may contain errors|^invited\b|^attachments\b)", re.I)
_DATE_LINE = re.compile(r"^[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}$")
_TIMESTAMP = re.compile(r"\s*\(\d{1,2}:\d{2}:\d{2}\)")


def summary_lines(text: str, title: str = "") -> list:
    """Gemini's summary and next steps, verbatim and in order: the Quick notes tab (or, in an email copy, the
    Quick Notes section), else the Full notes' Summary and Next steps. Never Details, never the transcript."""
    t = (text or "").replace("﻿", "").replace("\r", "")
    t = re.sub(r"[ \t]{2}\n(?=\S)", " ", t)          # an email copy wraps long lines with a two-space break
    lines = [ln.strip() for ln in t.split("\n")]
    start = next((i for i, ln in enumerate(lines) if _QUICK_TAB.match(ln)), None)
    if start is None:
        start = next((i for i, ln in enumerate(lines) if _SUMMARY_HEAD.match(ln)), None)
    if start is None:
        return []
    out = []
    for ln in lines[start + 1:]:
        if _SUMMARY_STOP.match(ln):
            break
        if not ln or not ln.strip("_-= ") or _SUMMARY_JUNK.search(ln) or _DATE_LINE.match(ln):
            continue
        if title and ln.lower() == title.strip().lower():
            continue
        if re.fullmatch(r"(?:[\w.+-]+@[\w.-]+\s*)+", ln):     # the attendee address line: we print names instead
            continue
        out.append(_TIMESTAMP.sub("", ln))
    return out


def _summary_html(company: dict, title: str, day: str, people: list, lines: list) -> str:
    import html as _h
    e = lambda s: _h.escape(str(s or ""))   # noqa: E731
    data = (db.one("select data from company_profiles where company_id=%s", (company["id"],)) or {}).get("data") or {}
    logo = ((data.get("brand") or {}).get("logo_dark_b64") or "")
    if logo and not logo.startswith("data:"):
        logo = "data:image/png;base64," + logo
    body, in_list = [], False
    for i, ln in enumerate(lines):
        bullet = ln[:2] in ("* ", "• ", "- ") or ln.startswith("[")
        if bullet:
            item = ln.lstrip("*•- ").strip()
            m = re.match(r"^\[([^\]]+)\]\s*([^:]{2,60}:)?\s*(.*)$", item)
            li = (f"<b>{e(m.group(1))}</b>: " + (f"<b>{e(m.group(2))}</b> " if m.group(2) else "") + e(m.group(3))
                  if m else e(item))
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{li}</li>")
            continue
        if in_list:
            body.append("</ul>")
            in_list = False
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if len(ln) <= 70 and not ln.endswith((".", ":", "?", "!")) and nxt:
            body.append(f"<h2>{e(ln)}</h2>")
        else:
            body.append(f"<p>{e(ln)}</p>")
    if in_list:
        body.append("</ul>")
    who = ", ".join(e(p) for p in people if p)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 22mm 20mm 20mm; @bottom-right {{ content: counter(page) " / " counter(pages);
  font: 9px Inter, sans-serif; color: #777; }} }}
body {{ font: 11px/1.55 Inter, sans-serif; color: #111; }}
.logo {{ background: #0A0A0A; padding: 12px 18px; display: inline-block; }}
.logo img {{ height: 28px; display: block; }}
.logo span {{ color: #fff; font-weight: 700; }}
h1 {{ font: 800 24px/1.2 Poppins, Inter, sans-serif; margin: 26px 0 4px; }}
.meta {{ color: #555; margin: 0 0 18px; }}
h2 {{ font: 700 13px/1.3 Poppins, Inter, sans-serif; margin: 18px 0 6px; }}
ul {{ margin: 4px 0 8px 16px; padding: 0; }} li {{ margin: 0 0 5px; }}
.foot {{ margin-top: 26px; padding-top: 8px; border-top: 1px solid #ddd; color: #777; font-size: 9px; }}
</style></head><body>
<div class="logo">{f'<img src="{logo}">' if logo else f'<span>{e(company.get("name"))}</span>'}</div>
<h1>Meeting notes</h1>
<div class="meta">{e(title)}<br>{e(day)}{f'<br>With {who}' if who else ''}</div>
{''.join(body)}
<div class="foot">Summary of the meeting notes taken with Google Gemini.</div>
</body></html>"""


def summary_pdf(meeting_id: int) -> dict | None:
    """Build (or rebuild) the summary PDF for one stored meeting, file it on the deal and record it on the row.
    Returns the library document, or None when the notes carry no summary to send."""
    import hashlib
    import os
    import tempfile
    from . import deck, documents, engine
    ensure_schema()
    row = db.one("select * from meeting_notes where id=%s", (int(meeting_id),))
    if not row:
        return None
    text = row.get("full_text") or ""
    if not text and _is_doc_id(row.get("file_id") or ""):
        try:
            text = _doc_text(row["file_id"], _tok())
        except Exception:  # noqa: BLE001
            text = ""
    lines = summary_lines(text, row.get("title") or "")
    if len(" ".join(lines)) < 120:
        return None
    deal_id = row.get("deal_id")
    co = store.get_company(row["company_id"]) if row.get("company_id") else None
    if not co and deal_id:
        p = db.one("select company from crm_projects where id=%s", (int(deal_id),)) or {}
        for slug in ("sensa", "skyvision", "filmspoke", "tabscanner", "snaprewards"):
            if (p.get("company") or "").lower() in (slug, crm._org(slug).lower()):
                co = store.get_company_by_slug(slug)
                break
    if not co:
        return None
    when = row.get("starts_at")
    day = (when.astimezone(timezone(timedelta(hours=4))).strftime("%-d %B %Y") if when else "")
    people = []
    for em in (row.get("attendees") or []):
        c = db.one("select first_name, last_name from crm_master where lower(email)=lower(%s)", (em,)) or {}
        people.append(((c.get("first_name") or "") + " " + (c.get("last_name") or "")).strip() or em)
    html_doc = _summary_html(co, row.get("title") or "Meeting", day, people, lines)
    with tempfile.TemporaryDirectory() as tmp:
        out = deck.to_pdf(html_doc, os.path.join(tmp, "notes.pdf"))
        data = open(out, "rb").read()
    client = (engine._deal_client_name(int(deal_id)) if deal_id else "") or (row.get("title") or "Meeting")
    short = (when.astimezone(timezone(timedelta(hours=4))).strftime("%-d %b %Y") if when else "undated")
    fname = re.sub(r"[^\w ,.&()'-]", "", f"Meeting notes, {client}, {short}")[:110].strip() + ".pdf"
    sha = hashlib.sha256(data).hexdigest()[:10]
    if deal_id:
        doc = engine._file_deal_document(co, int(deal_id), fname, "application/pdf", data, kind="meeting-summary",
                                         source="Gemini meeting notes, summary only", ref=f"meetsummary:{row['id']}:{sha}")
    else:
        doc = documents.save(co["id"], co.get("slug") or "", fname, "application/pdf", data, kind="meeting-summary",
                             uploaded_by="Gemini meeting notes, summary only", push=False)
    if not doc:
        return None
    old = row.get("summary_doc_id")
    if old and int(old) != int(doc["id"]):
        try:
            documents.supersede([int(old)], int(doc["id"]))
        except Exception:  # noqa: BLE001
            pass
    db.execute("update meeting_notes set summary_doc_id=%s where id=%s", (doc["id"], row["id"]))
    return doc


def ensure_summary_doc(deal_id=None, contact: str = "", meeting_id=None) -> dict | None:
    """The live summary PDF for a deal's newest meeting (or a contact's, or one meeting), built when missing."""
    from . import documents
    ensure_schema()
    if meeting_id:
        row = db.one("select id, summary_doc_id from meeting_notes where id=%s", (int(meeting_id),))
    elif deal_id:
        row = db.one("select id, summary_doc_id from meeting_notes where deal_id=%s "
                     "order by starts_at desc nulls last, id desc limit 1", (int(deal_id),))
    elif contact:
        row = db.one("select id, summary_doc_id from meeting_notes where attendees ? %s "
                     "order by starts_at desc nulls last, id desc limit 1", ((contact or "").lower(),))
    else:
        row = None
    if not row:
        return None
    if row.get("summary_doc_id"):
        d = db.one("select * from company_documents where id=%s and superseded_by is null", (row["summary_doc_id"],))
        if d:
            return d
    return summary_pdf(row["id"])


def _attach_summary_to_request(req: dict, deal_id=None, contact: str = "", meeting_id=None,
                               rebuild: bool = False) -> dict | None:
    """Put the summary PDF on an email request (replacing an earlier summary) and say so in the brief, above the
    notes marker so a refresh of the notes keeps it. Never fails the card."""
    try:
        from . import documents
        doc = (summary_pdf(int(meeting_id)) if (rebuild and meeting_id)
               else ensure_summary_doc(deal_id=deal_id, contact=contact, meeting_id=meeting_id))
        if not doc:
            return None
        refs = [r for r in (req.get("attach_docs") or [])
                if not str(r.get("filename") or "").startswith("Meeting notes,")]
        refs.append(documents.card_ref(doc))
        req["attach_docs"] = refs
        line = f"ATTACHED: {doc['filename']} (the meeting notes, summary only).\n"
        brief = req.get("brief") or ""
        if "ATTACHED: Meeting notes," in brief:
            brief = re.sub(r"ATTACHED: Meeting notes,[^\n]*\n", line, brief)
        else:
            marker = "MEETING NOTES (distilled):\n"
            brief = brief.replace(marker, line + marker, 1) if marker in brief else brief.rstrip() + "\n" + line
        req["brief"] = brief
        return doc
    except Exception as e:  # noqa: BLE001
        print(f"[meetnotes] summary not attached: {type(e).__name__}: {e}", flush=True)
        return None


def attach_summary(task_id: int) -> dict | None:
    """Attach the meeting summary to an existing email card and reconcile its wording (card 1301)."""
    from . import engine
    t = store.get_task(int(task_id))
    if not t:
        return None
    req = dict(t.get("request") or {})
    doc = _attach_summary_to_request(req, deal_id=t.get("deal_id") or req.get("deal_id"),
                                     contact=((req.get("inquiry") or {}).get("email") or ""))
    if not doc:
        return None
    store.update_task(int(task_id), request=req)
    try:
        engine.reconcile_attachments(int(task_id))
    except Exception as e:  # noqa: BLE001
        print(f"[meetnotes] reconcile {task_id}: {type(e).__name__}: {e}", flush=True)
    return doc


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


_QUOTES = "\"'“”‘’"


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _subject_title(subject: str) -> str:
    """The MEETING's own title out of Gemini's subject line:
    'Notes: "Call with Mai" Sep 1, 2026' -> 'Call with Mai'. This is the only hard link between a
    notes email and a calendar entry, so it is what attribution is proven against."""
    for rx in (_NOTES_SUBJ_TIME, _NOTES_SUBJ_DAY):
        m = rx.search(subject or "")
        if m:
            return (m.group("rest") or "").strip().strip(_QUOTES)
    return (subject or "").replace("Notes:", "").strip().strip(_QUOTES)


def _attendees_at(start: datetime, whole_day: bool = False,
                  want_title: str = "") -> tuple[list[str], str, str]:
    """Who was in THAT meeting, from the calendars — INCLUDING cancelled events, which is the whole
    point: the event may have been deleted and the meeting still happened. Returns
    (emails, event_title, refusal_reason); a non-empty reason means NOT ATTRIBUTED.

    Attribution has to be PROVEN, not assumed. This used to take the first event in the window with
    an external guest, so Mai's MAH Gold notes (subject carried a date but no time, widening the
    window to the whole day) were attributed to the ECBD call that ran that morning, and three of
    her commitments were written onto ECBD's tender deal (1 Sep 2026). The notes title must now
    match the calendar entry; a bare time window only decides it when exactly one meeting is in it
    and the clock is precise. Anything ambiguous is refused, because a wrong client is worse than
    no client."""
    from . import calendar as gcal
    if whole_day:
        lo = start.replace(hour=0, minute=0, second=0, microsecond=0)
        hi = lo + timedelta(days=1)
    else:
        lo, hi = start - timedelta(minutes=45), start + timedelta(minutes=45)
    cands, seen = [], set()
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
            ext = sorted({(a.get("email") or "").lower() for a in (ev.get("attendees") or [])
                          if a.get("email") and not _is_us(a["email"])})
            if not ext:
                continue
            # the same meeting sits on several of our calendars; count it once
            k = (ev.get("iCalUID") or ev.get("id")
                 or _norm_title(ev.get("summary")) + "|" + str((ev.get("start") or {}).get("dateTime")))
            if k in seen:
                continue
            seen.add(k)
            cands.append({"title": ev.get("summary") or "", "ext": ext})
    if not cands:
        return [], "", "no calendar event with an external guest in the window"
    want = _norm_title(want_title)
    hits = []
    for c in cands:
        got = _norm_title(c["title"])
        if want and (got == want or (len(want) >= 6 and (want in got or got in want))):
            hits.append(c)
    if hits:
        if len({tuple(c["ext"]) for c in hits}) == 1:
            return list(hits[0]["ext"]), hits[0]["title"], ""
        return [], "", f"several meetings titled like '{want_title}' with different guests"
    if not whole_day and len(cands) == 1:      # precise start, one meeting: the clock is the proof
        return list(cands[0]["ext"]), cands[0]["title"], ""
    found = ", ".join(f"'{c['title']}'" for c in cands[:4]) or "nothing"
    return [], "", f"'{want_title}' matches no meeting in the window (calendar had {found})"


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
                if key in seen_keys:
                    continue
                _doc = _doc_in_email(full.get("payload") or {}, body)
                if _doc and db.one("select 1 from meeting_notes where file_id=%s", (_doc,)):
                    seen_keys.add(key)                # this document is already stored (calendar path, or a copy)
                    continue
                _full = _full_notes(_doc)
                if _doc and not _full and _age_minutes(hdr.get("Date")) < FULL_NOTES_WAIT_MIN:
                    continue                          # the document is not readable yet: wait, never draft from the summary
                text = _full or body
                _fid = _doc if _full else ref["id"]   # a doc id only for a doc we read, so the calendar path can upgrade
                if db.one("select 1 from meeting_notes where event_id=%s", (key,)):
                    _first = db.one("select * from meeting_notes where event_id=%s", (key,)) or {}
                    if _full and not _is_doc_id(_first.get("file_id") or ""):
                        _upgrade_notes(_first, _doc, _full, backfill)     # stored short earlier; the document is here now
                        seen_keys.add(key)
                        continue
                    _fam = _family(key, _first.get("title") or "", _first.get("starts_at"), _first.get("deal_id"))
                    if _already_have(_fam, _doc, text):
                        seen_keys.add(key)
                        continue
                    _resumed_session(_fam, key, _fid, _first.get("title") or _subject_title(subject),
                                     _first.get("starts_at") or start, text, json.loads(json.dumps(_first.get("attendees") or [])),
                                     _first.get("company_id"), _first.get("deal_id"), backfill)
                    seen_keys.add(key)
                    made.append({"key": key + " (resumed)", "deal": _first.get("deal_id")})
                    continue
                want_title = _subject_title(subject)
                emails, ev_title, unmatched = (
                    _attendees_at(start, whole_day=not has_time, want_title=want_title)
                    if start else ([], "", "the subject carries no meeting date"))
                title = ev_title or want_title or subject.replace("Notes:", "").strip()
                summary = _distil(title, text)
                company_id = deal_id = None
                for em in emails:
                    for slug in ("sensa", "skyvision", "tabscanner", "filmspoke", "snaprewards"):
                        ds = crm.active_deals_for_email(em, slug)
                        if ds:
                            co = store.get_company_by_slug(slug)
                            _b = _bound_deal("", title, start, ds)
                            company_id, deal_id = (co or {}).get("id"), (_b or {}).get("id")
                            break
                    if deal_id:
                        break
                if not deal_id:                   # the owner linked this meeting to a deal its people are not on yet
                    _lc, _ld = _linked_deal("", title, start)
                    if _ld:
                        company_id, deal_id = _lc or company_id, _ld
                db.execute(
                    "insert into meeting_notes (event_id, file_id, company_id, deal_id, title, starts_at,"
                    " attendees, summary) values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (event_id) do nothing",
                    (key, _fid, company_id, deal_id, title, start, json.dumps(emails), summary))
                _keep_full(key, title, text, company_id, deal_id, start)
                for em in emails:
                    try:
                        crm.log_event(em, "meeting_notes", f"Meeting: {title} — notes captured", None)
                    except Exception:  # noqa: BLE001
                        pass
                if not backfill:
                    try:   # the same follow-up the calendar path drafts (it dedups on an email since the meeting)
                        from .identity import is_internal
                        _ext = [x for x in (emails or []) if x and not is_internal(x)]
                        _spawn_post_meeting_followup({"summary": title, "start": {"dateTime": str(start)}}, _ext,
                                                     company_id, deal_id, summary)
                    except Exception as _fe:  # noqa: BLE001
                        print(f"[meetnotes] follow-up: {type(_fe).__name__}: {_fe}", flush=True)
                if deal_id:
                    from . import pipeline
                    pipeline.record_meeting(deal_id, company_id, title, summary, commitments=not backfill)
                elif unmatched:
                    # captured, deliberately not filed: say so once, rather than guessing a client
                    from . import notifications
                    notifications.notify(
                        f"Meeting notes not filed: {title[:70]}",
                        f"Gemini sent notes for this meeting and Cortex could not prove which client "
                        f"they belong to, so nothing was written to a deal ({unmatched}). "
                        f"The notes are stored and searchable.",
                        priority="fyi", category="meeting", dedup_key="meetnotes-unfiled:" + key)
                seen_keys.add(key)
                made.append({"key": key, "deal": deal_id, "title": title[:60]})
            except Exception as e:  # noqa: BLE001 — one bad message never stops the rest
                print(f"[meetnotes-email] {type(e).__name__}: {e}", flush=True)
                continue
    return {"captured": made}
