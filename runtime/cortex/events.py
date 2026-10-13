"""ONE handler for "they wrote to us" (architecture audit, step 2c, 13 Oct 2026).

What must happen when a client or lead writes in was done in one place only, `engine._draft_direct_reply`: set the
closing rhythm, pause or reschedule the chase clock (reading any timeframe they state), pull our scheduled emails off
the clock, and log the message on the deal with its deadlines. Every other way a message arrives skipped all of it:
a client email the no-draft policy held, a reply under 40 characters ("Thanks, will review"), a reply read from the
sales mailbox, a WhatsApp from a deal client, and a colleague matched only by email domain. The chase then went out
as if they had never written; the only backstop was a thread check on one mailbox at chase time.

`they_wrote()` is called by every inbound path. It never drafts and never decides whether to reply: that stays with
each channel. It only makes the deal know they spoke.
"""
from __future__ import annotations

from datetime import datetime

from . import crm, db, pipeline


def _deals(company: dict, email: str, deals: list | None) -> list[dict]:
    """Full deal rows: the ones passed in, else the sender's own, else a colleague's at the same client."""
    ids = [int(d["id"]) for d in (deals or []) if d and str(d.get("id") or "").isdigit()]
    if not ids and email:
        slug = company.get("slug")
        ids = [int(d["id"]) for d in crm.active_deals_for_email(email, slug)]
        if not ids:
            d = crm.open_deal_for_domain(email, slug)
            ids = [int(d["id"])] if d else []
    return db.query("select * from crm_projects where id = any(%s) order by id desc", (ids,)) if ids else []


def they_wrote(company: dict, message: dict, deals: list | None = None, channel: str = "email",
               log: bool = True) -> dict | None:
    """They wrote to us. `message` is the email dict (email, subject, body, msg ids) or, for WhatsApp,
    {"phone", "name", "body"}. Returns the deal the message belongs to (or None)."""
    from . import engine          # local: engine imports this module
    from . import gmail
    sender = (message.get("email") or "").strip().lower()
    who = sender or message.get("phone") or message.get("name") or "they"
    body = (message.get("body") or message.get("snippet") or "").strip()
    rows = _deals(company, sender, deals)
    deal = None
    if rows:
        deal = rows[0] if len(rows) == 1 else (engine._deal_for_thread(message, rows) if channel == "email" else rows[0])
    try:
        if deal:
            crm.enter_closing(int(deal["id"]))      # set the rhythm first: a stated date below re-arms within it
        ref = gmail.mail_ref(message) if channel == "email" else str(message.get("ref") or "")
        engine._pause_or_reschedule_followups(company, rows, who, body, ref=ref)
        engine._hold_scheduled_emails(company["id"], sender, [r["id"] for r in rows],
                                      f"{who} wrote to us at {datetime.now(engine._GST):%H:%M}"
                                      + (f" ({(message.get('subject') or '')[:60]})" if message.get("subject") else
                                         f" on {channel}"))
    except Exception as e:  # noqa: BLE001 - the message itself must never be lost over bookkeeping
        print(f"[events] {type(e).__name__}: {e}", flush=True)
    if log and deal:
        try:
            if channel == "email":
                pipeline.record_inbound(message, deal, company)     # timeline + deadlines, once per Message-Id
            else:
                pipeline.log_deal(int(deal["id"]), f"{channel}_in", f"{channel.capitalize()} from {who}: {body[:400]}",
                                  ref=str(message.get("ref") or ""))
        except Exception as e:  # noqa: BLE001
            print(f"[events] log {type(e).__name__}: {e}", flush=True)
    return deal
