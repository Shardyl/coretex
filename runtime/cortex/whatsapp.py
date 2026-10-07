"""WhatsApp inbound: a message arrives, Cortex classifies it, captures the contact, drafts a reply IN
RASHAD'S VOICE and lands it in the Inbox for approval. Approving is what sends.

TWO TRANSPORTS, one brain. Both funnel into `_process_message`, so triage, CRM capture and drafting behave
identically whichever way a message reached us:
  • CLOUD API (the real one, Meta's WhatsApp Business Platform) — Meta POSTs to /api/whatsapp/webhook and
    approval sends via `send_text`. Requires the WABA env keys below.
  • RUNNER (WhatsApp Web on an office box, driven by Patchright) — kept as a fallback transport. The runner
    pushes chats to /api/whatsapp/inbox and types approved replies back itself. NOTE: automating WhatsApp Web
    is against WhatsApp's terms and got a fresh number banned within hours on 19 Aug 2026, so the Cloud API
    is the supported path and this is here only because it exists and works.

Deliberately the same shape as social_dm.py (LinkedIn) — ingest -> dedupe -> classify -> draft -> card — with
two WhatsApp-specific differences:
  • the identity is a PHONE NUMBER, not an email, so CRM capture goes through crm.match_or_add_by_phone
    (crm_master is email-keyed and add_inbound_contact hard-refuses an email-less contact).
  • approval actually SENDS, so the card's kind is 'wa_reply' — outward, never auto, biometric step-up.

VOICE: WhatsApp is a personal channel — people message it precisely because it is NOT a formal business
inbox. The tone lives in the skill craft/rules (social-dm-replies), never here; this module only says
"it's WhatsApp, keep it human" in the brief and lets the editable skill do the rest.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, crm, db, provider, social_dm, store, worker

# Which WhatsApp account routes to which company. Overridable live via the 'wa_routing' setting so a new
# number/company never needs a deploy.
_DEFAULT_ROUTING = {"sensa-uk": {"company_id": 3, "skill_key": "social-dm-replies", "author": "rashad"}}

GRAPH = "https://graph.facebook.com/v23.0"

# People type a WhatsApp enquiry the way they talk: three lines in fifteen seconds. Treated as three
# messages that is three cards, three drafts and three alerts for one enquiry (cards 1072-1074,
# 6 Oct 2026). So a follow-up within BURST_WINDOW_SEC joins the open card instead of starting a new one,
# and the owner's alert waits ALERT_QUIET_SEC after the LAST message so a burst rings his phone once.
BURST_WINDOW_SEC = 900
ALERT_QUIET_SEC = 40

# Only a card that is still just a draft absorbs a follow-up. One in 'awaiting_correction' is mid-edit:
# redrafting it from under him would throw his instruction away, so that starts a fresh card.
_FOLDABLE = ("new", "drafting", "awaiting_approval")

# How many waiting cards one reply from him pulls through at once. A backlog is a backlog, not a reason
# to send him eight WhatsApps in a row.
ALERT_BATCH = 3


def routing(account: str) -> dict:
    cfg = db.setting_get("wa_routing") or {}
    return {**_DEFAULT_ROUTING, **cfg}.get(account) or _DEFAULT_ROUTING["sensa-uk"]


def _key(account: str, phone: str, msg: str) -> str:
    return hashlib.sha1(f"{account}|{phone}|{msg}".encode("utf-8", "replace")).hexdigest()[:16]


def _classify(name: str, phone: str, msg: str, slug: str) -> dict:
    """WhatsApp triage. People who message a business WhatsApp are overwhelmingly real, so the bar to reply is
    LOW — only obvious spam/scam is filed. Also flags 'personal' (an actual friend/acquaintance, common on a
    number that used to be Rashad's personal line) so the draft doesn't answer a mate like a lead."""
    try:
        out = provider.think_json(
            "You triage inbound WHATSAPP messages for a Dubai production company. WhatsApp is a personal, "
            "informal channel. Almost every real human deserves a reply. Only decline for obvious spam, "
            "scams, crypto, or automated blasts. Distinguish a genuine business ENQUIRY from a PERSONAL "
            "message (a friend, family, or someone who knows the owner personally) — this number was once a "
            "personal line, so old contacts still message it.",
            f"From: {name or 'unknown'} ({phone})\nTheir message: {msg}\n\n"
            'Return JSON: {"reply": boolean, "category": "enquiry|personal|supplier|spam", '
            '"name": "their personal name ONLY if they actually state it or sign off with it, '
            'else empty string - never guess, never use the phone number", '
            '"summary": "one line, who and what", "reason": "short"}',
            model=provider.MODEL_ROUTER, purpose="wa_triage", company=slug)
        return {"reply": bool(out.get("reply")), "category": (out.get("category") or "enquiry"),
                "name": (out.get("name") or "").strip(),
                "summary": (out.get("summary") or "").strip(), "reason": (out.get("reason") or "").strip()}
    except Exception:  # noqa: BLE001 — triage must never block an inbound message
        return {"reply": True, "category": "enquiry", "name": "", "summary": "",
                "reason": "triage unavailable -> default reply"}


def _clean_phone(p: str) -> str:
    p = re.sub(r"[^\d+]", "", p or "")
    return ("+" + p.lstrip("+")) if p else ""


def _brief(verdict: dict, know_name: str, phone: str, msg: str, src: dict | None) -> str:
    """The FACTS of this enquiry, as the drafting brief. Channel voice and personal/unknown-number
    behaviour live in the social-dm-replies skill RULES (worker.draft serves them), never here.

    Shared by the first message and by every follow-up folded in after it, so a burst redrafts against
    exactly the same framing it would have had if they had typed it all in one go."""
    personal = verdict.get("category") == "personal"
    line = (src or {}).get("line")
    return (
        "Draft a reply to this WhatsApp message (the skill's standing rules govern the voice and shape). "
        + ("FACT: this is a PERSONAL message from someone who knows the owner, not a business lead.\n\n"
           if personal else f"FACT: triaged as a '{verdict.get('category')}' message.\n\n")
        + ("FACT: the sender's name is unknown, only their number.\n\n" if not know_name else "")
        + (f"FACT: they came from our Google Ads landing page ({line}). The '(ref ...)' in their message "
           "is our tracking code: never mention it.\n\n" if line else "")
        + ("FACT: these are consecutive messages from the same person, moments apart. One enquiry, not "
           "several: answer the whole thing once.\n\n" if "\n" in msg.strip() else "")
        + f"From: {know_name or phone}\nTheir message: {msg}")


def _open_card(account: str, phone: str) -> dict | None:
    """The still-unsent card for this person on this number, if a recent one exists."""
    return db.one(
        "select * from tasks where kind='wa_reply' and status = any(%s) and request->>'phone' = %s "
        "and request->>'account' = %s and updated_at > now() - make_interval(secs => %s) "
        "order by id desc limit 1",
        (list(_FOLDABLE), phone, account, BURST_WINDOW_SEC))


def _arm_alert(req: dict) -> dict:
    """Mark a card as owing the owner an alert, ALERT_QUIET_SEC from now. Returns the request to save."""
    req["alert_pending"] = True
    req["alert_due"] = time.time() + ALERT_QUIET_SEC
    return req


def _fold_in(card: dict, co: dict, skill: dict, rt: dict, msg: str, src: dict | None) -> str:
    """Add a follow-up message to an open card: redraft against the whole conversation so far and push
    the alert back, so the burst rings once with the full picture rather than three times in pieces."""
    req = dict(card.get("request") or {})
    msgs = [m for m in (req.get("messages") or [req.get("their_message") or ""]) if m] + [msg]
    joined = "\n".join(msgs)
    brief = _brief(req.get("triage") or {}, req.get("recipient") or "", req.get("phone") or "", joined,
                   req.get("lead_source") or src)
    try:
        draft = worker.draft(skill, co, {"brief": brief}, author=rt.get("author") or "rashad")
    except Exception:  # noqa: BLE001 - keep the draft we already had rather than losing the card
        draft = card.get("draft") or ""
    req.update({"brief": brief, "their_message": joined, "messages": msgs})
    if src and not req.get("lead_source"):
        req["lead_source"] = src
    store.update_task(card["id"], request=_arm_alert(req),
                      **({"draft": draft, "status": "awaiting_approval"} if draft else {}))
    print(f"[whatsapp] folded a follow-up into card {card['id']} ({len(msgs)} messages)", flush=True)
    return "folded"


# ---- the shared brain: one inbound message -> CRM capture + an approval card --------------------------

def _process_message(rt: dict, co: dict, skill: dict, slug: str, account: str,
                     phone: str, name: str, msg: str, chat_id: str = "") -> str:
    """Triage one inbound message, capture the contact, draft a reply card. Returns drafted | skipped.
    Shared by BOTH transports so the Cloud API and the runner can never drift apart in behaviour."""
    src = None
    try:   # a landing-page reference in the pre-filled text names the ad click behind this lead (lptrack)
        from . import lptrack
        src = lptrack.from_message(msg)
    except Exception:  # noqa: BLE001
        src = None
    verdict = _classify(name, phone, msg, slug)
    # CRM capture happens for every real human (even ones we don't draft for) so the contact is never lost.
    if verdict.get("category") != "spam":
        try:
            # WhatsApp never gives us a name for an unknown sender. The only honest source is the message
            # itself, when they introduce themselves. Fill-if-blank, so a later message where they DO say who
            # they are backfills the contact.
            crm.match_or_add_by_phone(
                phone, verdict.get("name") or name, slug,
                source=("whatsapp, PPC landing page: " + src["line"])[:200] if src else "whatsapp",
                summary=verdict.get("summary") or msg[:200], classification=verdict.get("category"))
        except Exception:  # noqa: BLE001 — a CRM hiccup must not lose the reply
            pass
    if not verdict.get("reply"):
        # Traffic on this number is very low (Rashad, 22 Aug 2026), so nothing arrives silently: a message we
        # decline to answer still raises an FYI card, with the reason, so he can see it was a real decision
        # and override it. No draft, no approval, nothing armed to send.
        try:
            from . import notifications
            notifications.notify(
                f"WhatsApp filed, no reply drafted ({verdict.get('category') or 'spam'})",
                f"{name or phone}: {msg[:180]}" + (f"  [{verdict.get('reason')}]" if verdict.get("reason") else ""),
                priority="fyi", category="social", company_id=rt["company_id"],
                item={"name": name or phone, "phone": phone, "cat": verdict.get("category") or "spam"})
        except Exception:  # noqa: BLE001 — visibility must never block ingest
            pass
        return "skipped"
    # ONE ENQUIRY, ONE CARD. A second message moments after the first is the rest of the same thought,
    # so it joins the open card and redrafts there instead of opening a rival card with its own draft.
    open_card = _open_card(account, phone)
    if open_card:
        return _fold_in(open_card, co, skill, rt, msg, src)
    # a real name only - never the phone number WhatsApp puts where a name would go
    know_name = verdict.get("name") or ("" if _clean_phone(name) else name)
    brief = _brief(verdict, know_name, phone, msg, src)
    try:
        draft = worker.draft(skill, co, {"brief": brief}, author=rt.get("author") or "rashad")
    except Exception:  # noqa: BLE001
        draft = ""
    task = store.create_task(rt["company_id"], skill["id"], "wa_reply", {
        "brief": brief, "channel": "whatsapp", "account": account, "recipient": know_name or phone,
        "phone": phone, "chat_id": chat_id, "their_message": msg, "messages": [msg], "triage": verdict,
        **({"lead_source": src} if src else {})})
    if draft:
        # ARMED, not fired: flush_alerts sends it once the burst has settled, so one enquiry rings his
        # phone once. The Inbox card is live immediately either way.
        req = _arm_alert(dict(task.get("request") or {}))
        store.update_task(task["id"], draft=draft, status="awaiting_approval", request=req)
    return "drafted"


def flush_alerts() -> dict:
    """Send the owner's WhatsApp alert for every card whose burst window has gone quiet. Called from the
    engine loop, which is what gives the fold its settling time.

    The pending flag is cleared BEFORE the send is attempted: a card that cannot be alerted is still
    waiting in the Inbox, and a send that half-works must never leave the loop ringing his phone."""
    if not (owner_number() and cloud_ready()):
        return {"sent": 0}
    rows = db.query(
        "select id from tasks where kind='wa_reply' and status='awaiting_approval' "
        "and (request->>'alert_pending') = 'true' "
        "and coalesce((request->>'alert_due')::float, 0) <= %s order by id limit 20", (time.time(),))
    sent = 0
    for r in rows:
        t = store.get_task(r["id"])
        if not t:
            continue
        req = dict(t.get("request") or {})
        req["alert_pending"] = False
        req["alerted_at"] = time.time()
        store.update_task(r["id"], request=req)
        try:
            if alert_owner(r["id"]):
                sent += 1
        except Exception as e:  # noqa: BLE001 - the card is the record; a failed alert never loses it
            print(f"[whatsapp] alert flush failed for card {r['id']}: {str(e)[:160]}", flush=True)
    return {"sent": sent}


def _lane(account: str):
    """(routing, company, skill, slug) or None when the company/skill isn't set up."""
    rt = routing(account)
    co = store.get_company(rt["company_id"])
    skill = store.get_skill_by_key(rt["company_id"], rt["skill_key"]) if co else None
    if not (co and skill):
        return None
    return rt, co, skill, co.get("slug", "sensa")


# ---- transport 1: Meta Cloud API ---------------------------------------------------------------------

def verify_webhook(mode: str, token: str, challenge: str) -> str:
    """Meta's subscription handshake. It GETs the webhook with hub.verify_token and will not save the
    config unless we echo hub.challenge back verbatim. Constant-time compare, and a missing/blank
    configured token must NEVER pass."""
    want = config.get("WHATSAPP_VERIFY_TOKEN") or ""
    if mode == "subscribe" and want and hmac.compare_digest(token or "", want):
        return challenge or ""
    raise PermissionError("bad verify token")


def check_signature(raw: bytes, header: str) -> bool:
    """Meta signs every webhook POST with the APP SECRET as X-Hub-Signature-256. Unsigned or wrongly signed
    payloads are forgeries — anyone can POST to a public URL. No secret configured = reject everything,
    rather than silently accepting spoofed messages."""
    secret = config.get("WHATSAPP_APP_SECRET") or ""
    if not (secret and header and header.startswith("sha256=")):
        return False
    mine = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, header.split("=", 1)[1])


