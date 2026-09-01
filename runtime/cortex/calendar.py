"""Per-company Google Calendar availability — read free/busy and propose real open booking slots.

Used by the sales-reply drafting so an inbound lead is offered a couple of genuinely-open times (pulled live
from the company's booking calendar) instead of just a generic scheduling link. Auth = the per-company Internal
OAuth client + `calendar_refresh_token:<company>` (token belongs to the calendar's own account, e.g. hello@sensa
.digital). Read path uses freeBusy; booking (create_event) is read-write and stays gated behind Inbox approval.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from . import db
from .schedule import _GST

_CLIENT = "/etc/cortex/google_oauth_client_{slug}.json"


def _token(company: str) -> str:
    """Exchange the company's calendar refresh token (its own Internal OAuth client) for an access token."""
    from . import db
    rt = db.setting_get(f"calendar_refresh_token:{company}")
    if not rt:
        raise RuntimeError(f"no calendar_refresh_token:{company} — authorise purpose=calendar first")
    c = json.load(open(_CLIENT.format(slug=company), encoding="utf-8"))["web"]
    data = urllib.parse.urlencode({"client_id": c["client_id"], "client_secret": c["client_secret"],
                                   "refresh_token": rt, "grant_type": "refresh_token"}).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data), timeout=30))["access_token"]


