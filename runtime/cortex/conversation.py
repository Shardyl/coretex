"""ONE CONVERSATION, ONE DRAFT, KEPT CURRENT (owner, 14 Oct 2026).

"When new emails come in while there's drafts sitting in Cortex, it should look at those drafts, at everything on the
opportunity, and redraft accordingly ... this is coming up time and time again." The DSO case: card 1219 (our
submission email to Alex) was waiting for approval when Alex replied on the same thread; his reply became TWO new cards
(1222, 1223), both queued behind 1219, both about to answer "looking forward to your proposal" after he had it.

A conversation is a PERSON and a THREAD, never just the opportunity: two contacts on two threads keep two drafts.

    absorb(company_id, request, deal_id)   store.create_card, for every inbound reply card: the same person's open draft
                                           on the same conversation (same thread, a reference to it, the same subject,
                                           or a new thread judged to be the same matter) becomes the reply to the new
                                           message, redrafted with his corrections kept. No second card.
    thread_key(request)                    the per-conversation queue key (person + subject), so two genuinely separate
                                           conversations with one person can both be live; a duplicate cannot.
    recheck_deal(company, deal_id, ...)    events.they_wrote: every OTHER open card on the opportunity (emails to other
                                           people, quotation and prep cards) is judged once against the new message:
                                           covered -> cancelled with the reason; affected -> redrafted (emails) or
                                           flagged (quotation/prep); unaffected -> untouched.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from . import db

OPEN = ("queued", "new", "drafting", "awaiting_approval", "awaiting_correction")
EMAIL_KINDS = ("email_reply", "email_draft")
_RE = re.compile(r"^\s*((re|fwd?|aw|sv)\s*:\s*)+", re.I)


def subject_base(s: str) -> str:
    return re.sub(r"\s+", " ", _RE.sub("", (s or "")).lower()).strip(" .-")


def thread_key(request: dict) -> str:
    """person|subject: one live card per person per conversation."""
    who = ((request.get("inquiry") or {}).get("email") or "").strip().lower()
    subj = subject_base((request.get("inquiry") or {}).get("subject") or "")
    return f"{who}|{subj}" if who and subj else who


def is_inbound(request: dict) -> bool:
    """A card made from a message they sent (not a chase, not a Talk-composed outbound)."""
    r = request or {}
    return bool((r.get("gmail_id") or r.get("mail_ref") or r.get("thread_reply")) and
                ((r.get("inquiry") or {}).get("message") or "").strip()) and not r.get("followup")


def same_thread(card_req: dict, new_req: dict) -> bool:
    """Code first: the same thread id, our card's message in their References, or the same subject."""
    a, b = card_req.get("thread") or {}, new_req.get("thread") or {}
    if a.get("id") and a.get("id") == b.get("id"):
        return True
    refs = f"{b.get('references') or ''} {b.get('msg_id') or ''}"
    if a.get("msg_id") and a["msg_id"] in refs:
        return True
    sa = subject_base((card_req.get("inquiry") or {}).get("subject") or "")
    sb = subject_base((new_req.get("inquiry") or {}).get("subject") or "")
    return bool(sa) and sa == sb


def _same_matter(card: dict, new_req: dict) -> bool:
    """A NEW thread from the same person: the same matter as the waiting draft, or a separate one? One narrow question."""
    from . import provider
    try:
        r = provider.think_json(
            "A client sent a new email on a NEW email thread while we have a draft reply to them waiting. Is the new "
            "email about the SAME matter as the waiting draft (so one email should answer both), or a SEPARATE matter "
            'that needs its own reply? Return {"same": true|false}.',
            "WAITING DRAFT (subject: " + str(((card.get("request") or {}).get("inquiry") or {}).get("subject") or "") + "):\n"
            + (card.get("draft") or (card.get("request") or {}).get("brief") or "")[:1500]
            + "\n\nNEW EMAIL (subject: " + str((new_req.get("inquiry") or {}).get("subject") or "") + "):\n"
            + str((new_req.get("inquiry") or {}).get("message") or "")[:1500],
            model=provider.MODEL_ROUTER, max_tokens=30, purpose="same-matter")
        return bool((r or {}).get("same"))
    except Exception:  # noqa: BLE001 - unsure: keep them separate (two drafts is recoverable, a lost reply is not)
        return False