def ingest_cloud(payload: dict, account: str = "sensa-uk") -> dict:
    """Handle one Meta webhook body. Shape:
       entry[].changes[].value.messages[]  + .contacts[] (the sender's WhatsApp profile name)
    Statuses (delivered/read receipts) arrive on the same hook and are ignored. Meta RETRIES on any non-200,
    so this must never raise: a bad message is dropped, not propagated."""
    lane = _lane(account)
    if not lane:
        return {"drafted": 0, "skipped": 0, "reason": "company/skill missing"}
    rt, co, skill, slug = lane
    seen = set(db.setting_get(f"wa_seen:{account}") or [])
    drafted = skipped = 0
    fresh: list[str] = []
    for entry in (payload or {}).get("entry") or []:
        # A WEBHOOK IS THE ONLY PLACE THE WABA ID SHOWS UP. entry.id IS the WhatsApp Business Account id,
        # and template management needs it; the Graph API will not hand it over from a phone number id or
        # a system-user token (tried every edge, 6 Oct 2026). So keep it the moment Meta tells us.
        if entry.get("id") and not db.setting_get("wa_waba_id"):
            try:
                db.setting_set("wa_waba_id", str(entry["id"]))
                print(f"[whatsapp] learned WABA id {entry['id']}", flush=True)
            except Exception:  # noqa: BLE001
                pass
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            # DELIVERY STATUSES arrive on this same hook. A send that Meta ACCEPTS can still fail on the way
            # (no WhatsApp on that number, blocked, out of window), and an accepted-then-failed message is
            # otherwise completely invisible: the API returned a message id and nothing ever contradicted it.
            for st in value.get("statuses") or []:
                if st.get("status") == "failed":
                    errs = "; ".join(
                        f"{e.get('code')}: {e.get('title') or ''} {(e.get('error_data') or {}).get('details') or ''}"
                        .strip() for e in (st.get("errors") or [])) or "no reason given"
                    print(f"[whatsapp] delivery FAILED to {st.get('recipient_id')}: {errs}", flush=True)
                    try:
                        from . import notifications
                        notifications.notify("WhatsApp message failed to deliver",
                                             f"To {st.get('recipient_id')}: {errs}"[:400],
                                             priority="high", category="social",
                                             dedup_key=f"wa_fail:{st.get('recipient_id')}")
                    except Exception:  # noqa: BLE001
                        pass
                    codes = {str((e or {}).get("code")) for e in (st.get("errors") or [])}
                    who = st.get("recipient_id") or ""
                    if "131047" in codes:
                        # 131047 = re-engagement required: the window was shut after all, whatever our
                        # stamp said. Believe Meta.
                        forget_window(who)
                        try:
                            if is_owner(who):
                                ring_waiting()        # his alert: ring the doorbell instead
                            else:
                                reopen_undelivered(who, errs)   # a client: the card is NOT answered
                        except Exception:  # noqa: BLE001
                            pass
                    elif is_owner(who):
                        pass                          # any other failure to him: the log + notify is enough
                    else:
                        try:
                            reopen_undelivered(who, errs)
                        except Exception:  # noqa: BLE001
                            pass
            # the sender's WhatsApp profile name, keyed by wa_id — this is the push name Rashad expected
            names = {c.get("wa_id"): ((c.get("profile") or {}).get("name") or "")
                     for c in (value.get("contacts") or [])}
            for m in value.get("messages") or []:
                phone = _clean_phone(m.get("from") or "")
                if not phone:
                    continue
                # THE OWNER'S OWN MESSAGES ARE CONTROL, NEVER AN ENQUIRY. Checked before anything else so a
                # button tap or a voice note from him can never be triaged, CRM-captured or replied to.
                if is_owner(phone):
                    note_inbound(phone)       # his message reopens the window alerts have to fit inside
                    mid = m.get("id") or ""
                    if mid and (mid in seen or mid in fresh):   # Meta retries: never act on a tap twice
                        continue
                    if mid:
                        fresh.append(mid)
                    try:
                        _owner_control(m, _owner_text(m))
                    except Exception:  # noqa: BLE001 — a bad control message must not 500 the webhook
                        pass
                    continue
                if m.get("type") != "text":       # media/location from a client: capture later, never guess
                    continue
                msg = ((m.get("text") or {}).get("body") or "").strip()
                if not msg:
                    continue
                note_inbound(phone)   # their message is what makes a reply sendable for the next 24h
                k = _key(account, phone, msg)
                if k in seen or k in fresh:       # Meta retries on non-200; never draft the same twice
                    continue
                fresh.append(k)
                try:
                    out = _process_message(rt, co, skill, slug, account, phone,
                                           names.get(m.get("from"), ""), msg, chat_id=m.get("id") or "")
                except Exception:  # noqa: BLE001 — one bad message must not 500 the whole webhook
                    continue
                drafted += 1 if out == "drafted" else 0
                skipped += 1 if out == "skipped" else 0
    if fresh:
        db.setting_set(f"wa_seen:{account}", (list(seen) + fresh)[-800:])
    return {"drafted": drafted, "skipped": skipped}


