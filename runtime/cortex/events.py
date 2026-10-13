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


def ask_which_deal(company: dict, message: dict, candidates: list[dict], direction: str = "in") -> None:
    """NEVER GUESS WHICH DEAL (owner, 13 Oct 2026, MAH Gold: Mai's video emails were filed on the SSD quotation deal
    because it was her newest open deal, and the quote check then read her video sign-off as acceptance of the quote).
    When the matcher cannot tell, ONE small card asks; the owner's answer is bound to the thread (thread_deal:<id>) so
    he is asked once per conversation. Until then the message is on the contact, not on a deal."""
    from . import store
    ref = str(message.get("mail_ref") or message.get("msg_id") or message.get("thread_id") or "")
    if ref and db.one("select 1 from tasks where request->>'kind'='deal_pick' and request->'message'->>'ref'=%s", (ref,)):
        return
    sk = store.get_skill_by_key(company["id"], "general-operations") or store.get_skill_by_key(company["id"], "email-handling")
    who = message.get("email") or message.get("to") or "them"
    lines = [f"{i}. #{d['id']} {d.get('title')} ({d.get('stage')})" for i, d in enumerate(candidates, 1)]
    draft = (f"Which deal is this about? Cortex could not tell from the thread, the subject or a quote number.\n\n"
             f"{'From' if direction == 'in' else 'To'} {who}: {(message.get('subject') or '(no subject)')[:120]}\n"
             f"{(message.get('body') or message.get('snippet') or '')[:400]}\n\n" + "\n".join(lines)
             + f"\n{len(lines) + 1}. None of these\n\nReply with the number. Your answer files this whole conversation.")
    store.create_task(company["id"], sk["id"], "content", {
        "kind": "deal_pick", "title": f"Which deal: {(message.get('subject') or who)[:70]}",
        "candidates": [{"id": int(d["id"]), "title": d.get("title"), "stage": d.get("stage")} for d in candidates],
        "direction": direction,
        "message": {"ref": ref, "email": message.get("email") or "", "to": message.get("to") or "",
                    "subject": message.get("subject") or "", "body": (message.get("body") or "")[:3000],
                    "thread_id": message.get("thread_id") or "", "msg_id": message.get("msg_id") or "",
                    "references": message.get("references") or "", "mail_ref": message.get("mail_ref") or ref,
                    "gmail_id": message.get("gmail_id") or message.get("id") or ""}})
    db.execute("update tasks set status='awaiting_approval', draft=%s where request->>'kind'='deal_pick' and "
               "request->'message'->>'ref'=%s and status='new'", (draft, ref))


def answer_which_deal(task: dict, text: str) -> str:
    """The owner answered a deal_pick card: bind the thread, file the message where it belongs, close the card."""
    import re as _re
    from . import store
    req = task.get("request") or {}
    cands = req.get("candidates") or []
    m = _re.search(r"\d+", text or "")
    n = int(m.group(0)) if m else 0
    pick = None
    if 1 <= n <= len(cands):
        pick = cands[n - 1]
    elif any(int(c["id"]) == n for c in cands):
        pick = next(c for c in cands if int(c["id"]) == n)
    msg = req.get("message") or {}
    if pick:
        for k in (msg.get("thread_id"), *(_re.findall(r"<([^<>\s]+)>", msg.get("references") or ""))):
            if k:
                db.setting_set(f"thread_deal:{k}", int(pick["id"]))
        co = store.get_company(task["company_id"])
        if req.get("direction") == "out":
            pipeline.log_deal(int(pick["id"]), "email_out_manual", f"manually sent to {msg.get('to')}: "
                              f"{msg.get('subject') or '(no subject)'}", ref=msg.get("ref") or "")
        else:
            they_wrote(co, {**msg, "email": msg.get("email")}, [pick])
        note = f"Filed on #{pick['id']} {pick.get('title')}; the rest of this conversation will follow."
    else:
        note = "Not filed on any deal (none of these)."
    store.update_task(task["id"], status="done", draft=(task.get("draft") or "") + "\n\nANSWER: " + note)
    return note


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
        if not deal and len(rows) > 1 and channel == "email":
            ask_which_deal(company, message, rows, "in")     # never guess: ask once, file it on his answer
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