def _busy(tok: str, calendar_id: str, start: datetime, end: datetime, tz: str) -> list[tuple]:
    body = json.dumps({"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                       "timeZone": tz, "items": [{"id": calendar_id}]}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://www.googleapis.com/calendar/v3/freeBusy", data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST"), timeout=30))
    cal = (r.get("calendars") or {}).get(calendar_id) or {}
    return [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in cal.get("busy", [])]




def _cal_id(company: str, calendar_id: str) -> str:
    """The company's configured booking calendar (setting calendar_id:<slug>, e.g. Sensa Productions
    Main Calender) when the caller didn't name one — 'primary' only as the last resort."""
    if calendar_id and calendar_id != "primary":
        return calendar_id
    from . import db
    return db.setting_get(f"calendar_id:{company}") or "primary"


def free_slots(company: str, *, calendar_id: str = "primary", days: int = 21, work_start: int = 10,
               work_end: int = 14, slot_min: int = 30, count: int = 3, buffer_min: int = 180,
               weekdays: tuple = (0, 1, 2, 3), tz: str = "Asia/Dubai",
               prefer: tuple = ("10:00", "11:30", "13:00")) -> list[datetime]:
    """Return up to `count` genuinely-open slot start times (tz-aware), ONE per day, spread across business days
    and varied across the preferred times so it reads naturally without exposing the whole calendar. Defaults to
    the Sensa booking rules: 10:00-14:00 GST, Mon-Thu (Fridays + weekend excluded), 3-hour lead time. `prefer`
    rotates the target time per offered slot so they aren't all at the same hour."""
    tzi = ZoneInfo(tz)
    calendar_id = _cal_id(company, calendar_id)
    tok = _token(company)
    now = datetime.now(tzi)
    start = now + timedelta(minutes=buffer_min)
    end = now + timedelta(days=days)
    busy = _busy(tok, calendar_id, start, end, tz)
    prefs = [tuple(int(x) for x in p.split(":")) for p in prefer] or [(work_start, 0)]
    slots: list[datetime] = []
    day = start.date()
    while day <= end.date() and len(slots) < count:
        if day.weekday() in weekdays:
            ph, pm = prefs[len(slots) % len(prefs)]          # rotate the target time across offered slots
            target = datetime(day.year, day.month, day.day, ph, pm, tzinfo=tzi)
            win_end = datetime(day.year, day.month, day.day, work_end, 0, tzinfo=tzi)
            t = datetime(day.year, day.month, day.day, work_start, 0, tzinfo=tzi)
            day_free = []
            while t + timedelta(minutes=slot_min) <= win_end:
                s_end = t + timedelta(minutes=slot_min)
                if t >= start and not any(t < be and bs < s_end for bs, be in busy):
                    day_free.append(t)
                t = s_end
            if day_free:                                      # one slot/day: the first free at/after the target
                slots.append(next((x for x in day_free if x >= target), day_free[0]))
        day += timedelta(days=1)
    return slots


def format_slots(slots: list[datetime]) -> list[str]:
    """Human-readable slot strings, e.g. 'Tuesday 24 June, 2:00pm'."""
    return [s.strftime("%A %-d %B, %-I:%M%p").replace("AM", "am").replace("PM", "pm") for s in slots]


def create_event(company: str, *, calendar_id: str = "primary", start: datetime, minutes: int = 30,
                 summary: str = "Call", attendee: str = "", description: str = "",
                 meet: bool = False) -> dict:
    """Create a booking event (read-write). Caller MUST gate this behind Inbox approval — never auto-book.
    meet=True attaches a Google Meet room; the returned dict carries its join link."""
    import uuid
    calendar_id = _cal_id(company, calendar_id)
    tok = _token(company)
    end = start + timedelta(minutes=minutes)
    ev: dict = {"summary": summary, "description": description,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    if attendee:
        ev["attendees"] = [{"email": attendee}]
    if meet:
        ev["conferenceData"] = {"createRequest": {
            "requestId": uuid.uuid4().hex,
            "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events"
        "?sendUpdates=all&conferenceDataVersion=1", data=json.dumps(ev).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST"), timeout=30))
    meet_link = r.get("hangoutLink") or next(
        (e.get("uri") for e in (r.get("conferenceData", {}).get("entryPoints") or [])
         if e.get("entryPointType") == "video"), "")
    return {"id": r.get("id"), "link": r.get("htmlLink"), "meet": meet_link}


def delete_event(company: str, event_id: str, calendar_id: str = "primary", notify: bool = False) -> None:
    """Remove an event (e.g. a pre-booked meeting whose card was skipped) so phantom bookings never
    accumulate and block availability. notify=True sends guests a cancellation."""
    calendar_id = _cal_id(company, calendar_id)
    tok = _token(company)
    urllib.request.urlopen(urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events/"
        f"{urllib.parse.quote(event_id)}?sendUpdates=" + ("all" if notify else "none"),
        headers={"Authorization": f"Bearer {tok}"}, method="DELETE"), timeout=30)


def add_attendee(company: str, event_id: str, attendee: str, calendar_id: str = "primary") -> dict:
    """Add the guest to an existing event and let Google send them the invite — the approval-time step
    after an event was pre-booked (attendee-less). MERGES with existing attendees (a PATCH of the
    attendees array REPLACES it; replacing would cancel-notify anyone already on the event)."""
    calendar_id = _cal_id(company, calendar_id)
    tok = _token(company)
    cur = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events/"
        f"{urllib.parse.quote(event_id)}", headers={"Authorization": f"Bearer {tok}"}), timeout=30))
    have = [a for a in (cur.get("attendees") or [])]
    if not any((a.get("email") or "").lower() == attendee.lower() for a in have):
        have.append({"email": attendee})
    body = json.dumps({"attendees": have}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events/"
        f"{urllib.parse.quote(event_id)}?sendUpdates=all", data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="PATCH"), timeout=30))
    return {"id": r.get("id")}


# ---------- AVAILABILITY: one busy picture across every calendar that owns Rashad's time ----------
#
# The registry (setting `availability_calendars`) is a list of {"company": <token slug>, "id": <calendar
# id>, "label": ...}. Each entry is queried with THAT company's token, so calendars living under
# different Google accounts merge into one answer. Rashad's personal work calendar is shared into
# rashad@tabscanner.com as FREE/BUSY ONLY - Cortex sees that he is busy, never what the appointment is
# (owner, 31 Aug 2026). Proposed times are computed from this; the model never guesses availability.

WORK_START, WORK_END = 9, 18          # GST working window used when nothing narrower is asked for
PREFER_START, PREFER_END = 10, 14     # owner's preferred calling window (GST): 10am-2pm
CLIENT_START, CLIENT_END = 9, 18      # the slot must also be a sane hour where the CLIENT sits
SLOT_MINUTES = 30
# PREP GAP (owner, 31 Aug 2026): meetings must never butt together. Rashad reads the pre-meeting brief
# in the minutes before he walks in, so every proposed slot keeps this much clear air on both sides of
# anything already booked. Bunching still applies - calls cluster, they just stop touching.
PREP_GAP_MINUTES = 15