def send_text(phone: str, text: str) -> dict:
    """Send a plain text reply via the Cloud API. Only valid inside the 24h customer service window; outside
    it Meta requires a pre-approved template, which we do not have yet and which is a separate build."""
    return _post({"messaging_product": "whatsapp", "recipient_type": "individual",
                  "to": phone.lstrip("+"), "type": "text",
                  "text": {"preview_url": False, "body": text}})


def cloud_ready() -> bool:
    return bool(config.get("WHATSAPP_TOKEN") and config.get("WHATSAPP_PHONE_NUMBER_ID"))


# ---- the owner's control channel: read, approve, teach, all from his own WhatsApp ---------------------
#
# Rashad approves WhatsApp enquiries FROM WhatsApp, because a back-and-forth chat that waits on someone
# opening the cockpit is not a chat (his call, 28 Sep 2026). The exemption is deliberately narrow:
# `wa_reply` ONLY. Every other outward kind still takes the biometric/PIN step-up, so widening this is a
# decision someone has to make on purpose rather than something that quietly already happened.

def owner_number() -> str:
    """Where approval alerts go. A setting first, so the number can change without a deploy."""
    return _clean_phone(db.setting_get("wa_owner_number") or config.get("WHATSAPP_OWNER_NUMBER") or "")


def is_owner(phone: str) -> bool:
    """True when a message came from the owner's own WhatsApp. Compared on the last 9 digits so a
    +971/00971/0 spelling can never make his own control messages look like a client enquiry."""
    own = owner_number()
    if not own:
        return False
    a, b = re.sub(r"\D", "", own), re.sub(r"\D", "", phone or "")
    return bool(a) and len(a) >= 9 and a[-9:] == b[-9:]


