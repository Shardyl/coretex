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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
        "https://oauth2.googleapis.com/token", data=data)))["access_token"]


def _busy(tok: str, calendar_id: str, start: datetime, end: datetime, tz: str) -> list[tuple]:
    body = json.dumps({"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                       "timeZone": tz, "items": [{"id": calendar_id}]}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://www.googleapis.com/calendar/v3/freeBusy", data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST")))
    cal = (r.get("calendars") or {}).get(calendar_id) or {}
    return [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in cal.get("busy", [])]


def free_slots(company: str, *, calendar_id: str = "primary", days: int = 21, work_start: int = 10,
               work_end: int = 14, slot_min: int = 30, count: int = 3, buffer_min: int = 180,
               weekdays: tuple = (0, 1, 2, 3), tz: str = "Asia/Dubai",
               prefer: tuple = ("10:00", "11:30", "13:00")) -> list[datetime]:
    """Return up to `count` genuinely-open slot start times (tz-aware), ONE per day, spread across business days
    and varied across the preferred times so it reads naturally without exposing the whole calendar. Defaults to
    the Sensa booking rules: 10:00-14:00 GST, Mon-Thu (Fridays + weekend excluded), 3-hour lead time. `prefer`
    rotates the target time per offered slot so they aren't all at the same hour."""
    tzi = ZoneInfo(tz)
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
                 summary: str = "Call", attendee: str = "", description: str = "") -> dict:
    """Create a booking event (read-write). Caller MUST gate this behind Inbox approval — never auto-book."""
    tok = _token(company)
    end = start + timedelta(minutes=minutes)
    ev: dict = {"summary": summary, "description": description,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    if attendee:
        ev["attendees"] = [{"email": attendee}]
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id)}/events"
        "?sendUpdates=all", data=json.dumps(ev).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, method="POST")))
    return {"id": r.get("id"), "link": r.get("htmlLink")}
