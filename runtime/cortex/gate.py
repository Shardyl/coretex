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


def check(company_id: int, email: str, deal_id, request: dict | None) -> str | None:
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
    deal = db.one("select id, stage, history from crm_projects where id=%s", (int(deal_id),)) if str(deal_id or "").isdigit() else None
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
        stopped = max((_ts(e.get("ts")) for e in hist if e.get("event") == "chase_stopped"), default=None)
        last_mail = max((_ts(e.get("ts")) for e in hist if str(e.get("event", "")).startswith(("email_", "whatsapp_"))),
                        default=None)
        if why in CHASE_TYPES and stopped and (not last_mail or stopped >= last_mail):
            return "the owner stopped chasing this deal"

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
        if last_in and (not last_out or last_in > last_out):
            return f"they wrote to us last ({last_in:%d %b}), so a reply is owed, not a nudge"
        quiet = QUIET_DAYS.get(why, 2)
        if quiet and last_out and now - last_out < timedelta(days=quiet):
            return f"we emailed them on {last_out:%d %b}, under {quiet} days ago"
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