# THE 24 HOUR WINDOW IS THE WHOLE GAME ON WHATSAPP. Meta only accepts a free-form message to someone
# inside 24h of THEIR last message to us. Outside it, Meta ACCEPTS the send (returns a wamid) and fails it
# asynchronously on a status webhook, so an exception-based check never fires: the owner's alerts silently
# stopped arriving, and a four-day-old lead's reply was marked 'done' having never been delivered
# (cards 1050 / 1072, 6 Oct 2026). So the window is TRACKED PER NUMBER and decided BEFORE sending.
WINDOW_SEC = 24 * 3600
WINDOW_MARGIN_SEC = 20 * 60          # the OWNER's alerts never gamble on the last twenty minutes


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _win_key(phone: str) -> str:
    """Keyed on the FULL digits, never the last nine: a last-nine key collides across countries (1,384
    CRM rows share one), and here a collision would read as 'window open' for the wrong person."""
    return "wa_in:" + _digits(phone)


def note_inbound(phone: str) -> None:
    """Their message to us reopens the window. Stamped for every inbound number, owner included."""
    if not _digits(phone):
        return
    try:
        db.setting_set(_win_key(phone), {"at": time.time()})
    except Exception:  # noqa: BLE001 - bookkeeping must never break ingest
        pass


def forget_window(phone: str) -> None:
    """Meta says the window was shut after all (131047). Believe Meta, not our stamp."""
    try:
        db.setting_set(_win_key(phone), {})
    except Exception:  # noqa: BLE001
        pass