def _when(new_req: dict) -> str:
    """The day the message was folded in (the request carries no sent time, so no clock time is claimed)."""
    return datetime.now(timezone.utc).strftime("%d %b")


def absorb(company_id, request: dict, deal_id=None) -> dict | None:
    """Fold a new inbound message into the same person's waiting draft on the same conversation. Returns that card
    (updated, sent back to drafting) or None when it should be a card of its own."""
    from . import store
    if not is_inbound(request):
        return None
    who = ((request.get("inquiry") or {}).get("email") or "").strip().lower()
    if not who:
        return None
    cards = db.query("select * from tasks where company_id=%s and kind = any(%s) and status = any(%s) and "
                     "lower(request->'inquiry'->>'email')=%s order by id desc", (company_id, list(EMAIL_KINDS), list(OPEN), who))
    msg = str((request.get("inquiry") or {}).get("message") or "")
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip()[:600]   # noqa: E731
    for c in cards:
        cr = c.get("request") or {}
        if norm((cr.get("inquiry") or {}).get("message")) == norm(msg) and msg.strip():
            return c                       # the same email again (another mailbox's copy): the card already has it
        if not (same_thread(cr, request) or _same_matter(c, request)):
            continue
        name = (request.get("inquiry") or {}).get("name") or who
        merged = dict(cr)
        # their newest message is what this email now answers; the card keeps WHO sends it (adoption re-runs and
        # pins the thread inside that mailbox, since thread ids are mailbox-local)
        for k in ("thread_cc", "gmail_id", "mail_ref", "inbound_attachments"):
            if request.get(k):
                merged[k] = request[k]
        # THREAD IDS ARE MAILBOX-LOCAL: the card keeps its own thread (it sends from its own mailbox) and takes only the
        # GLOBAL reply headers from their message, so the reply lands on the thread in every mailbox. A card with no
        # thread takes theirs only when it was read in the mailbox the card sends from (DSO 1219, 14 Oct 2026).
        nt, ot = dict(request.get("thread") or {}), dict(cr.get("thread") or {})
        same_box = (request.get("mailbox_rt") or "") == (cr.get("mailbox_rt") or "") and request.get("mailbox_rt")
        merged["thread"] = {"id": ot.get("id") or (nt.get("id") if same_box else ""),
                            "msg_id": nt.get("msg_id") or ot.get("msg_id") or "",
                            "references": nt.get("references") or ot.get("references") or ""}
        if not merged.get("from_email"):
            merged["from_email"], merged["mailbox_rt"] = request.get("from_email"), request.get("mailbox_rt")
        inq = dict(cr.get("inquiry") or {})
        inq["message"] = msg
        if (request.get("inquiry") or {}).get("subject"):
            inq["subject"] = request["inquiry"]["subject"]
        merged["inquiry"] = inq
        merged["thread_reply"] = True
        brief = re.split(r"\n\nTHEIR NEWEST MESSAGE \(", cr.get("brief") or "")[0]
        merged["brief"] = (brief + f"\n\nTHEIR NEWEST MESSAGE ({name}, {_when(request)}), which this email now answers on "
                           "the same thread. Keep everything this email was going to say that still applies, answer what "
                           f"they said, and drop anything it makes redundant:\n{msg[:2500]}")
        merged["updated_for"] = f"{name}'s reply, {_when(request)}"
        if deal_id and not merged.get("deal_id"):
            merged["deal_id"] = int(deal_id)
        status = "new" if c["status"] in ("awaiting_approval", "awaiting_correction", "new") else c["status"]
        store.update_task(c["id"], request=merged, status=status, draft=None if status == "new" else c.get("draft"))
        try:
            from . import pipeline
            if merged.get("deal_id"):
                pipeline.log_deal(int(merged["deal_id"]), "note", f"Card #{c['id']} now answers {name}'s newest message "
                                                                  "(same conversation): redrafted, no second card.")
        except Exception:  # noqa: BLE001
            pass
        print(f"[conversation] new message from {who} folded into card {c['id']}", flush=True)
        return store.get_task(c["id"])
    return None


