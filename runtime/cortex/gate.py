"""ONE answer to "may Cortex contact this person, unprompted, right now?" (architecture audit, step 2b, 13 Oct 2026).

Before this, every automatic sender carried its own checks: the deal chase had the full set, nurture its own, reminder
and promise emails almost none, post-meeting follow-ups a card check only, and a queued card released later by
`store.promote_queued` was never re-checked at all. Recent-contact windows were 3 days, 2 working days, 21 days or
nothing. So a chase could follow a client's reply, a promise email could land the day after a chase, and a queued
card could go out on a deal that had closed while it waited.

`check()` is called in exactly two places, which every automatic email passes through:
  - `store.create_card`, when the card is made;
  - `store.promote_queued`, when a card that waited in line is released (the world may have changed meanwhile).
An email the OWNER asked for (a reply to their mail, Talk, the cockpit, his own reminder) is never automatic and is
never held here. A held card is not created, or is cancelled with the reason on it, and the deal's timeline says why.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db, stages

AUTO_FOLLOWUPS = ("chase", "checkin", "followup", "revive", "nurture", "post-meeting")
CHASE_TYPES = ("chase", "checkin", "followup", "revive", "lead_chase")

# how long after OUR last email to this person an unprompted one may follow, by purpose
QUIET_DAYS = {"chase": 3, "checkin": 3, "followup": 3, "lead_chase": 3, "reminder": 2, "commitment": 2,
              "revive": 14, "nurture": 14, "post-meeting": 0}


def purpose(request: dict | None) -> str | None:
    """Why an automatic card exists, or None when a person asked for it (never gated)."""
    r = request or {}
    if r.get("lead_chase"):
        return "lead_chase"
    fu = str(r.get("followup") or "")
    if fu in AUTO_FOLLOWUPS:
        return fu
    if r.get("auto_source"):                         # set by reminders._spawn_task for a reminder Cortex created
        return str(r["auto_source"])
    return None


def _ts(v) -> datetime | None:
    if not v:
        return None
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _manual_owner(company_id, deal_id, email: str, request: dict | None) -> str:
    """A manual person (profile `manual_people`, owner 14 Oct 2026) owns this: the deal's owner, the card's own
    sender, or the last of our people to send to this contact. No automatic email goes out on their work."""
    try:
        from . import profile
        manual = {str(a).lower() for a in (profile.get(company_id) or {}).get("manual_people") or []}
        if not manual:
            return ""
        if str(deal_id or "").isdigit():
            own = str((db.one("select owner from crm_projects where id=%s", (int(deal_id),)) or {}).get("owner") or "").lower()
            if own in manual:
                return own
        fe = str((request or {}).get("from_email") or "").lower()
        if fe in manual:
            return fe
        if email:
            last = db.one("select lower(d.snapshot->>'from') f from decisions d join tasks t on t.id=d.task_id where "
                          "t.company_id=%s and d.action='send' and lower(d.snapshot->>'to')=%s order by d.id desc limit 1",
                          (company_id, email.lower()))
            if (last or {}).get("f") in manual:
                return last["f"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _rhythm_floor(deal: dict, hist: list):
    """When the next chase may go: the deal's current rhythm step, counted from the last email we sent."""
    try:
        from . import crm
        outs = [_ts(e.get("ts")) for e in hist if str(e.get("event", "")).startswith("email_out")]
        last_out = max((t for t in outs if t), default=None)
        if not last_out:
            return None
        cad = deal.get("cadence") if isinstance(deal.get("cadence"), dict) and deal["cadence"].get("steps") \
            else crm.get_cadence(deal.get("company"))
        return crm._schedule_point(cad, int(deal.get("followup_step") or 0), base=last_out)
    except Exception:  # noqa: BLE001
        return None


_LAST = __import__("threading").local()


def last_hold() -> str:
    """The reason the most recent check on this thread held an email ('' if it did not)."""
    return getattr(_LAST, "why", "") or ""


def check(company_id: int, email: str, deal_id, request: dict | None) -> str | None:
    _LAST.why = ""
    r = _check(company_id, email, deal_id, request)
    _LAST.why = r or ""
    return r