def last_inbound_at(phone: str) -> float:
    """When they last messaged us, as an epoch. Falls back to the newest card we raised for that number,
    so a card that predates this bookkeeping is judged on the message that created it rather than being
    assumed dead."""
    v = db.setting_get(_win_key(phone)) or {}
    try:
        at = float(v.get("at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    if at:
        return at
    row = db.one("select extract(epoch from max(created_at)) as ts from tasks where kind='wa_reply' "
                 "and regexp_replace(request->>'phone', '[^0-9]', '', 'g') = %s", (_digits(phone),))
    try:
        return float((row or {}).get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def window_open(phone: str, margin: float = 0.0) -> bool:
    """True when Meta will still accept a free-form message to this number. No margin by default: for a
    client we would rather try with ten minutes left than refuse a reply he has approved."""
    at = last_inbound_at(phone)
    return bool(at) and (time.time() - at) < (WINDOW_SEC - margin)


def window_closes_in(phone: str) -> float:
    """Seconds of window left, 0 when it has gone. For telling him BEFORE he approves, not after."""
    at = last_inbound_at(phone)
    return 0.0 if not at else max(0.0, (at + WINDOW_SEC) - time.time())


def owner_window_open() -> bool:
    return window_open(owner_number(), margin=WINDOW_MARGIN_SEC)


def reopen_undelivered(recipient: str, errs: str) -> int | None:
    """A send Meta accepted and then failed must NOT sit on the card as 'done'. The card goes back to the
    Inbox with the reason on it, because a lead we never actually answered reading as answered is how a
    lead gets quietly lost (card 1050, 6 Oct 2026)."""
    row = db.one("select id from tasks where kind='wa_reply' and status in ('done','sending','queued') "
                 "and regexp_replace(request->>'phone', '[^0-9]', '', 'g') = %s "
                 "order by id desc limit 1", (_digits(recipient),))
    if not row:
        return None
    t = store.get_task(row["id"]) or {}
    req = dict(t.get("request") or {})
    req["card_problem"] = ("Not delivered: " + errs)[:300]
    store.update_task(row["id"], status="awaiting_approval", request=req,
                      last_status=("undelivered: " + errs)[:300])
    print(f"[whatsapp] reopened card {row['id']}: accepted then failed - {errs[:120]}", flush=True)
    return row["id"]


def _doorbell(to: str, co: dict, req: dict) -> bool:
    """Ring the out-of-window template. A template cannot carry the draft, so it is only a doorbell: his
    reply reopens the window and _owner_control walks the real buttoned alert through the door."""
    tmpl = db.setting_get("wa_alert_template")
    if not (to and tmpl):
        return False
    try:
        send_template(to, tmpl, [co.get("name") or "Cortex",
                                 str(req.get("recipient") or req.get("phone") or "a new number")])
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[whatsapp] doorbell template failed: {str(e)[:200]}", flush=True)
        return False


def ring_waiting() -> bool:
    """Re-ring the doorbell for whatever is still waiting on him. Used when Meta tells us after the fact
    that a buttoned alert was refused for re-engagement, so the refusal is not the end of it."""
    to = owner_number()
    row = db.one("select id from tasks where kind='wa_reply' and status='awaiting_approval' "
                 "order by id desc limit 1")
    if not (to and row):
        return False
    t = store.get_task(row["id"]) or {}
    return _doorbell(to, store.get_company(t.get("company_id")) or {}, t.get("request") or {})


def _post(payload: dict) -> dict:
    """One place that talks to the Cloud API, so the pause guard and the error surfacing cannot diverge
    between a plain reply, a buttoned alert and a template."""
    if db.setting_get("whatsapp_paused") or db.setting_get("outbound_paused"):
        raise RuntimeError("WhatsApp sending is PAUSED - resume it to send")
    token = config.get("WHATSAPP_TOKEN")
    pnid = config.get("WHATSAPP_PHONE_NUMBER_ID")
    if not (token and pnid):
        raise RuntimeError("WhatsApp Cloud API not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID)")
    req = urllib.request.Request(f"{GRAPH}/{pnid}/messages", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:           # surface Meta's own error text, not a bare 400
        raise RuntimeError(f"WhatsApp send failed ({e.code}): {e.read().decode()[:300]}") from e


def send_buttons(phone: str, body: str, buttons: list[tuple[str, str]]) -> dict:
    """An interactive message with up to three reply buttons. Meta caps a button title at 20 characters
    and the body at 1024, so both are clamped here rather than failing the send."""
    rows = [{"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}} for bid, title in buttons[:3]]
    return _post({"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone.lstrip("+"),
                  "type": "interactive",
                  "interactive": {"type": "button", "body": {"text": body[:1024]},
                                  "action": {"buttons": rows}}})


def send_template(phone: str, name: str, params: list[str], lang: str = "en") -> dict:
    """Business-initiated messages outside the 24h window need a pre-approved template. Used only as the
    fallback when a plain alert is refused for re-engagement."""
    comps = [{"type": "body", "parameters": [{"type": "text", "text": p[:1024]} for p in params]}] if params else []
    return _post({"messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "template",
                  "template": {"name": name, "language": {"code": lang}, "components": comps}})


def _flat(text: str, cap: int = 700) -> str:
    """Flatten text for a template PARAMETER. Meta rejects a parameter containing a newline, a tab or
    more than four consecutive spaces, so a draft's paragraphs have to become one line. The template's
    OWN body may be multi-line; only what we substitute into it may not."""
    t = re.sub(r"\s*\n+\s*", "  /  ", (text or "").strip())
    t = re.sub(r"[\t ]{2,}", " ", t.replace("\t", " "))
    return (t[:cap - 1] + "…") if len(t) > cap else (t or "-")


def send_card_template(phone: str, name: str, params: list[str], payloads: list[str],
                       lang: str = "en") -> dict:
    """A template that carries the whole approval card AND working buttons.

    THIS IS THE ONLY WAY TO REACH HIM OUTSIDE THE 24H WINDOW WITH SOMETHING HE CAN ACT ON. A template's
    quick-reply buttons take a PER-MESSAGE payload, so the same wa:ok / wa:edit / wa:skip ids that the
    in-window buttoned alert uses ride on a template too. Without this, an out-of-window alert can only
    be a doorbell: it tells him something arrived and he has to message back before he can see it."""
    comps: list[dict] = [{"type": "body",
                          "parameters": [{"type": "text", "text": _flat(p)} for p in params]}]
    for i, payload in enumerate(payloads[:3]):
        comps.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                      "parameters": [{"type": "payload", "payload": payload[:128]}]})
    return _post({"messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "template",
                  "template": {"name": name, "language": {"code": lang}, "components": comps}})


def download_media(media_id: str) -> tuple[bytes, str]:
    """Fetch a media object (a voice note) by id. Two calls: the id resolves to a short-lived signed URL,
    which then needs the SAME bearer token to download."""
    token = config.require("WHATSAPP_TOKEN")
    hdr = {"Authorization": f"Bearer {token}"}
    with urllib.request.urlopen(urllib.request.Request(f"{GRAPH}/{media_id}", headers=hdr), timeout=30) as r:
        meta = json.loads(r.read().decode())
    url = meta.get("url")
    if not url:
        raise RuntimeError("media has no url")
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60) as r:
        return r.read(), (meta.get("mime_type") or "audio/ogg")


def alert_owner(task_id: int) -> bool:
    """Push a wa_reply card to the owner's WhatsApp with Approve / Edit / Skip. Returns True when sent.

    TRANSPORT IS CHOSEN, NOT DISCOVERED. Outside the 24h window Meta accepts a free-form send and then
    fails it on a status webhook, so an exception-based fallback never fires and the alert simply vanishes.
    We decide from `owner_window_open()` instead, and keep the exception path as a second line of defence.

    Fail-soft by design: the Inbox card already exists and is the record, so a failed alert must never
    lose the enquiry."""
    to = owner_number()
    if not (to and cloud_ready()):
        return False
    t = store.get_task(task_id)
    if not t or t["kind"] != "wa_reply":
        return False
    req = t.get("request") or {}
    co = store.get_company(t["company_id"]) or {}
    src = req.get("lead_source") or {}
    if not owner_window_open():
        # OUT OF WINDOW, BUT STILL ACTIONABLE. A card template carries the enquiry, the draft and the
        # three buttons; the bare doorbell (which carries nothing) is only the fallback for when no card
        # template is approved yet.
        card_tmpl = db.setting_get("wa_card_template")
        if card_tmpl:
            try:
                send_card_template(to, card_tmpl,
                                   [co.get("name") or "Cortex",
                                    str(req.get("recipient") or req.get("phone") or "a new number"),
                                    req.get("their_message") or "",
                                    t.get("draft") or "(no draft)"],
                                   [f"wa:ok:{task_id}", f"wa:edit:{task_id}", f"wa:skip:{task_id}"])
                return True
            except Exception as e:  # noqa: BLE001
                print(f"[whatsapp] card template failed for {task_id}: {str(e)[:220]}", flush=True)
        if _doorbell(to, co, req):
            return True
        # Nothing approved to send: try the buttons anyway. Out of window it will very likely fail, but
        # staying silent on purpose is worse than a send that might get through.
    body = (f"{co.get('name') or 'Cortex'} - WhatsApp enquiry\n"
            f"From: {req.get('recipient') or req.get('phone')}\n"
            + (f"Source: {src.get('line')}\n" if src.get("line") else "")
            + f"\nThey said:\n{(req.get('their_message') or '')[:400]}\n"
            + f"\nDraft reply:\n{(t.get('draft') or '(no draft)')[:500]}")
    buttons = [(f"wa:ok:{task_id}", "Approve & send"), (f"wa:edit:{task_id}", "Edit"),
               (f"wa:skip:{task_id}", "Skip")]
    try:
        send_buttons(to, body, buttons)
        return True
    except Exception as e:  # noqa: BLE001
        if _doorbell(to, co, req):
            return True
        print(f"[whatsapp] owner alert failed for card {task_id}: {str(e)[:200]}", flush=True)
        return False


CARD_TEMPLATE_BODY = (
    "New WhatsApp enquiry for {{1}}.\n\n"
    "From: {{2}}\n\n"
    "They said:\n{{3}}\n\n"
    "Draft reply:\n{{4}}\n\n"
    "Approve and send it as written, Edit it, or Skip.")


def create_card_template(name: str = "cortex_wa_card", lang: str = "en",
                         category: str = "UTILITY") -> dict:
    """Submit the owner's card template to Meta for approval, and record its name once accepted.

    Done through the API on purpose: this template is the whole out-of-window approval path, and
    hand-building it in Meta's UI is where this project has lost the most time. Meta refuses a variable
    at the very start or end of a body, which is why the text opens with words."""
    waba = db.setting_get("wa_waba_id")
    if not waba:
        raise RuntimeError("no wa_waba_id yet - it is learned from the next inbound webhook")
    token = config.require("WHATSAPP_TOKEN")
    body = {"name": name, "language": lang, "category": category, "components": [
        {"type": "BODY", "text": CARD_TEMPLATE_BODY,
         "example": {"body_text": [["Sensa Productions", "Samantha",
                                    "Hey Dear,  /  How are you?",
                                    "Hey Samantha, I'm good thanks, how are you?"]]}},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Approve & send"},
                                        {"type": "QUICK_REPLY", "text": "Edit"},
                                        {"type": "QUICK_REPLY", "text": "Skip"}]}]}
    req = urllib.request.Request(f"{GRAPH}/{waba}/message_templates",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"template create failed ({e.code}): {e.read().decode()[:400]}") from e
    db.setting_set("wa_card_template", name)   # alerts use it as soon as Meta approves it
    return out


# THE ONLY WAY BACK TO A LEAD WHO WENT QUIET. Outside 24h Meta accepts no free-form message to them, so
# a landing-page enquiry we did not answer in time is unreachable (cards 1050, 1053). This template needs
# no window; the moment they reply, the window opens and the REAL drafted reply sends normally. It is a
# nudge on purpose, not the answer: a template whose variable smuggles free-form content is what Meta has
# been clamping down on, and the conversation should happen in-window where it can be a conversation.
REENGAGE_BODY = (
    "Hi {{1}}, thanks for your message to {{2}} about video production. "
    "We have a reply waiting for you. Tap below and it will come straight through.")


def create_reengage_template(name: str = "sensa_enquiry_reply_waiting", lang: str = "en",
                             category: str = "MARKETING") -> dict:
    """Submit the client-facing re-engagement template. Meta decides the final category whatever we ask
    for; MARKETING carries per-user frequency limits, which at this volume do not bite."""
    waba = db.setting_get("wa_waba_id")
    if not waba:
        raise RuntimeError("no wa_waba_id yet - it is learned from the next inbound webhook")
    token = config.require("WHATSAPP_TOKEN")
    body = {"name": name, "language": lang, "category": category, "components": [
        {"type": "BODY", "text": REENGAGE_BODY,
         "example": {"body_text": [["Rahal", "Sensa Productions"]]}},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Send it over"}]}]}
    req = urllib.request.Request(f"{GRAPH}/{waba}/message_templates",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"template create failed ({e.code}): {e.read().decode()[:400]}") from e
    db.setting_set("wa_reengage_template", name)
    return out


NUDGE_COOLOFF_DAYS = 14


def nudge(task_id: int) -> dict:
    """Ask a lead who went quiet to reopen the chat, so the reply waiting on their card can actually send.

    APPROVED, NEVER AUTOMATIC. This is an outward message to a client, so it goes out on his tap like any
    other outward card; a stuck card OFFERS the nudge rather than firing it. The draft itself is left
    exactly as it is: when they reply, the window opens and the real reply sends through the normal gate."""
    tmpl = db.setting_get("wa_reengage_template")
    if not tmpl:
        return {"ok": False, "error": "no re-engagement template approved yet"}
    t = store.get_task(task_id)
    if not t or t["kind"] != "wa_reply":
        return {"ok": False, "error": "not a WhatsApp card"}
    req = dict(t.get("request") or {})
    phone = req.get("phone") or ""
    if not phone:
        return {"ok": False, "error": "no number on the card"}
    if (req.get("triage") or {}).get("category") in ("spam",):
        return {"ok": False, "error": "triaged as spam - never nudged"}
    if window_open(phone):
        return {"ok": False, "error": "their window is open - approve the reply instead"}
    last = req.get("nudged_at")
    if last and (time.time() - float(last)) < NUDGE_COOLOFF_DAYS * 86400:
        return {"ok": False, "error": f"already nudged in the last {NUDGE_COOLOFF_DAYS} days"}
    co = store.get_company(t["company_id"]) or {}
    # Their FIRST name only, and only when it is a real name: WhatsApp puts the number where a name goes,
    # and "Hi +971521015355" is worse than no greeting at all.
    who = (req.get("recipient") or "").strip()
    first = "" if (not who or _clean_phone(who)) else who.split()[0]
    try:
        send_card_template(phone, tmpl, [first or "there", co.get("name") or "Sensa Productions"], [])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"nudge failed: {str(e)[:200]}"}
    req["nudged_at"] = time.time()
    req.pop("card_problem", None)
    store.update_task(task_id, request=req,
                      last_status=f"nudged {first or phone} to reopen the chat")
    print(f"[whatsapp] nudged {phone} to reopen the window for card {task_id}", flush=True)
    return {"ok": True, "sent_to": first or phone}


def nudgeable(task_id: int) -> bool:
    """Whether the Inbox should offer the nudge on this card. Mirrors `nudge`'s own refusals."""
    t = store.get_task(task_id)
    if not (t and t["kind"] == "wa_reply" and db.setting_get("wa_reengage_template")):
        return False
    req = t.get("request") or {}
    if not req.get("phone") or (req.get("triage") or {}).get("category") == "spam":
        return False
    if window_open(req["phone"]):
        return False
    last = req.get("nudged_at")
    return not (last and (time.time() - float(last)) < NUDGE_COOLOFF_DAYS * 86400)


def template_status(name: str = "cortex_wa_card") -> dict:
    """Where Meta has got to with a template. APPROVED is when alerts start carrying the card."""
    waba = db.setting_get("wa_waba_id")
    if not waba:
        return {"error": "no wa_waba_id yet"}
    token = config.require("WHATSAPP_TOKEN")
    url = f"{GRAPH}/{waba}/message_templates?name={urllib.parse.quote(name)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.loads(r.read().decode()).get("data") or []
    return {"templates": [{"name": t.get("name"), "status": t.get("status"),
                           "category": t.get("category"), "language": t.get("language")}
                          for t in rows]}


def _owner_control(msg: dict, text: str) -> str:
    """Handle one message FROM the owner. Returns a short outcome word for the ingest tally.

    Three shapes: a button tap, a voice note or typed text answering an Edit, and anything else, which is
    acknowledged rather than silently dropped so he is never left wondering whether it landed."""
    from . import engine                          # local: engine imports this module
    inter = msg.get("interactive") or {}
    # TWO SHAPES, ONE MEANING. An in-window interactive button comes back as interactive.button_reply.id;
    # a TEMPLATE quick-reply comes back as type 'button' with button.payload. Reading only the first is
    # why taps on a card template would do nothing.
    btn = ((inter.get("button_reply") or {}).get("id")
           or (msg.get("button") or {}).get("payload") or "")
    if btn.startswith("wa:"):
        _, _, rest = btn.partition("wa:")
        action, _, tid = rest.partition(":")
        if not tid.isdigit():
            return "ignored"
        task_id = int(tid)
        if action == "ok":
            r = engine.approve_wa_reply(task_id)
            ok = bool(r.get("ok"))
            _tell(f"Sent." if ok else f"Not sent: {r.get('error') or 'blocked'}")
            return "approved" if ok else "blocked"
        if action == "skip":
            engine.skip_task(task_id)
            _tell("Skipped, nothing sent.")
            return "skipped"
        if action == "edit":
            db.setting_set("wa_edit_pending", {"task_id": task_id})
            _tell("Tell me what to change, typed or as a voice note.")
            return "editing"
        return "ignored"
    pending = db.setting_get("wa_edit_pending") or {}
    task_id = pending.get("task_id")
    if task_id and text.strip():
        db.setting_set("wa_edit_pending", {})
        t = store.get_task(int(task_id))
        if not t:
            _tell("That card has gone.")
            return "ignored"
        try:
            # The SAME correction path the cockpit uses, so the instruction redrafts the reply AND feeds
            # the standing-rule inference. Teaching from the phone is the point, not a side effect.
            engine.apply_correction(t, text.strip())
        except Exception as e:  # noqa: BLE001
            _tell(f"Could not apply that: {str(e)[:120]}")
            return "ignored"
        alert_owner(int(task_id))
        return "corrected"
    # HONOUR THE DOORBELL. The out-of-window template tells him to reply so Cortex can send the draft: his
    # reply is what reopens the 24h window, and this is the half that walks through the door it opened.
    # Without it the template promises something nothing delivers (his first real reply, 29 Sep 2026).
    if text.strip():
        waiting = db.query(
            "select id from tasks where kind='wa_reply' and status in ('awaiting_approval','awaiting_correction') "
            "order by id desc")
        if waiting:
            # Newest first and capped: a backlog must not answer his one "hi" with eight messages. The
            # rest stay in the Inbox and come through as he clears these.
            batch = waiting[:ALERT_BATCH]
            sent = sum(1 for r in batch if alert_owner(r["id"]))
            if sent:
                if len(waiting) > sent:
                    _tell(f"{len(waiting) - sent} more waiting in your Inbox after these.")
                return "resent"
            _tell("I could not send the draft through. It is waiting in your Inbox.")
            return "ignored"
        _tell("Nothing is waiting for approval right now. I will message you when an enquiry comes in.")
    return "ignored"


def _owner_text(m: dict) -> str:
    """The owner's words out of one message: typed text, or a voice note transcribed. A voice note IS the
    natural way to correct a draft on a phone, so it is a first-class input, not a fallback."""
    if m.get("type") == "text":
        return ((m.get("text") or {}).get("body") or "").strip()
    if m.get("type") in ("audio", "voice"):
        mid = ((m.get("audio") or m.get("voice")) or {}).get("id")
        if not mid:
            return ""
        try:
            from . import voice as _voice
            data, mime = download_media(mid)
            return _voice.transcribe(data, mime).strip()
        except Exception as e:  # noqa: BLE001
            print(f"[whatsapp] voice note not transcribed: {str(e)[:160]}", flush=True)
            _tell("I could not hear that voice note. Type it instead?")
            return ""
    return ""


def _tell(text: str) -> None:
    """A one-line reply to the owner. Never raises: a failed acknowledgement must not undo the action."""
    try:
        to = owner_number()
        if to:
            send_text(to, text)
    except Exception:  # noqa: BLE001
        pass


# ---- transport 2: the office-box runner (fallback) ----------------------------------------------------

def ingest_threads(account: str, threads: list[dict]) -> dict:
    """The runner pushes [{phone, name, message, ts, chat_id, from_me}]. Drafts a reply card per NEW inbound
    message. Reads + drafts only — nothing is sent here; the owner approves in the Inbox."""
    lane = _lane(account)
    if not lane:
        return {"drafted": 0, "skipped": 0, "reason": "company/skill missing"}
    rt, co, skill, slug = lane
    seen = set(db.setting_get(f"wa_seen:{account}") or [])
    drafted = skipped = stale = 0
    fresh: list[str] = []
    for t in threads or []:
        if t.get("from_me"):                       # our own outbound echo — never a thing to answer
            continue
        msg = (t.get("message") or "").strip()
        phone = _clean_phone(t.get("phone") or "")
        name = (t.get("name") or "").strip()
        if not msg or not phone:
            continue
        if t.get("ts") and not social_dm._is_recent(t.get("ts")):   # current messages only, never the backlog
            stale += 1
            continue
        k = _key(account, phone, msg)
        if k in seen or k in fresh:                # never draft the same message twice
            continue
        fresh.append(k)
        out = _process_message(rt, co, skill, slug, account, phone, name, msg, chat_id=t.get("chat_id") or "")
        drafted += 1 if out == "drafted" else 0
        skipped += 1 if out == "skipped" else 0
    if fresh:
        db.setting_set(f"wa_seen:{account}", (list(seen) + fresh)[-800:])
    return {"drafted": drafted, "skipped": skipped, "stale": stale}