def _registry() -> list:
    return db.setting_get("availability_calendars") or []


def busy_blocks(start: datetime, end: datetime) -> list:
    """Every busy interval across the registered calendars, merged and sorted. Fail-soft per company:
    one unreachable account never hides the others (it just contributes nothing)."""
    import collections
    by_co = collections.defaultdict(list)
    for e in _registry():
        by_co[e.get("company") or "sensa"].append({"id": e["id"]})
    out = []
    for co, items in by_co.items():
        try:
            r = httpx.post(f"{'https://www.googleapis.com/calendar/v3'}/freeBusy",
                           json={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                                 "timeZone": "Asia/Dubai", "items": items},
                           headers={"Authorization": f"Bearer {_token(co)}"}, timeout=30)
            for _cid, val in (r.json().get("calendars") or {}).items():
                for b in (val.get("busy") or []):
                    out.append((datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                                datetime.fromisoformat(b["end"].replace("Z", "+00:00"))))
        except Exception:  # noqa: BLE001
            continue
    out.sort()
    merged = []
    for s, e in out:                       # collapse overlaps so a slot check is one clean pass
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def free_slots(days: int = 10, minutes: int = 30, tz: str = "Asia/Dubai",
               earliest_hour: int | None = None, latest_hour: int | None = None,
               skip_weekends: bool = True, min_notice_hours: int = 18, limit: int = 8) -> list:
    """Real openings, in the CLIENT's timezone. Skips anything busy on any registered calendar, keeps
    the working window, honours a minimum notice so we never propose 'in an hour', and skips weekends."""
    from zoneinfo import ZoneInfo
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("Asia/Dubai")
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=min_notice_hours)
    end = now + timedelta(days=days)
    busy = busy_blocks(start, end)
    lo = WORK_START if earliest_hour is None else earliest_hour
    hi = WORK_END if latest_hour is None else latest_hour
    # BUNCHING (owner, 31 Aug 2026): calls should cluster, not scatter a day into fragments. Every free
    # slot is scored - sitting a short hop after an existing meeting wins (a short hop, NOT touching:
    # the prep gap is protected), then the 10am-2pm window, then the earlier date - and we offer at
    # most two options per day.
    gap = timedelta(minutes=PREP_GAP_MINUTES)
    near = timedelta(minutes=75)     # "next to" a meeting means a short hop away, not the same hour
    cand, cur = [], start.astimezone(_GST).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while cur < end:
        if not (skip_weekends and cur.weekday() >= 5) and lo <= cur.hour < hi:
            fin = cur + timedelta(minutes=minutes)
            # the busy block is treated as PREP_GAP wider at both ends, so a proposed slot can never
            # start the moment another meeting finishes (or finish the moment the next one starts)
            if not any(s - gap < fin and cur < e + gap for s, e in busy):
                adjacent = any(gap <= (cur - e) <= near or gap <= (s - fin) <= near
                               for s, e in busy)
                # CIVILISED FOR THEM TOO: 09:00 Dubai is 07:00 Amsterdam. A slot must sit inside the
                # working day at BOTH ends, or we propose times no client would take.
                their = cur.astimezone(zone)
                if not (CLIENT_START <= their.hour < CLIENT_END):
                    cur += timedelta(minutes=SLOT_MINUTES)
                    continue
                preferred = PREFER_START <= cur.hour < PREFER_END
                cand.append((0 if adjacent else 1, 0 if preferred else 1, cur))
        cur += timedelta(minutes=SLOT_MINUTES)
    cand.sort(key=lambda x: (x[0], x[1], x[2]))
    per_day, slots = {}, []
    for _adj, _pref, dt in cand:
        d = dt.date()
        if per_day.get(d, 0) >= 2:
            continue
        per_day[d] = per_day.get(d, 0) + 1
        slots.append(dt.astimezone(zone))
        if len(slots) >= limit:
            break
    return sorted(slots)