def _check(company_id: int, email: str, deal_id, request: dict | None) -> str | None:
    """None = may contact. Otherwise the reason it is held, in words for the owner."""
    why = purpose(request)
    if not why:
        return None
    email = (email or "").strip().lower()
    now = datetime.now(timezone.utc)

    # 1. the person
    if email:
        c = db.one("select lead_status, classification from crm_master where lower(email)=%s", (email,)) or {}
        if c.get("lead_status") == "left-company":
            return f"{email} has left their company"
        if c.get("classification") == "not_qualified":
            return f"{email} is marked not qualified"

    # 2. the deal's stage (stages.py decides what each stage allows)
    deal = db.one("select id, stage, history, cadence, followup_step, company, automation from crm_projects where id=%s",
                  (int(deal_id),)) if str(deal_id or "").isdigit() else None
    held = _manual_owner(company_id, deal_id, email, request)
    if held:
        return f"{held.split('@')[0].title()} handles this by hand"
    hist = (deal or {}).get("history") or []
    if deal:
        st = deal.get("stage")
        if st in ("Lost", "Completed"):
            return f"the deal is {st}"
        if why in CHASE_TYPES:
            r = stages.rhythm(st)
            if r is None:
                return f"a deal in {st} is not chased"
            if r == "revival" and why != "revive":
                return f"a {st} deal only gets soft revivals"
        stopped = max((_ts(e.get("ts")) for e in hist if e.get("event") in ("chase_stopped", "chase_paused")), default=None)
        last_mail = max((_ts(e.get("ts")) for e in hist if str(e.get("event", "")).startswith(("email_", "whatsapp_"))),
                        default=None)
        if why in CHASE_TYPES and stopped and (not last_mail or stopped >= last_mail):
            return "chasing is stopped or paused on this deal until they or we write again"
        # THE CHASE FOLLOWS THE NEXT-STEPS LIST (18 Oct 2026, Shama #164: a chase was armed while WE owed her the
        # treatments): while anything on the list is OUR move, nobody chases the client.
        if why in CHASE_TYPES:
            try:
                from . import nextsteps
                _ours = nextsteps.ours_open(int(deal["id"]))
            except Exception:  # noqa: BLE001
                _ours = []
            if _ours:
                return f"we owe them the next step: {_ours[0]['what'][:120]}"
        # THE RHYTHM COUNTS FROM OUR LAST EMAIL (15 Oct 2026, Massar #117): the 13 Oct migration set the next chase
        # for Thu 15 Oct without seeing the chase that went that morning, and the 0.8-day quiet window let a second
        # chase through two days later. A chase waits its step's full gap from the last email we actually sent.
        if why in CHASE_TYPES:
            nxt = _rhythm_floor(deal, hist)
            if nxt and now < nxt - timedelta(hours=6):
                if deal.get("automation") == "auto":
                    db.execute("update crm_projects set next_followup=%s where id=%s and (next_followup is null or "
                               "next_followup < %s)", (nxt, deal["id"], nxt))
                return f"the next chase is due from {nxt:%a %d %b}, a full rhythm step after our last email"

    # 3. who spoke last, on any channel we can see
    if email and why != "post-meeting":
        theirs = [_ts(e.get("ts")) for e in hist if e.get("event") in ("email_in", "whatsapp_in")]
        ch = db.one("select history from crm_master where lower(email)=%s", (email,)) or {}
        theirs += [_ts(h.get("ts")) for h in (ch.get("history") or []) if h.get("event") in ("inbound_email", "enquiry")]
        last_in = max((t for t in theirs if t), default=None)
        ours = [_ts(e.get("ts")) for e in hist if str(e.get("event", "")).startswith("email_out")]
        s = db.one("select max(d.created_at) at from decisions d join tasks t on t.id=d.task_id where d.action='send' "
                   "and lower(t.request->'inquiry'->>'email')=%s", (email,)) or {}
        ours.append(_ts(s.get("at")))
        last_out = max((t for t in ours if t), default=None)
        # a message the owner chose not to pursue is answered, though nothing was sent (engine.not_interested)
        declined = max((_ts(e.get("ts")) for e in hist if e.get("event") == "not_pursued"), default=None)
        last_answer = max((t for t in (last_out, declined) if t), default=None)
        if last_in and (not last_answer or last_in > last_answer):
            return f"they wrote to us last ({last_in:%d %b}), so a reply is owed, not a nudge"
        quiet = QUIET_DAYS.get(why, 2)
        # a quoted deal's own rhythm decides its gaps (receipt check the next working day, then weekly); the closing
        # gate holds anything inside two working days, so this only stops a same-day repeat
        if why == "chase" and deal and stages.rhythm(deal.get("stage")) == "closing":
            quiet = 0.8
        if quiet and last_out and now - last_out < timedelta(days=quiet):
            return f"we emailed them on {last_out:%d %b}, under {quiet:g} days ago"
        # A MEETING ALREADY BOOKED IS CONTACT TOO (14 Oct 2026: the Shama #164 chase re-armed the day before their
        # meeting): nothing automatic goes to someone we are about to meet; the meeting notes take it from there
        try:
            from . import calendar as _cal
            soon = _cal.next_meeting_with(email)
        except Exception:  # noqa: BLE001
            soon = None
        if soon:
            return f"a meeting with them is booked for {soon:%d %b}"
        # A MEETING IS CONTACT (audit 13 Oct): a "they have not replied" chase could go out the day after a call
        met = db.one("select max(starts_at) at from meeting_notes where attendees @> %s::jsonb and starts_at <= now()",
                     (f'["{email}"]',)) or {}
        met_at = _ts(met.get("at"))
        if met_at and now - met_at < timedelta(days=max(quiet, 2)):
            return f"we met them on {met_at:%d %b}"
        # A CALL OR WHATSAPP HE LOGGED IS CONTACT TOO (personal touch, touch.py, 13 Oct 2026)
        spoke = max((_ts(e.get("ts")) for e in hist if e.get("event") == "touch_made"), default=None)
        if spoke and now - spoke < timedelta(days=max(quiet, 2)):
            return f"we spoke to them on {spoke:%d %b} (a logged call or WhatsApp)"
    return None


def note_hold(deal_id, reason: str, request: dict | None) -> None:
    """The hold is written on the deal, so nothing is held silently."""
    if not str(deal_id or "").isdigit():
        return
    try:
        from . import pipeline
        pipeline.log_deal(int(deal_id), "note", f"No {purpose(request) or 'automatic'} email drafted: {reason}.")
    except Exception:  # noqa: BLE001
        pass
