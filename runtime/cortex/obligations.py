"""What Cortex owes, and what completes it (architecture audit, step 2d, 13 Oct 2026).

Cortex sets reminders for things it owes: send the proposal, keep a promise, meet a client's deadline, take a next
step, chase a payment. Before this, almost none of them knew what completes them. "Prepare and send the proposal"
fired after the proposal had gone (MENA Water, deal 155); client deadlines and next steps were never settled by the
email that delivered them; notifications never cleared when the thing they announced was resolved. Only closing the
deal cleaned anything up.

KINDS names each kind of reminder by its title and says how it completes. The three entry points are called by the
code where the thing happens:
    on_sent(deal_id, body, to, subject, attachment_names)   pipeline.record_send, every email that goes out on a deal
    on_stage(deal_id, old, new)                              crm.set_project_stage
    on_task_closed(task_id)                                  store.update_task, when a card leaves the open states
Only reminders Cortex created (created_by 'cortex%') are ever settled here: the owner's own are his.
"""
from __future__ import annotations

import re

from . import db

KINDS = [
    # prefix of the reminder title             how it completes
    {"key": "proposal_owed", "prefix": "Prepare and send the proposal",
     "done_on": "a quotation or proposal sent on the deal, or the deal leaving Opportunity"},
    {"key": "commitment", "prefix": "Commitment owed to",
     "done_on": "an email that actually delivers it (judged)"},
    {"key": "meeting_commitment", "prefix": "Meeting commitment",
     "done_on": "an email that actually delivers it (judged)"},
    {"key": "client_deadline", "prefix": "Client deadline",
     "done_on": "an email that actually delivers what they asked for (judged)"},
    {"key": "next_step", "prefix": "Next step on",
     "done_on": "an email that actually delivers it (judged)"},
    {"key": "conversation_commitment", "created_by": "cortex-commitment",
     "done_on": "an email that actually delivers it (judged)"},
    {"key": "tender_close", "prefix": "Tender closes",
     "done_on": "the deal moving to won work or Lost"},
]
JUDGED = ("commitment", "meeting_commitment", "client_deadline", "next_step", "conversation_commitment")
_QUOTE_RX = re.compile(r"quotation|proposal|\bSEN-\d{4}-\d{4}\b", re.I)


def _open(deal_id: int) -> list[dict]:
    """Cortex's reminders on the deal that are still owed: pending or snoozed, and FIRED ones too (a fired nudge is
    still owed until the thing is done; settling it clears the notice it raised)."""
    return db.query("select id, title, created_by from reminders where target_type='deal' and target_id=%s and "
                    "status in ('pending','snoozed','fired') and coalesce(created_by,'') like 'cortex%%'", (str(int(deal_id)),))


def kind_of(r: dict) -> str | None:
    for k in KINDS:
        if k.get("prefix") and (r.get("title") or "").startswith(k["prefix"]):
            return k["key"]
        if k.get("created_by") and r.get("created_by") == k["created_by"]:
            return k["key"]
    return None


def _done(deal_id: int, r: dict, why: str) -> None:
    from . import pipeline, reminders
    reminders.mark_done(int(r["id"]))
    db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and id = "
               "(select last_notification_id from reminders where id=%s)", (int(r["id"]),))   # its nudge is answered too
    pipeline.log_deal(int(deal_id), "commitment_done", f"{r['title'][:160]} ({why})")


def on_sent(deal_id: int, body: str, to: str, subject: str = "", attachment_names: list | None = None) -> list[int]:
    """An email went out on the deal. Deterministic first (a quotation or proposal settles 'send the proposal'),
    then one judgement over everything an email can deliver. Returns the reminder ids settled."""
    settled = []
    rows = _open(deal_id)
    if not rows:
        return settled
    names = " ".join(str(a) for a in (attachment_names or []))
    if _QUOTE_RX.search(names) or _QUOTE_RX.search(subject or ""):
        for r in rows:
            if kind_of(r) == "proposal_owed":
                _done(deal_id, r, "the quotation or proposal went out")
                settled.append(r["id"])
    judged = [r for r in rows if kind_of(r) in JUDGED and r["id"] not in settled]
    if judged and (body or "").strip():
        from . import provider
        listing = "\n".join(f"[{r['id']}] {r['title']}" for r in judged)
        try:
            out = provider.think_json(
                "An email was just sent on a deal. Below are things we OWE this client (promises we made, deadlines "
                "they set, next steps). Decide which THIS email genuinely completes: the promised or requested thing is "
                "actually delivered, attached or answered in it, not merely mentioned or promised again. Return "
                '{"done_ids": [<int>...]}. Conservative: when unclear, leave it open.',
                f"OWED:\n{listing}\n\nEMAIL JUST SENT (to {to}, subject {subject}, attachments: {names or 'none'}):\n"
                f"{(body or '')[:3000]}", model=provider.MODEL_ROUTER, purpose="obligation-settle")
            ids = {int(i) for i in (out or {}).get("done_ids", []) if str(i).isdigit()}
        except Exception:  # noqa: BLE001
            ids = set()
        for r in judged:
            if r["id"] in ids:
                _done(deal_id, r, "delivered in the email that went out")
                settled.append(r["id"])
    return settled


def on_reply_sent(deal_id: int) -> None:
    """We emailed them: notices that said a reply was owed on this deal are answered."""
    db.execute("update notifications set state='dismissed' where target_type='deal' and target_id=%s and "
               "state in ('unread','read','snoozed') and (title like 'Unanswered%%' or title like 'Not drafted%%' "
               "or title like '%%replied: no %%')", (str(int(deal_id)),))


def on_stage(deal_id: int, old: str | None, new: str) -> None:
    """The deal moved stage: what the stage itself completes is settled, and notices that asked about the stage
    (move it to Booked? to Lost? signed off?) are cleared, because the owner has answered them."""
    from . import stages
    for r in _open(deal_id):
        k = kind_of(r)
        if k == "proposal_owed" and new != "Opportunity":
            _done(deal_id, r, f"the deal moved to {new}")
        elif k == "tender_close" and (new in stages.WON or new == "Lost"):
            _done(deal_id, r, f"the deal moved to {new}")
    if old in stages.SALES and new not in stages.SALES:      # won, lost or parked: the personal touch track ends
        db.execute("update reminders set status='cancelled' where status in ('pending','snoozed') and "
                   "created_by='cortex-touch' and target_type='deal' and target_id=%s", (str(int(deal_id)),))
    if new != old:
        db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and "
                   "(dedup_key like %s or dedup_key like %s)", (f"closing-gate:{int(deal_id)}:%", f"signed-off:{int(deal_id)}"))


def on_task_closed(task_id: int) -> None:
    """A card left the open states (sent, done, cancelled, rejected): notices about that card have done their job."""
    db.execute("update notifications set state='dismissed' where target_type='task' and target_id=%s and "
               "state in ('unread','read','snoozed')", (str(int(task_id)),))


def on_contact_added(deal_id: int) -> None:
    """A deal got a contact: the 'no contact, follow-ups can't start' notices are answered."""
    db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and "
               "dedup_key in (%s, %s)", (f"nocontact:{int(deal_id)}", f"no-contact:{int(deal_id)}"))


def on_deal_for_contact(email: str) -> None:
    """An opportunity now exists for someone we emailed by hand: the 'untracked sales email' notice is answered."""
    db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and "
               "lower(dedup_key)=%s", (f"untracked-sales:{(email or '').strip().lower()}",))
