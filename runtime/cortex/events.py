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

import json
import re

from . import crm, db, pipeline, stages


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
    from . import engine
    if direction == "in" and engine._no_reply_mail(message):
        return      # A CALENDAR INVITE IS NOT A MESSAGE TO FILE (14 Oct 2026: Farheen's invite + update = two questions)
    ref = str(message.get("mail_ref") or message.get("msg_id") or message.get("thread_id") or "")
    if ref and db.one("select 1 from tasks where request->>'kind'='deal_pick' and (request->'message'->>'ref'=%s or "
                      "request->'messages' @> %s::jsonb)", (ref, json.dumps([{"ref": ref}]))):
        return
    who = (message.get("email") if direction == "in" else message.get("to")) or "them"
    msg = {"ref": ref, "email": message.get("email") or "", "to": message.get("to") or "",
           "subject": message.get("subject") or "", "body": (message.get("body") or "")[:3000],
           "thread_id": message.get("thread_id") or "", "msg_id": message.get("msg_id") or "",
           "references": message.get("references") or "", "mail_ref": message.get("mail_ref") or ref,
           "gmail_id": message.get("gmail_id") or message.get("id") or ""}
    # ONE QUESTION PER PERSON: while one is open, their next messages join it and one answer files them all
    open_q = db.one("select * from tasks where company_id=%s and request->>'kind'='deal_pick' and status in "
                    "('new','awaiting_approval','awaiting_correction') and request->>'direction'=%s and "
                    "lower(request->>'who')=lower(%s) order by id desc limit 1", (company["id"], direction, who))
    if open_q:
        rq = open_q.get("request") or {}
        msgs = (rq.get("messages") or [rq.get("message")]) + [msg]
        store.update_task(open_q["id"], request={**rq, "messages": [m for m in msgs if m]},
                          draft=_pick_text(direction, who, [m for m in msgs if m], rq.get("candidates") or []))
        return
    sk = store.get_skill_by_key(company["id"], "general-operations") or store.get_skill_by_key(company["id"], "email-handling")
    cands = [{"id": int(d["id"]), "title": d.get("title"), "stage": d.get("stage")} for d in candidates]
    store.create_task(company["id"], sk["id"], "content", {
        "kind": "deal_pick", "title": f"Which opportunity: {(message.get('subject') or who)[:70]}",
        "candidates": cands, "direction": direction, "who": who, "message": msg, "messages": [msg]})
    db.execute("update tasks set status='awaiting_approval', draft=%s where request->>'kind'='deal_pick' and "
               "request->'message'->>'ref'=%s and status='new'", (_pick_text(direction, who, [msg], cands), ref))


def _pick_text(direction: str, who: str, msgs: list, cands: list) -> str:
    """The card's words: who, which messages, the options (also the buttons), and how else to answer."""
    head = (f"Which opportunity are {'these messages' if len(msgs) > 1 else 'this message'} "
            f"{'from' if direction == 'in' else 'to'} {who} about? Cortex could not tell from the thread, the subject "
            "or a quote number.\n\n")
    body = "\n".join(f"- {(m.get('subject') or '(no subject)')[:120]}" for m in msgs)
    last = (msgs[-1].get("body") or "")[:400] if msgs else ""
    opts = "\n".join(f"{i}. #{c['id']} {c.get('title')} ({c.get('stage')})" for i, c in enumerate(cands, 1))
    return (head + body + (f"\n\n{last}" if last else "") + "\n\n" + opts + f"\n{len(cands) + 1}. None of these\n\n"
            "Tap one, or say which opportunity (its name or number), 'a new job: <name>', or 'none'.")


_NONE_RX = re.compile(r"\b(none|neither|not a deal|no deal|not an opportunity|no opportunity|personal|ignore it)\b", re.I)
_NEW_RX = re.compile(r"\bnew (job|opportunity|deal|project|enquiry)\b", re.I)