def availability_block(tz: str = "Asia/Dubai", days: int = 10, minutes: int = 30) -> str:
    """The drafter's view of real availability: a short list of genuinely free times it may offer.
    Empty string when nothing can be read, so a draft falls back to proposing in words."""
    try:
        s = free_slots(days=days, minutes=minutes, tz=tz)
    except Exception:  # noqa: BLE001
        return ""
    if not s:
        return ""
    label = tz.split("/")[-1].replace("_", " ")
    lines = [f"- {d.strftime('%A %-d %B, %H:%M')} ({label})" for d in s]
    return ("GENUINELY FREE TIMES (computed from our real calendars - offer ONLY from this list, in the "
            "recipient's timezone; never invent a time). They are ordered by preference - the first "
            "ones sit shortly after an existing meeting or inside the preferred window, so offer the "
            "top two or three:\n" + "\n".join(lines))


# ---------- UPCOMING EVENTS: the detail view, used by the pre-meeting brief ----------
#
# busy_blocks() answers "is he free?" and deliberately sees nothing else — the personal calendar is
# shared as FREE/BUSY ONLY. This reads the actual entries (title, attendees, description) from the
# calendars where we DO have detail access, so a brief can be written before a real meeting. Any
# calendar that only exposes free/busy simply returns nothing here; that is expected, not an error.

def upcoming_events(hours: int = 30) -> list[dict]:
    """Every readable event starting in the next `hours`, one flat list across the registry.

    Each row carries the company slug of the calendar it came from, so the brief knows which brand
    is taking the meeting. Fail-soft per calendar: a 403 on a free/busy-only share is normal."""
    start = datetime.now(timezone.utc)
    end = start + timedelta(hours=hours)
    out: list[dict] = []
    for entry in _registry():
        co = entry.get("company") or "sensa"
        cal_id = entry.get("id") or "primary"
        try:
            tok = _token(co)
            q = urllib.parse.urlencode({
                "timeMin": start.isoformat(), "timeMax": end.isoformat(),
                "singleEvents": "true", "orderBy": "startTime", "maxResults": "50"})
            r = httpx.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal_id)}/events?{q}",
                headers={"Authorization": f"Bearer {tok}"}, timeout=30)
            if r.status_code != 200:
                continue
            items = r.json().get("items") or []
        except Exception:  # noqa: BLE001 — one unreachable calendar never hides the others
            continue
        for ev in items:
            s = (ev.get("start") or {})
            if s.get("date") and not s.get("dateTime"):
                continue                      # all-day entries are markers, not meetings
            if (ev.get("status") or "") == "cancelled":
                continue
            try:
                starts = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            # our own RSVP: never brief a meeting the owner has declined
            declined = any((a.get("self") and a.get("responseStatus") == "declined")
                           for a in (ev.get("attendees") or []))
            if declined:
                continue
            out.append({
                "company": co,
                "calendar_id": cal_id,
                "event_id": ev.get("id"),
                "title": (ev.get("summary") or "").strip(),
                "description": (ev.get("description") or "").strip()[:4000],
                "location": (ev.get("location") or "").strip(),
                "hangout": (ev.get("hangoutLink") or "").strip(),
                "organizer": ((ev.get("organizer") or {}).get("email") or "").lower(),
                "starts_at": starts,
                "attendees": [
                    {"email": (a.get("email") or "").lower(),
                     "name": (a.get("displayName") or "").strip(),
                     "self": bool(a.get("self")),
                     "optional": bool(a.get("optional")),
                     "response": a.get("responseStatus") or ""}
                    for a in (ev.get("attendees") or []) if a.get("email")],
            })
    out.sort(key=lambda e: e["starts_at"])
    return out


def event_has_guests(company: str, event_id: str, calendar_id: str = "primary") -> bool:
    """Does this event have anyone invited to it? A guest on an event means deleting it EMAILS them a
    cancellation, so nothing may delete it silently. Fails CLOSED: if we cannot tell, we say yes, and
    the caller leaves the event alone (a stale slot costs availability; a wrongly-cancelled client
    meeting costs the meeting)."""
    try:
        calendar_id = _cal_id(company, calendar_id)
        tok = _token(company)
        r = httpx.get(
            f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}"
            f"/events/{urllib.parse.quote(event_id)}",
            headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        if r.status_code != 200:
            return True
        ev = r.json()
        return bool([a for a in (ev.get("attendees") or []) if not a.get("self")])
    except Exception:  # noqa: BLE001
        return True
