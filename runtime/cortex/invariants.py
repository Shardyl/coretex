"""Things that must never be true. Checked every night against the live database (owner, 13 Oct 2026: "we need a
system that fixes these problems", after a week of the same kinds of fault surfacing one at a time: a chase after a
deal was Lost, a reminder for a proposal already sent, one person chased by two systems).

Each check is a query that should return NO rows. A check with a `fix` repairs itself when the repair is safe and
fully reversible (cancel an automatic card or reminder, stop a clock, dismiss a notice); every repair is logged.
Everything else is REPORTED, never guessed at. One notification per run lists what was found.

Add a check whenever a new kind of fault is found: the query that would have caught it, before the code fix ships.

    python -m cortex.invariants          # report
    python -m cortex.invariants --fix    # report and apply the safe repairs
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from . import db

OPEN = "('new','drafting','queued','awaiting_approval','awaiting_correction')"
NO_CHASE = "('Lost','Completed')"          # Dormant deliberately absent: dormant deals get soft revivals
CLOSED = "('Lost','Dormant','Completed')"
AUTO_CARD = ("(coalesce(t.request->>'followup','')<>'' or t.request->>'lead_chase'='true' "
             "or t.request->>'system_note' like 'From reminder #%%' "
             "or t.request->>'system_note' like 'From commitment reminder%%')")

CHECKS = [
    {
        "key": "auto_card_on_closed_deal",
        "what": "An unsent card Cortex raised by itself (chase, check-in, reminder email) on a closed deal",
        "sql": f"select t.id, t.deal_id, p.stage from tasks t join crm_projects p on p.id=t.deal_id "
               f"where t.status in {OPEN} and {AUTO_CARD} and (p.stage in {NO_CHASE} or "
               f"(p.stage='Dormant' and coalesce(t.request->>'followup','')<>'revive'))",
        "fix": f"update tasks t set status='cancelled', updated_at=now(), request = request || "
               f"'{{\"cancelled_reason\": \"invariant: the deal is closed\"}}'::jsonb from crm_projects p "
               f"where p.id=t.deal_id and t.status in {OPEN} and {AUTO_CARD} and (p.stage in {NO_CHASE} or "
               f"(p.stage='Dormant' and coalesce(t.request->>'followup','')<>'revive')) returning t.id",
    },
    {
        "key": "cortex_reminder_on_closed_deal",
        "what": "A reminder Cortex set is still pending on a closed deal",
        "sql": f"select r.id, r.title, p.stage from reminders r join crm_projects p on r.target_type='deal' "
               f"and p.id::text=r.target_id where r.status in ('pending','snoozed') and "
               f"coalesce(r.created_by,'') like 'cortex%%' and p.stage in {CLOSED}",
        "fix": f"update reminders r set status='cancelled' from crm_projects p where r.target_type='deal' and "
               f"p.id::text=r.target_id and r.status in ('pending','snoozed') and "
               f"coalesce(r.created_by,'') like 'cortex%%' and p.stage in {CLOSED} returning r.id",
    },
    {
        "key": "chase_clock_on_lost_deal",
        "what": "A Lost or Completed deal still has a follow-up date",
        "sql": f"select id, title, stage, next_followup from crm_projects where stage in {NO_CHASE} "
               f"and next_followup is not null",
        "fix": f"update crm_projects set next_followup=null, updated_at=now() where stage in {NO_CHASE} "
               f"and next_followup is not null returning id",
    },
    {
        "key": "proposal_reminder_after_quote",
        "what": "'Prepare and send the proposal' still pending after a quotation or proposal went out",
        "sql": "select r.id, r.title, p.stage from reminders r join crm_projects p on r.target_type='deal' and "
               "p.id::text=r.target_id where r.status in ('pending','snoozed') and r.title like "
               "'Prepare and send the proposal%%' and (p.stage <> 'Opportunity' or exists (select 1 from "
               "jsonb_array_elements(p.history) e where e->>'event' = 'quotation_sent'))",
        "fix": "update reminders r set status='done' from crm_projects p where r.target_type='deal' and "
               "p.id::text=r.target_id and r.status in ('pending','snoozed') and r.title like "
               "'Prepare and send the proposal%%' and (p.stage <> 'Opportunity' or exists (select 1 from "
               "jsonb_array_elements(p.history) e where e->>'event' = 'quotation_sent')) returning r.id",
    },
    {
        "key": "notice_on_closed_deal",
        "what": "A notification still showing for a closed deal",
        "sql": f"select n.id, left(n.title,80), p.stage from notifications n join crm_projects p on "
               f"n.target_type='deal' and n.target_id=p.id::text where n.state in ('unread','read','snoozed') "
               f"and p.stage in {NO_CHASE}",
        "fix": f"update notifications n set state='dismissed' from crm_projects p where n.target_type='deal' and "
               f"n.target_id=p.id::text and n.state in ('unread','read','snoozed') and p.stage in {NO_CHASE} "
               f"returning n.id",
    },
    {
        "key": "double_chase",
        "what": "One person has more than one open automatic chase",
        "sql": f"select lower(request->'inquiry'->>'email') who, count(*), array_agg(id) from tasks "
               f"where status in {OPEN} and (coalesce(request->>'followup','') in ('chase','checkin','followup',"
               f"'revive') or request->>'lead_chase'='true') group by 1 having count(*)>1",
    },
    {
        "key": "retired_silence_chase",
        "what": "Keys of the retired silence-chase list exist again (something is still arming it)",
        "sql": "select key from settings where key like 'lead_fu:%%'",
        "fix": "delete from settings where key like 'lead_fu:%%' returning key",
    },
    {
        "key": "card_deal_company_mismatch",
        "what": "A card filed under one company points at another company's deal",
        "sql": "select t.id, c.name, p.company, p.id from tasks t join crm_projects p on p.id=t.deal_id join "
               "companies c on c.id=t.company_id where t.status in " + OPEN + " and p.company <> c.name and "
               "not (c.name='Sensa Productions' and p.company='Sensa')",
    },
    {
        "key": "email_to_departed_or_unqualified",
        "what": "An open email card is addressed to someone who has left their company or is marked not qualified",
        "sql": f"select t.id, t.request->'inquiry'->>'email', m.lead_status, m.classification from tasks t join "
               f"crm_master m on lower(m.email)=lower(t.request->'inquiry'->>'email') where t.status in {OPEN} "
               f"and t.kind in ('email_reply','email_draft') and (m.lead_status='left-company' or "
               f"m.classification='not_qualified')",
    },
    {
        "key": "stalled_sales_deal",
        "what": "An open sales deal on auto with no follow-up date, no open card, and our last email over 14 days ago",
        "sql": f"select p.id, p.title from crm_projects p where p.automation='auto' and p.next_followup is null "
               f"and p.stage in ('Opportunity','Quote') and not exists (select 1 from tasks t where t.deal_id=p.id "
               f"and t.status in {OPEN}) and (select max(e->>'ts') from jsonb_array_elements(p.history) e where "
               f"e->>'event' like 'email_out%%') < (now()-interval '14 days')::text and not exists (select 1 from "
               f"jsonb_array_elements(p.history) e where e->>'event'='chase_stopped')",
    },
    {
        "key": "opportunity_from_stale_enquiry",
        "what": "An open opportunity opened in the last 14 days from an enquiry over 30 days old",
        "sql": "select p.id, p.title from crm_projects p where p.created_at > now()-interval '14 days' and "
               "p.stage not in ('Lost','Completed','Dormant') and coalesce(p.contact_email,'')<>'' and (select "
               "min(h->>'ts') from crm_master m, jsonb_array_elements(m.history) h where lower(m.email)="
               "lower(p.contact_email)) < (p.created_at - interval '30 days')::text",
    },
    {
        "key": "email_card_waiting_a_week",
        "what": "An email card has waited for approval for over 7 days",
        "sql": "select id, kind, company_id, created_at::date from tasks where status='awaiting_approval' and "
               "kind in ('email_reply','email_draft') and created_at < now()-interval '7 days'",
    },
    {
        "key": "same_email_two_open_cards",
        "what": "The same inbound email has two open cards",
        "sql": f"select request->>'mail_ref', count(*), array_agg(id) from tasks where "
               f"coalesce(request->>'mail_ref','')<>'' and status in {OPEN} group by 1 having count(*)>1",
    },
    {
        "key": "reminder_fired_after_close",
        "what": "In the last 2 days a Cortex reminder fired on a deal that was already closed",
        "sql": "select r.id, r.title from reminders r join crm_projects p on r.target_type='deal' and "
               "p.id::text=r.target_id where r.status='fired' and coalesce(r.created_by,'') like 'cortex%%' and "
               "r.due_at > now()-interval '2 days' and exists (select 1 from jsonb_array_elements(p.history) e "
               "where e->>'event'='stage_change' and e->>'text' ~ '-> (Lost|Completed|Dormant)$' and "
               "(e->>'ts')::timestamptz < r.due_at)",
    },
]


def run(fix: bool = False) -> dict:
    """Run every check. Returns {checked_at, findings: {key: {what, rows, fixed}}}; saves it in setting
    `invariants:last` and raises ONE notification when anything is left for a person to look at."""
    out = {"checked_at": datetime.now(timezone.utc).isoformat(), "findings": {}}
    for c in CHECKS:
        try:
            rows = db.query(c["sql"])
        except Exception as e:  # noqa: BLE001 - one broken check never stops the others
            out["findings"][c["key"]] = {"what": c["what"], "error": f"{type(e).__name__}: {str(e)[:200]}"}
            continue
        if not rows:
            continue
        f = {"what": c["what"], "rows": [{k: (str(v) if v is not None else None) for k, v in r.items()}
                                         for r in rows[:25]], "count": len(rows), "fixed": 0}
        if fix and c.get("fix"):
            try:
                f["fixed"] = len(db.query(c["fix"]) or [])
            except Exception as e:  # noqa: BLE001
                f["fix_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["findings"][c["key"]] = f
    db.setting_set("invariants:last", out)
    left = {k: v for k, v in out["findings"].items() if v.get("error") or v.get("count", 0) > v.get("fixed", 0)}
    fixed = {k: v for k, v in out["findings"].items() if v.get("fixed")}
    if left or fixed:
        try:
            from . import notifications
            body = []
            if fixed:
                body.append("Fixed automatically: " + "; ".join(f"{v['what']} ({v['fixed']})" for v in fixed.values()))
            if left:
                body.append("Needs a look: " + "; ".join(
                    f"{v['what']} ({v.get('count', 0) - v.get('fixed', 0) if not v.get('error') else 'check failed'})"
                    for v in left.values()))
            notifications.notify(
                f"Health check: {len(left)} to look at" if left else f"Health check: {sum(v['fixed'] for v in fixed.values())} fixed",
                "\n".join(body), priority="normal" if not left else "high", category="system",
                dedup_key=f"invariants:{out['checked_at'][:10]}")
        except Exception:  # noqa: BLE001
            pass
    return out


_last = 0.0


def maybe_run() -> None:
    """Engine-loop hook: once a day after 01:00 UTC (05:00 Dubai), with the safe repairs on."""
    global _last
    if time.time() - _last < 600:
        return
    _last = time.time()
    now = datetime.now(timezone.utc)
    if now.hour < 1:
        return
    done = (db.setting_get("invariants:last") or {}).get("checked_at") or ""
    if done[:10] == now.date().isoformat():
        return
    print(f"[invariants] {sum(1 for _ in run(fix=True)['findings'])} finding(s)", flush=True)


if __name__ == "__main__":
    res = run(fix="--fix" in sys.argv)
    for k, v in res["findings"].items():
        print(f"- {k}: {v.get('count', 0)} found, {v.get('fixed', 0)} fixed {v.get('error') or ''}")
        for r in v.get("rows", [])[:5]:
            print("    ", r)
    if not res["findings"]:
        print("all clear")