def _open_deals(company: dict) -> list[dict]:
    """Open opportunities and projects, with the client's name: he names the client ('the Merck summit'), and a
    deal's title often does not ('Abu Dhabi Future Health Summit')."""
    return db.query("select p.id, p.title, p.stage, a.name client, p.contact_email from crm_projects p left join "
                    "crm_accounts a on a.id=p.account_id where p.company=%s and p.stage not in "
                    + stages.sql_list(stages.NO_CORRESPONDENCE) + " order by p.id desc", (crm._org(company.get("slug")),))


def _resolve_pick(company: dict, req: dict, text: str):
    """His words -> ("deal", row) | ("none", None) | ("new", title) | ("ask", [rows]) | ("unknown", None).
    Code first (a number on the card, a deal number, 'none', 'new job'); otherwise ONE narrow model question
    matches his words against EVERY open opportunity of this company, and code keeps only real ids."""
    cands = req.get("candidates") or []
    t = (text or "").strip()
    deals = _open_deals(company)
    by_id = {int(d["id"]): d for d in deals}
    m = re.search(r"#?\b(\d{1,5})\b", t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= len(cands) and len(t) <= 12:
            return "deal", cands[n - 1]
        if n == len(cands) + 1 and len(t) <= 12:
            return "none", None
        if n in by_id:
            return "deal", by_id[n]
    if _NEW_RX.search(t):
        title = re.split(_NEW_RX, t, maxsplit=1)[-1]
        title = re.sub(r"^[\s:,\-]*(called|named|for)?\s*", "", title, flags=re.I).strip(" .") \
            if isinstance(title, str) else ""
        return "new", title
    if _NONE_RX.search(t) and len(t.split()) <= 6:
        return "none", None
    if not deals:
        return "unknown", None
    from . import provider
    listing = "\n".join(f"#{d['id']} {d['title']} ({d['stage']})"
                        + (f" [client: {d['client']}]" if d.get("client") else "")
                        + (f" [contact: {d['contact_email']}]" if d.get("contact_email") else "") for d in deals[:150])
    try:
        r = provider.think_json(
            "The owner is saying which of his open opportunities an email belongs to. Return the id(s) of every "
            "opportunity his words could mean. Only ids from the list. If nothing fits, return an empty list. "
            'Return {"ids": [<id>, ...]}.',
            f"HIS WORDS: {t[:500]}\n\nOPEN OPPORTUNITIES:\n{listing}",
            model=provider.MODEL_ROUTER, max_tokens=80, purpose="deal-pick-words") or {}
    except Exception:  # noqa: BLE001
        r = {}
    ids = [int(i) for i in (r.get("ids") or []) if str(i).isdigit() and int(i) in by_id]
    if len(ids) == 1:
        return "deal", by_id[ids[0]]
    if len(ids) > 1:
        return "ask", [by_id[i] for i in ids[:6]]
    return "unknown", None


def answer_which_deal(task: dict, text: str = "", pick_id: int | None = None) -> str:
    """The owner answered a deal_pick card, by a button (pick_id; 0 = none of these) or in his own words: bind the
    thread, file EVERY message on the card where it belongs, close the card. Words that match several
    opportunities re-ask with only those; words that match nothing keep the card open and say so."""
    from . import store
    req = task.get("request") or {}
    co = store.get_company(task["company_id"])
    msgs = [m for m in (req.get("messages") or [req.get("message")]) if m]
    if pick_id is not None:
        if int(pick_id) == 0:
            how, val = "none", None
        else:
            row = db.one("select id, title, stage from crm_projects where id=%s", (int(pick_id),))
            how, val = ("deal", row) if row else ("unknown", None)
    else:
        how, val = _resolve_pick(co, req, text)
    if how == "ask":
        cands = [{"id": int(d["id"]), "title": d["title"], "stage": d["stage"]} for d in val]
        store.update_task(task["id"], status="awaiting_approval",
                          request={**req, "candidates": cands, "card_problem": None},
                          draft=_pick_text(req.get("direction") or "in", req.get("who") or "", msgs, cands))
        return "Your words fit more than one opportunity: tap the right one."
    if how == "unknown":
        store.update_task(task["id"], request={**req, "card_problem": f"Could not match \"{(text or '')[:80]}\" to an "
                                               "open opportunity. Tap one, give its number, or say 'a new job: <name>'."})
        return "No opportunity matched those words: tap one, or give its number."
    if how == "new":
        sender = (msgs[0].get("email") if req.get("direction") != "out" else msgs[0].get("to")) if msgs else ""
        c = db.one("select first_name, last_name from crm_master where lower(email)=lower(%s)", (sender or "",)) or {}
        name = f"{c.get('first_name') or ''} {c.get('last_name') or ''}".strip()
        title = (val or (msgs[0].get("subject") if msgs else "") or "New opportunity")[:120]
        try:
            d = crm.create_deal(co["slug"], title, second_job=True, contact={"name": name, "email": sender})
        except Exception as e:  # noqa: BLE001
            store.update_task(task["id"], request={**req, "card_problem": f"Could not create the opportunity: {e}"})
            return f"Could not create the opportunity: {e}"
        how, val = "deal", {"id": d["id"], "title": d.get("title") or title, "stage": d.get("stage") or "Opportunity"}
    if how == "deal":
        pick = val
        for msg in msgs:
            for k in (msg.get("thread_id"), *(re.findall(r"<([^<>\s]+)>", msg.get("references") or ""))):
                if k:
                    db.setting_set(f"thread_deal:{k}", int(pick["id"]))
            if req.get("direction") == "out":
                pipeline.log_deal(int(pick["id"]), "email_out_manual", f"manually sent to {msg.get('to')}: "
                                  f"{msg.get('subject') or '(no subject)'}", ref=msg.get("ref") or "")
            else:
                they_wrote(co, {**msg, "email": msg.get("email")}, [pick])
        note = f"Filed on #{pick['id']} {pick.get('title')}; the rest of this conversation will follow."
    else:
        note = "Not filed on any opportunity (none of these)."
    store.update_task(task["id"], status="done", request={**req, "card_problem": None},
                      draft=(task.get("draft") or "") + "\n\nANSWER: " + note)
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
    if deal:
        try:   # WHO'S WHO (cast.py, 17 Oct 2026): "I'm taking over this project" must move the lead, by reading their words
            from . import cast
            from . import gmail as _gm
            _new = _gm.new_part(body) if channel == "email" else body
            cast.read_async(int(deal["id"]),
                            f"From: {message.get('name') or ''} <{sender or message.get('phone') or ''}>\n"
                            f"To: {message.get('to') or ''}\nCc: {message.get('cc') or ''}\n"
                            f"Subject: {message.get('subject') or ''}\n\n{_new[:6000]}",
                            kind="email", source=f"their {channel}: {(message.get('subject') or '')[:60]}",
                            people={sender: message.get("name") or ""} if sender else None)
        except Exception as e:  # noqa: BLE001
            print(f"[events] cast {type(e).__name__}: {e}", flush=True)
    if deal and channel != "email":     # email re-plans in pipeline.record_inbound; WhatsApp here (nextsteps.py)
        try:
            from . import nextsteps
            nextsteps.observe_async(int(deal["id"]), "whatsapp_in", f"From: {message.get('name') or sender or ''}\n\n{body[:6000]}")
        except Exception as e:  # noqa: BLE001
            print(f"[events] nextsteps {type(e).__name__}: {e}", flush=True)
    if deal:
        try:   # ONE CONVERSATION, ONE DRAFT: every OTHER open card on the opportunity is judged against this message
            from . import conversation
            conversation.recheck_deal(company, deal["id"], sender or who, body)
        except Exception as e:  # noqa: BLE001
            print(f"[events] recheck {type(e).__name__}: {e}", flush=True)
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
