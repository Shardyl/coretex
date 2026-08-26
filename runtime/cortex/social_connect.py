"""LinkedIn connection-ACCEPTANCE monitor. After the persona sends an invite (recorded by
social_warm.record_connect: tag `invited`, stage Contacted), watch for the invitee ACCEPTING - i.e.
becoming a 1st-degree connection. The runner (accepts.py) reads each invited profile's connection degree
and reports back; here we mark accepted, advance the CRM stage, notify per acceptance, and expose a
success-rate report so the outreach can be analysed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Json

from . import db, notifications, social_config


def pending_accept_checks(account: str, limit: int = 40) -> list[dict]:
    """Contacts the persona has invited that haven't been resolved yet (not accepted/declined). The runner
    revisits each profile to see whether it's now a 1st-degree connection."""
    cfg = social_config.get_account(account) or {}
    company_id = cfg.get("company_id", 5)
    rows = db.query(
        "select id, first_name, last_name, linkedin from crm_master "
        "where tags @> '[\"invited\"]'::jsonb and not (tags @> '[\"accepted\"]'::jsonb) "
        "and not (tags @> '[\"invite-declined\"]'::jsonb) and linkedin is not null "
        "order by updated_at desc limit %s", (limit,))
    return [{"id": r["id"], "name": f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "there",
             "linkedin": r["linkedin"]} for r in rows]


def ingest_accepts(account: str, results: list[dict]) -> dict:
    """results = [{linkedin, connected: bool, degree: '1st'|'2nd'|...}]. For each newly-connected invitee:
    tag `accepted`, advance stage Contacted->Engaged, log history, and raise ONE rolling 'new connections'
    notification (grouped). Returns {accepted, checked}."""
    cfg = social_config.get_account(account) or {}
    company_id = cfg.get("company_id", 5)
    persona = cfg.get("persona", "Paul Anderson")
    accepted = 0
    for r in results or []:
        url = (r.get("linkedin") or "").strip()
        if not url or not r.get("connected"):
            continue
        row = db.one("select id, first_name, last_name, tags, stage from crm_master "
                     "where lower(linkedin)=lower(%s)", (url,))
        if not row or "accepted" in (row.get("tags") or []):
            continue
        name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or "Someone"
        ev = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "event": "linkedin_invite_accepted", "account": account}
        tags = sorted(set(row.get("tags") or []) | {"accepted"})
        new_stage = "Engaged" if (row.get("stage") or "Cold") in ("Cold", "Contacted") else row.get("stage")
        db.execute("update crm_master set tags=%s::jsonb, stage=%s, history = history || %s::jsonb, "
                   "updated_at=now() where id=%s", (Json(tags), new_stage, Json([ev]), row["id"]))
        accepted += 1
        stats = _stats(account, company_id)
        notifications.notify(
            f"{name} accepted {persona}'s connection request",
            f"New LinkedIn connection for {persona}. Accepted so far: {stats['accepted']} of "
            f"{stats['invited']} invites ({stats['rate']}%).",
            category="system", company_id=company_id,
            target_type="crm_contact", target_id=row["id"],
            dedup_key=f"li_accepts:{account}",
            item={"name": name, "linkedin": url, "at": ev["at"]})
    return {"accepted": accepted, "checked": len(results or [])}


def _stats(account: str, company_id: int) -> dict:
    """Success-rate counts from the CRM tags this account stamps."""
    def _n(where: str) -> int:
        return (db.one(f"select count(*) c from crm_master where tags @> '[\"anchor-harvest\"]'::jsonb "
                       f"and {where}") or {}).get("c", 0)
    invited = _n("tags @> '[\"invited\"]'::jsonb")
    accepted = _n("tags @> '[\"accepted\"]'::jsonb")
    declined = _n("tags @> '[\"invite-declined\"]'::jsonb")
    pending = max(0, invited - accepted - declined)
    rate = round(100.0 * accepted / invited) if invited else 0
    return {"invited": invited, "accepted": accepted, "declined": declined, "pending": pending, "rate": rate}


def connect_report(account: str) -> dict:
    """Post the outreach success-rate summary as a notification card, and return the numbers."""
    cfg = social_config.get_account(account) or {}
    company_id = cfg.get("company_id", 5)
    persona = cfg.get("persona", "Paul Anderson")
    s = _stats(account, company_id)
    notifications.notify(
        f"{persona} LinkedIn outreach: {s['accepted']}/{s['invited']} accepted ({s['rate']}%)",
        f"Invites sent: {s['invited']}. Accepted: {s['accepted']}. Still pending: {s['pending']}. "
        f"Accept rate: {s['rate']}%.",
        category="system", company_id=company_id, dedup_key=f"li_connect_report:{account}")
    return s