_JUDGE = ("A client or contact on a sales opportunity just wrote (NEW MESSAGE). Another card on the same opportunity is "
          "waiting in the owner's inbox (CARD). Does the new message change that card? cancel = the card is now covered, "
          "answered or pointless; redraft = it should be rewritten to reflect the new message; keep = unaffected. "
          'Be conservative: keep unless the message clearly changes it. Return {"verdict": "keep"|"redraft"|"cancel", '
          '"why": "<one short line>"}.')


def recheck_deal(company: dict, deal_id, sender: str, message: str, background: bool = True) -> None:
    """Every OTHER open card on the opportunity, judged once against the new message (see module note)."""
    if not str(deal_id or "").isdigit() or not (message or "").strip():
        return

    def _run():
        from . import provider, store, pipeline, notifications
        rows = db.query("select * from tasks where deal_id=%s and status in ('awaiting_approval','awaiting_correction') "
                        "and (kind = any(%s) or kind='quotation' or request->>'prep_action' is not null) "
                        "and coalesce(lower(request->'inquiry'->>'email'),'') <> %s order by id desc limit 6",
                        (int(deal_id), list(EMAIL_KINDS), (sender or "").lower()))
        for c in rows:
            cr = c.get("request") or {}
            what = (f"{c['kind']} to {(cr.get('inquiry') or {}).get('email') or '-'}: " if c["kind"] in EMAIL_KINDS
                    else f"{c['kind']} {cr.get('number') or cr.get('title') or ''}: ") + (c.get("draft") or cr.get("brief") or "")[:1500]
            try:
                r = provider.think_json(_JUDGE, f"NEW MESSAGE from {sender}:\n{message[:2000]}\n\nCARD #{c['id']}: {what}",
                                        model=provider.MODEL_ROUTER, max_tokens=80, purpose="deal-recheck") or {}
            except Exception:  # noqa: BLE001
                continue
            v, why = r.get("verdict"), str(r.get("why") or "")[:200]
            if v == "cancel":
                store.update_task(c["id"], status="cancelled",
                                  request={**cr, "cancelled_reason": f"{sender} wrote and this is now covered: {why}"})
                pipeline.log_deal(int(deal_id), "note", f"Card #{c['id']} cancelled after {sender} wrote: {why}")
            elif v == "redraft" and c["kind"] in EMAIL_KINDS:
                brief = re.split(r"\n\nNEW ON THE OPPORTUNITY", cr.get("brief") or "")[0]
                store.update_task(c["id"], status="new", draft=None, request={
                    **cr, "brief": brief + f"\n\nNEW ON THE OPPORTUNITY: {sender} wrote ({why}). Rewrite whatever it "
                                           f"changes:\n{message[:1500]}", "updated_for": f"{sender}'s message ({why})"})
                pipeline.log_deal(int(deal_id), "note", f"Card #{c['id']} redrafted after {sender} wrote: {why}")
            elif v == "redraft":
                store.update_task(c["id"], request={**cr, "card_problem": f"{sender} has written since this was made: {why}. "
                                                                          "Check it still stands before approving."})
                notifications.notify(f"Card #{c['id']} may need changing", f"{sender} wrote: {why}", priority="high",
                                     category="approval", company_id=c.get("company_id"), target_type="task",
                                     target_id=str(c["id"]))
    if background:
        threading.Thread(target=_run, daemon=True).start()
    else:
        _run()
