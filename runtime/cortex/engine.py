"""The engine — ties it together.

Loop:  process new tasks (worker -> manager -> approval/auto)  +  handle Telegram
taps and corrections (approve / correct->redraft->learn-rule / skip), updating the
trust streak and offering auto at the threshold.

Two task shapes:
  • string tasks (kind != 'blog')  -> Phase 1 path: draft text, approve = mark done.
  • blog tasks   (kind == 'blog')  -> Phase 2 path: write an article, stage it as a
    HIDDEN DRAFT on the company's WordPress, approve = publish it live. Blog tasks are
    NEVER auto-run (publishing/indexing always needs the owner's per-post tap — the
    web-page-builder golden rule), regardless of trust streak.
"""
from __future__ import annotations

import base64 as _b64
import html as _html
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from .schedule import _GST

from psycopg.types.json import Json

from . import (contentqueue, crm, db, doctext, documents, envelope, gmail, manager, media, meetnotes, policy,
               newsletter, notifications, pipeline, profile, ppc_report, provider, quotation, reminders,
               schedule, seo_report, store, webauthn_auth, whatsapp, worker)
from .integrations import telegram as tg, wordpress as wp

MONEY_KINDS = {"payment", "invoice_send"}  # never auto, regardless of trust
EMAIL_KINDS = {"email_reply"}              # an inbound-reply, sent via Gmail on approval
EMAIL_SEND_KINDS = {"email_reply", "email_draft"}    # ALL kinds that actually SEND an email on approval
EMAIL_RENDER_KINDS = EMAIL_KINDS | {"email_draft"}   # rendered as an email (envelope + logo) in the Inbox
NEVER_AUTO_KINDS = {"newsletter_idea", "newsletter_review", "newsletter_send", "email_reply", "email_draft",
                    "wa_reply"}  # outward sends always need the owner
# PUBLIC actions (go OUT to the public) — approving these needs a biometric step-up (see
# feedback_public_actions_biometric). Internal items use the normal approve. Split by where the action fires:
_APPROVE_PUBLIC = {"email_reply", "email_draft", "newsletter_idea", "blog", "social_shift", "social_action",
                   "wa_reply"}   # the action happens in approve_task
_CONFIRM_PUBLIC = {"newsletter_review", "newsletter_send"}        # the action happens in confirm_send_task

# Phase 3.2 — central kind -> security class (the single source of truth for gating; merged spec §3a).
#   internal : may auto-run on an auto lane with a clean manager verdict.
#   outward  : goes OUT to the public — NEVER auto; Inbox + PIN/biometric step-up to approve.
#   money    : never auto; human + PIN.
# Existing gate constants above stay the enforcers for now; this map is wired into the unified pipeline in
# 3.4/3.5. The assertion below keeps the two in lock-step so they can never silently diverge.
KIND_CLASS = {
    "content": "internal", "draft": "internal", "research": "internal", "summary": "internal",
    "report": "internal", "seo_report": "internal", "ppc_report": "internal", "crm_update": "internal",
    "internal_note": "internal",
    "project_plan": "internal",   # the living plan for a project: never sends, arms the next steps
    "quotation": "internal",   # a downloadable quote card; SENDING it to a client is a separate outward step
    "email_reply": "outward", "email_draft": "outward", "email_send": "outward", "blog": "outward",
    "blog_idea": "internal", "blog_scheduled": "outward",
    "newsletter_idea": "outward", "newsletter_review": "outward", "newsletter_send": "outward",
    "social_post": "outward", "dm_reply": "outward", "sms": "outward",
    "social_shift": "outward", "social_relogin": "internal", "social_action": "outward",
    "wa_reply": "outward",     # an approved WhatsApp reply the runner types back — goes to a real person
    "payment": "money", "invoice_send": "money", "refund": "money",
}


def kind_class(kind: str) -> str:
    """Security class for a task kind. Unknown kinds default to 'outward' (fail SAFE — never auto-send)."""
    return KIND_CLASS.get(kind, "outward")


# What tapping Approve ACTUALLY DOES, per kind — surfaced verbatim on the Inbox button so the consequence
# is never ambiguous (email sends, blog publishes, newsletter schedules/sends). Add a line when you add a kind.
APPROVE_ACTION = {
    "email_reply": "Approve & send", "email_draft": "Approve & send",
    "blog_idea": "Approve & build", "blog": "Approve & schedule",
    "project_plan": "Confirm plan",
    "newsletter_idea": "Approve & build", "newsletter_review": "Approve & schedule",
    "newsletter_send": "Approve & send",
    "social_shift": "Approve today's run", "social_relogin": "I've logged back in",
    "social_action": "Approve & run",
    "wa_reply": "Approve & send on WhatsApp",
    # lead_escalation: an INTERNAL strategic-lead briefing card — nothing sends anywhere; approving only
    # acknowledges the owner is taking the lead over. Deliberately NOT in KIND_CLASS (unknown kinds fail safe
    # to 'outward' = never auto) and not in _APPROVE_PUBLIC (internal ack, no biometric).
    "lead_escalation": "Acknowledge — I'm taking it over",
}


def approve_label(kind: str) -> str:
    return APPROVE_ACTION.get(kind, "Approve")


def is_auto_eligible(kind: str) -> bool:
    """Only 'internal' kinds may ever auto-run (and even then only on an auto lane with a clean verdict)."""
    return kind_class(kind) == "internal"


# guard: every currently-gated kind must be classed outward/money (so centralising can't loosen a gate)
assert all(kind_class(k) in ("outward", "money") for k in (NEVER_AUTO_KINDS | MONEY_KINDS)), \
    "KIND_CLASS drift: a never-auto/money kind is not classed outward/money"


def _biometric_gate(is_public: bool, stepup_token: str | None, money: bool = False) -> dict | None:
    """For a PUBLIC approval, require a fresh fingerprint/PIN step-up: returns a needs_biometric response if
    one wasn't provided, else None to proceed (consuming the step-up). Authorised TEAM members pass with
    their own PIN for outward work; MONEY-class kinds accept the OWNER's step-up only, always. No-op until
    a device/PIN is registered, so enabling this can never lock the operator out of existing flows."""
    if not (is_public or money) or not webauthn_auth.stepup_enabled():
        return None
    scope = webauthn_auth.consume_stepup(stepup_token)
    if scope and (scope == "owner" or not money):
        return None
    if scope and money:
        return {"ok": False, "needs_biometric": True,
                "error": "Money approvals are owner-only — this needs Rashad's fingerprint or PIN."}
    return {"ok": False, "needs_biometric": True,
            "error": "This goes out to the public — confirm with your fingerprint or PIN to approve."}
REPORTS_DIR = "/opt/coretex/reports"       # generated report PDFs (persisted, served to the Inbox)

_PWD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no ambiguous chars, easy to type on mobile


def _gen_password(n: int = 6) -> str:
    return "".join(secrets.choice(_PWD_ALPHABET) for _ in range(n))


# ---------- formatting ----------

def _fmt(task: dict, skill: dict, company: dict, verdict: dict | None) -> str:
    head = f"[{company['name']} · {skill['name']}]  ·  needs your yes"
    draft = task.get("draft") or ""
    if len(draft) > 3500:
        draft = draft[:3500] + "\n…(truncated for preview)"
    return f"{head}\n\n{draft}{_verdict_line(verdict)}"


def _verdict_line(verdict: dict | None) -> str:
    """The Manager's review, in one line the owner reads before deciding."""
    if not verdict:
        return ""
    v = verdict.get("verdict") or ("pass" if verdict.get("aligned", True) else "revise")
    summary = (verdict.get("summary") or "").strip()
    issues = verdict.get("issues") or []
    if v == "pass":
        return f"\n\n🛠 Manager: passed" + (f" — {summary}" if summary else "")
    label = "needs your judgement" if v == "escalate" else "flagged"
    body = summary or "; ".join(issues)
    return f"\n\n⚠ Manager ({label}): {body}" if body else f"\n\n⚠ Manager: {label}"


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?i)</(p|h2|h3|li|blockquote)>", "\n", html)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fmt_blog(company: dict, skill: dict, art: dict, verdict: dict | None,
              preview: str | None = None) -> str:
    head = f"[{company['name']} · {skill['name']}]  ·  blog post — WordPress draft"
    body = _html_to_text(art["html"])
    if len(body) > 2600:
        body = body[:2600] + "\n…(truncated for preview)"
    extra = _verdict_line(verdict)
    tail = "\n\n📝 Saved as an unpublished DRAFT on the site (not public, not indexed)."
    if preview:
        tail += f"\nPreview the finished page (open while logged into wp-admin):\n{preview}"
    tail += "\nTap “Publish live” to publish it, or Correct / Discard."
    return f"{head}\n\nTITLE: {art['title']}\n\n{body}{extra}{tail}"


def _approval_buttons(task_id: int) -> list[list[dict]]:
    return [[tg.button("✅ Approve", f"ap:{task_id}"),
             tg.button("✎ Correct", f"co:{task_id}"),
             tg.button("✗ Skip", f"sk:{task_id}")]]


# ---------- email replies ----------

def _booking_slots_brief(co: dict | None) -> str:
    """If the company has a booking calendar configured, fetch a few REAL open slots and return an instruction to
    offer them when a call is the next step (exact times, never invented). Empty string otherwise (safe no-op for
    companies without booking set up, so it never changes their drafts)."""
    if not co:
        return ""
    slug = co.get("slug")
    try:
        bk = (profile.get(co["id"]) or {}).get("booking")
        if not bk or not db.setting_get(f"calendar_refresh_token:{slug}"):
            return ""
        from . import calendar as _cal
        slots = _cal.format_slots(_cal.free_slots(slug))
        if not slots:
            return ""
        line = bk.get("email_line") or "or let us know what works for you and we'll do our best to meet it."
        return ("\n\nIF you propose a call and mention specific times, these are the ONLY real open slots "
                "(GST) — quote them EXACTLY, never invent, shift or add a time:\n  - " + "\n  - ".join(slots) +
                f"\nOffer a couple of them naturally in a sentence, then add, in your own words: \"{line}\". "
                "Everything else about how scheduling is handled (links, tone, alternatives) is governed by "
                "the standing rules.")
    except Exception:  # noqa: BLE001 — availability must never break drafting
        return ""


def _notify_new_opportunity(co: dict, opp: dict, how: str) -> None:
    """ONE announcement per new opportunity: an Inbox notification card (+ phone push once a device is
    registered) and a Telegram mirror line. Fail-soft — announcing must never break the pipeline."""
    val = (f" — est. {opp.get('currency')} {float(opp['value']):,.0f} (Cortex estimate)"
           if opp.get("value") else "")
    try:
        notifications.notify(f"New opportunity: {opp.get('title')}",
                             f"{co['name']} — {how}{val}. Account linked; edit the figure or disqualify any time.",
                             priority="normal", category="lead", company_id=co.get("id"),
                             target_type="deal", target_id=opp.get("id"))
    except Exception:  # noqa: BLE001
        pass
    try:
        tg.send(f"[{co['name']}] New opportunity: {opp.get('title')}{val} ({how}).")
    except Exception:  # noqa: BLE001
        pass


def _escalation_brief(inq: dict, co: dict | None, sug: dict | None) -> str:
    """Brief the worker to write an INTERNAL owner briefing for a strategic-bucket lead (`lead_escalation`
    card). Never emailed anywhere — approving only acknowledges the takeover. Plumbing only: the facts are
    code-stamped from intake + research; what makes a lead strategic lives in the lead-qualification rules.
    DORMANT since 2026-08-14: owner-takeover mode is off (strategic leads get a normal drafted reply, per the
    trained rule); kept so the mode — and any legacy lead_escalation card — still works if re-enabled."""
    s = sug or {}
    p = s.get("person") or {}
    facts = [f"Lead: {inq.get('name') or '(unknown)'} <{inq.get('email')}>"]
    if inq.get("company_name"):
        facts.append(f"Company stated: {inq['company_name']}")
    if p.get("role"):
        facts.append(f"Role (researched, public sources): {p['role']}")
    if p.get("location"):
        facts.append(f"Based (researched): {p['location']}" + (f" — timezone {p['timezone']}" if p.get("timezone") else ""))
    if s.get("reason"):
        facts.append(f"Qualification: {s.get('verdict')} ({s.get('confidence')}) — {s['reason']}")
    return ("INTERNAL BRIEFING for the owner — NOT an email to the lead; it is never sent externally, and "
            "approving the card only acknowledges the handover. A STRATEGIC-bucket lead has come in; per the "
            "lead-qualification standing rules the owner takes this over personally. Write a concise plain-text "
            "briefing (no markdown, no greeting-to-the-lead, no sign-off): who they are, why this is strategic "
            "under the rules, the recommended next step, and — if known — their timezone for a call. Use ONLY "
            "these facts, never invent others:\n" + "\n".join(f"- {x}" for x in facts) +
            f"\n\nTheir message, verbatim:\n{(inq.get('message') or '').strip()}")


def _email_brief(inq: dict, co: dict | None = None) -> str:
    """Frame a website enquiry as a reply-drafting brief for the worker."""
    return ("Draft a reply to this website enquiry. Write it as a clean, professional plain-text email: "
            "normal sentences in short paragraphs. Do NOT use any markdown, no **bold**, no #headings, no "
            "[text](link) markdown links (write URLs plainly). Avoid bullet lists unless genuinely needed, "
            "and if so use a simple hyphen. Do NOT add any closing, sign-off, name or signature, those are "
            "appended automatically. Output ONLY the email body, no subject or headers, and never a Cc/Bcc line "
            "or any 'sending note'/'system note' (recipients and Cc/Bcc are handled by the sending system). Reply "
            "directly to the person, in the company voice, following the standing rules.\n\n"
            f"Their name: {inq.get('name') or 'there'}\n"
            f"Their email: {inq.get('email') or '(unknown)'}\n"
            f"Their message:\n{(inq.get('message') or inq.get('snippet') or '').strip()}"
            ) + _booking_slots_brief(co)


_EMAIL_RE = r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"


def _email_envelope(task: dict, company: dict) -> dict:
    """Who the approved reply goes to / from / cc / bcc — resolved from the inquiry, the company profile,
    AND any CC/BCC the owner set as a standing rule on the skill."""
    inq = (task.get("request") or {}).get("inquiry") or {}
    try:
        data = profile.get(company["id"]) or {}
    except Exception:  # noqa: BLE001
        data = {}
    cc_list, bcc_list = [], []
    # CC/BCC (BOTH the profile defaults AND any skill-rule "cc <addr>") apply to inquiry REPLIES only.
    # A Talk-composed OUTBOUND draft NEVER inherits a Cc/Bcc from any automatic source — it goes to its
    # single addressed recipient and nothing else. (Operator: single drafted emails don't copy anyone in.)
    if not (task.get("request") or {}).get("outbound"):
        for v in [(data.get("default_cc") or "").strip()]:
            if "@" in v:
                cc_list.append(v)
        for v in [(data.get("default_bcc") or "").strip()]:
            if "@" in v:
                bcc_list.append(v)
        try:
            # COMPILED envelope config — distilled from the skill's rules by envelope.compile_skill()
            # whenever rules change. Rules are the only source of cc behaviour; this just executes.
            _ecfg = envelope.get(store.get_skill(task.get("skill_id"))) or {}
            cc_list += [e for e in _ecfg.get("cc_add", []) if "@" in e]
            bcc_list += [e for e in _ecfg.get("bcc_add", []) if "@" in e]
        except Exception:  # noqa: BLE001
            _ecfg = {}
    req = task.get("request") or {}
    # ALWAYS-CC (company profile 'always_cc'): people who ride EVERY email this company sends, replies
    # and Talk-composed outbound alike - the one cc source that is not reply-only (owner, 30 Aug:
    # "copy Dalal into everything"). Still subject to never_cc, cc_remove and the To-dedup below.
    cc_list += [str(v).strip() for v in (data.get("always_cc") or [])
                if isinstance(v, str) and "@" in v]
    # THREAD CONTINUITY: everyone already on this conversation stays on it - replying to a multi-party
    # thread (client + their consultants + third parties) must never quietly drop half the room.
    cc_list += [str(v).strip() for v in (req.get("thread_cc") or [])
                if isinstance(v, str) and "@" in v]
    cc_list += [e for e in (req.get("cc_extra") or []) if "@" in e]
    try:   # DEAL LOOP: deal contacts marked cc ride EVERY email on that deal (owner: Alia at MAH Gold
        # responds on the development-department address and must be looped into all communications)
        did = task.get("deal_id") or req.get("deal_id")
        if did:
            drow = db.one("select contacts from crm_projects where id=%s", (int(did),))
            for c_ in (drow or {}).get("contacts") or []:
                if isinstance(c_, dict) and c_.get("cc") and "@" in (c_.get("email") or ""):
                    cc_list.append(c_["email"].strip())
    except Exception:  # noqa: BLE001
        pass
    if req.get("high_value"):   # Director-handled opportunity: the profile's high-value cc set rides along
        cc_list += [str(v).strip() for v in (data.get("high_value_cc") or []) if isinstance(v, str) and "@" in v]
    outbound = bool(req.get("outbound"))   # a Talk-composed email_draft (not a reply) — no "Re:" prefix
    subj = inq.get("subject") or "your enquiry"
    from_addr = (req.get("from_email") or data.get("reply_from") or "").strip() or None
    if from_addr and from_addr.lower() in {a.lower() for a in INBOXES.values()}:
        # HARD policy: catch-all addresses never send — fall back to the company's reply_from person
        from_addr = (data.get("reply_from") or "").strip() or None
        req = {**req, "mailbox_rt": None}   # and never their mailbox token either
    # cc exclusions — RULE-driven via the compiled envelope config (never_cc), plus the owner's per-card
    # removals (request.cc_remove). The only mechanical part is deduping the To recipient out of cc.
    try:
        _nc = [str(x).lower() for x in (envelope.get(store.get_skill(task.get("skill_id"))) or {}).get("never_cc", [])]
    except Exception:  # noqa: BLE001
        _nc = []
    drop = {e.lower() for e in (req.get("cc_remove") or [])}
    # ALWAYS-BCC: the owner's silent copy of everything, from every company. Global setting first (so a
    # new company is covered the day it is created), then any per-company profile override. BCC because
    # the recipient must never see it (owner, 31 Aug 2026 - he reads everything in one personal inbox).
    try:
        _gb = db.setting_get("always_bcc") or []
        bcc_list += [str(v).strip() for v in _gb if isinstance(v, str) and "@" in v]
        bcc_list += [str(v).strip() for v in (data.get("always_bcc") or [])
                     if isinstance(v, str) and "@" in v]
    except Exception:  # noqa: BLE001
        pass
    # NEVER-COPY (company profile 'never_cc'): addresses the owner has ruled off every email, whatever
    # the source - team roster, thread participants, or an inherited cc. Sensa: hello@ is the catch-all
    # for first enquiries only and is never copied (owner, 31 Aug 2026).
    drop |= {str(v).strip().lower() for v in (data.get("never_cc") or []) if isinstance(v, str)}
    drop.add((inq.get("email") or "").lower())            # To-recipient in cc = duplicate mail, always deduped
    if from_addr:
        drop.add(from_addr.lower())   # the SENDER is never cc'd on their own email (team-copy rule:
        # "whoever is not sending it copies everybody else in" - owner, 30 Aug)
    if "sender" in _nc and from_addr:
        drop.add(from_addr.lower())
    drop |= {x for x in _nc if "@" in x}
    drop.discard("")
    cc_list = [e for e in cc_list if e.lower() not in drop]
    bcc_list = [e for e in bcc_list if e.lower() not in drop]
    cc = ", ".join(dict.fromkeys(cc_list))     # dedupe, keep order
    bcc = ", ".join(dict.fromkeys(bcc_list))
    # per-sender signature: a reply sent FROM a specific person (e.g. gino@sensa.digital) carries THEIR
    # signature (profile.signatures[email]); otherwise the company default.
    sender_sig = (data.get("signatures") or {}).get((from_addr or "").lower()) or {}
    sig_plain = (sender_sig.get("signature") or data.get("signature") or "").strip()
    sig_html = (sender_sig.get("signature_html") or data.get("signature_html") or "").strip()
    return {"to": inq.get("email") or "", "to_name": inq.get("name") or "", "from": from_addr,
            "cc": cc or None, "bcc": bcc or None,
            # never "Re: Re:" — a thread continuation keeps the subject verbatim so Gmail chains it
            "subject": subj if (outbound or subj.lower().startswith(("re:", "fwd:", "fw:"))) else ("Re: " + subj),
            "name": inq.get("name") or "", "signature": sig_plain, "signature_html": sig_html}


def _fmt_email(task: dict, skill: dict, company: dict, verdict: dict | None) -> str:
    env = _email_envelope(task, company)
    inq = (task.get("request") or {}).get("inquiry") or {}
    their = (inq.get("message") or inq.get("snippet") or "").strip()
    if len(their) > 800:
        their = their[:800] + "…"
    head = f"[{company['name']} · {skill['name']}]  ·  reply to {env['name'] or env['to']} — needs your yes"
    line = f"To: {env['to']}" + (f"  ·  From: {env['from']}" if env["from"] else "") + \
           (f"  ·  Cc: {env['cc']}" if env["cc"] else "") + f"\nSubject: {env['subject']}"
    their_block = f"\n\nTHEIR MESSAGE:\n“{their}”" if their else "\n\nTHEIR MESSAGE: (none provided)"
    draft = (task.get("draft") or "").strip()
    if len(draft) > 3000:
        draft = draft[:3000] + "\n…(truncated for preview)"
    return f"{head}\n\n{line}{their_block}\n\nDRAFTED REPLY:\n{draft}{_verdict_line(verdict)}"


def _clean_email_text(s: str) -> str:
    """Strip markdown so a plain-text email reads neat and professional (no **, #, [](), stray bullets)."""
    s = s or ""
    s = re.sub(r"^\s*Subject\s*:[^\n]*\n+", "", s, flags=re.I)     # a leaked Subject: header never ships in a body
    s = s.replace("**", "").replace("__", "")                      # bold markers
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", s)                    # markdown headings
    def _link(m):                                                 # [text](url): drop redundant URL text
        text, url = m.group(1).strip(), m.group(2).strip()
        norm = lambda x: x.rstrip("/").replace("https://", "").replace("http://", "").lower()
        return url if norm(text) == norm(url) else f"{text}: {url}"
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", _link, s)
    s = re.sub(r"(?m)^(\s*)[*+]\s+", r"\1- ", s)                   # normalise bullets to "- "
    s = s.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").replace("–", "-")  # house: no em/en dash
    s = re.sub(r"[ \t]+\n", "\n", s)                               # trailing spaces
    s = re.sub(r"\n{3,}", "\n\n", s)                               # collapse blank runs
    return s.strip()


_SIGNOFF = re.compile(
    r"(?is)\n\s*(best regards|kind regards|warm regards|best wishes|many thanks|thank you|thanks|"
    r"best|regards|cheers|sincerely|talk soon)\s*[,.]?\s*(\n+\s*[^\n]{1,40}){0,2}\s*$")


def _strip_signoff(s: str) -> str:
    """Remove a trailing sign-off + name the worker may have added, so the real signature isn't doubled."""
    return _SIGNOFF.sub("", s or "").rstrip()


_ENQUIRY_REF_LABEL = "For your reference, here is the enquiry you sent us:"


def _inquiry_reference_block(task: dict) -> tuple[str, str]:
    """The prospect's ORIGINAL enquiry, reproduced VERBATIM after the signature, so a first-response reply
    carries the context of what they asked. Web contact-form enquiries have no email thread to reply into, so
    we quote their submitted message instead. Done HERE (not via a worker prompt) so the text is reproduced
    exactly, never paraphrased or invented by the model. Returns (plain, html); ('','') when not applicable."""
    req = task.get("request") or {}
    if req.get("outbound"):                                   # Talk-composed outbound draft, not a reply
        return "", ""
    if req.get("followup") or req.get("lead_chase") or req.get("thread_reply"):
        # CONTINUATION of an existing conversation (chase / nudge / reply in-thread): NO reference box —
        # a thread reply must read like a natural email, and these tasks' inquiry.message is an INTERNAL
        # system note ("No reply yet...") that must never appear in a customer email. First responses only.
        return "", ""
    inq = req.get("inquiry") or {}
    msg = (inq.get("message") or inq.get("snippet") or "").strip()
    if not msg:
        return "", ""
    plain = f"\n\n----------\n{_ENQUIRY_REF_LABEL}\n\n{msg}"
    html_block = ("<div style='margin-top:22px;padding:12px 14px;border-left:3px solid #d9d9d9;"
                  "background:#f6f6f6;color:#555;font-size:13px;line-height:1.55'>"
                  f"<div style='color:#888;margin-bottom:7px'>{_html.escape(_ENQUIRY_REF_LABEL)}</div>"
                  f"<div style='white-space:pre-wrap'>{_html.escape(msg)}</div></div>")
    return plain, html_block


def compose_reply_body(task: dict, company: dict) -> str:
    """The plain-text body that will be sent: the cleaned reply plus the company signature (text). Also the
    fallback part of the multipart email, so non-HTML clients still get a clean message."""
    env = _email_envelope(task, company)
    body = _strip_signoff(_clean_email_text(task.get("draft") or ""))
    sig = (env.get("signature") or "").strip()
    if sig:
        body = body + "\n\n" + sig
    body += _inquiry_reference_block(task)[0]                 # quote the original enquiry AFTER the signature
    return body


# ---- HTML email (so the footer logo + formatting render) ----

LOGO_PATH = os.environ.get("CORTEX_TAB_LOGO", "/opt/cortex/assets/tabscanner-logo.png")


def _logo_path(company: dict | None) -> str | None:
    """The local logo file for a company's email signature (attached inline via cid). Per-company file at
    /opt/cortex/assets/<slug>-logo.png; Tabscanner keeps its legacy env override + filename. The file should
    be the brand's LIGHT-BACKGROUND logo (sigs render on white), sourced from the company's Drive asset folder."""
    slug = (company or {}).get("slug") or ""
    candidates = []
    if slug == "tabscanner":
        env = os.environ.get("CORTEX_TAB_LOGO")
        if env:
            candidates.append(env)
        candidates.append("/opt/cortex/assets/tabscanner-logo.png")
    if slug:
        candidates.append(f"/opt/cortex/assets/{slug}-logo.png")
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _linkify(s: str) -> str:
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1" style="color:#1E9BD7">\1</a>', s)


def _body_to_html(text: str) -> str:
    blocks = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln for ln in para.split("\n")]
        real = [ln for ln in lines if ln.strip()]
        if real and all(ln.lstrip().startswith("- ") for ln in real):
            items = "".join(f"<li style='margin:0 0 4px 0'>{_linkify(_html.escape(ln.lstrip()[2:]))}</li>"
                             for ln in real)
            blocks.append(f"<ul style='margin:0 0 14px 0;padding-left:20px'>{items}</ul>")
        else:
            blocks.append("<p style='margin:0 0 14px 0'>"
                          + "<br>".join(_linkify(_html.escape(ln)) for ln in lines) + "</p>")
    return "".join(blocks)   # no stray newlines between tags (would show as gaps in a pre-wrap context)


def _signature_html(plain_sig: str, logo_src: str | None, alt: str = "") -> str:
    lines = (plain_sig or "").split("\n")
    while lines and (not lines[0].strip()
                     or re.match(r"(?i)^(best regards|kind regards|regards|thanks)[,.]?$", lines[0].strip())):
        lines.pop(0)
    rows, first = [], True
    for ln in lines:
        if not ln.strip():
            continue
        cell = _linkify(_html.escape(ln))
        rows.append(f"<strong>{cell}</strong>" if first else cell)
        first = False
    logo = (f"<img src='{logo_src}' alt='{_html.escape(alt)}' width='150' "
            "style='display:block;border:0;margin:0 0 10px 0'>") if logo_src else ""
    return ("<div style='margin-top:20px;font-family:Arial,Helvetica,sans-serif;font-size:13px;"
            "color:#0A1828;line-height:1.55'><p style='margin:0 0 14px 0'>Best regards,</p>"
            f"{logo}{'<br>'.join(rows)}</div>")


def compose_reply_html(task: dict, company: dict, for_preview: bool = False) -> dict:
    """Returns {plain, html, inline} — the multipart email. For preview the logo is a data-URI (renders in
    the browser); for the real send it's a cid inline image."""
    env = _email_envelope(task, company)
    clean = _strip_signoff(_clean_email_text(task.get("draft") or ""))
    ref_html = _inquiry_reference_block(task)[1]              # original enquiry, quoted after the signature
    sig_html = (env.get("signature_html") or "").strip()
    if sig_html:
        # Stored rich-HTML signature (its logo is referenced by a public URL) — render it verbatim
        # after the body. No cid attachment needed; the same markup renders in the cockpit preview.
        html_body = ("<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#0A1828;"
                     f"line-height:1.6'>{_body_to_html(clean)}"
                     f"<div style='margin-top:20px'><p style='margin:0 0 14px 0'>Best regards,</p>{sig_html}</div>"
                     f"{ref_html}</div>")
        return {"plain": compose_reply_body(task, company), "html": html_body, "inline": []}
    plain_sig = (env.get("signature") or "").strip()
    logo_file = _logo_path(company)
    logo_src, inline = None, []
    if logo_file:
        if for_preview:
            logo_src = "data:image/png;base64," + _b64.b64encode(open(logo_file, "rb").read()).decode()
        else:
            cid = re.sub(r"[^a-z0-9]", "", (company or {}).get("slug") or "company") + "logo"
            logo_src, inline = f"cid:{cid}", [(cid, logo_file)]
    html_body = ("<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#0A1828;"
                 f"line-height:1.6'>{_body_to_html(clean)}{_signature_html(plain_sig, logo_src, (company or {}).get('name') or '')}"
                 f"{ref_html}</div>")
    return {"plain": compose_reply_body(task, company), "html": html_body, "inline": inline}


def _send_email_reply(task: dict, skill: dict, company: dict, actor: str, auto: bool) -> dict:
    if db.setting_get("email_sending_paused"):   # global kill-switch — keep the card, don't send
        store.update_task(task["id"], status="awaiting_approval")
        return {"blocked": True, "error": "Email sending is PAUSED. Resume it to send this reply."}
    # SAFETY (audit F2): claim the card ATOMICALLY before any side effect. Compare-and-swap means a
    # double-tap, a second device, or a concurrent supersede can never produce two sends of one card;
    # a crash mid-send leaves it stuck in 'sending' (alerted, never re-approvable) instead of duplicated.
    claimed = db.execute("update tasks set status='sending', updated_at=now() where id=%s and "
                         "status in ('awaiting_approval','awaiting_correction','new') returning id",
                         (task["id"],))
    if not claimed:
        return {"blocked": True, "error": "This card was already handled or just changed — reload it."}
    task = store.get_task(task["id"]) or task     # re-read: send exactly what the DB holds NOW
    env = _email_envelope(task, company)
    mt = (task.get("request") or {}).get("meeting")
    if mt and not mt.get("event_id"):   # confirmed slot -> the event + Meet room must exist (still guest-less)
        from . import calendar as gcal
        _slug = (company or {}).get("slug") or ""
        cal_co = mt.get("calendar") or _slug
        if not db.setting_get(f"calendar_refresh_token:{cal_co}"):
            # same guard at SEND: never book one brand's client onto another brand's calendar
            store.update_task(task["id"], status="awaiting_approval")
            return {"blocked": True,
                    "error": f"{(company or {}).get('name')} has no calendar connected — the email "
                             "promises a meeting we cannot book. Nothing was sent. Connect a calendar "
                             "for this company, or tell me which calendar to use."}
        try:
            ev = gcal.create_event(cal_co, start=datetime.fromisoformat(mt["start"]),
                                   minutes=int(mt.get("minutes") or 30),
                                   summary=mt.get("summary") or "Call",
                                   description=f"Booked via Cortex approval (task #{task['id']}).", meet=True)
        except Exception as ex:  # noqa: BLE001 — never send an email promising a meeting we failed to book
            store.update_task(task["id"], status="awaiting_approval")
            return {"blocked": True, "error": f"Couldn't book the calendar/Meet: {ex}. Nothing was sent — approve again to retry."}
        mt = {**mt, "event_id": ev.get("id"), "meet": ev.get("meet") or "", "calendar": cal_co}
        task = dict(task)
        task["request"] = {**(task.get("request") or {}), "meeting": mt}
        if mt.get("meet") and mt["meet"] not in (task.get("draft") or ""):
            task["draft"] = (task.get("draft") or "").rstrip() + f"\n\nGoogle Meet: {mt['meet']}"
        store.update_task(task["id"], request=task["request"], draft=task["draft"])
    c = compose_reply_html(task, company, for_preview=False)
    req = task.get("request") or {}
    files = list(req.get("attachments") or [])            # outbound drafts carry real file attachments
    file_names = list(req.get("attachment_names") or [])  # ...with their original filenames
    for ref in (req.get("attach_docs") or []):            # library documents referenced by id -> real files
        doc = documents.get(int(ref.get("id", 0)), company_id=task.get("company_id"))
        if not doc:
            raise RuntimeError(f"attached document #{ref.get('id')} not found — remove it from the card")
        # APPROVAL PIN: Drive is the source of truth, so the canonical file can change between the
        # owner approving and this send. If it did, STOP - he must never send a document he did not
        # read (owner design, 31 Aug). The pin is taken when the document is put on the card.
        if ref.get("pin_md5"):
            st = documents.drive_state(doc)
            if st is None:
                raise RuntimeError(f"'{doc['filename']}' is no longer readable in Drive (moved, renamed "
                                   "or deleted) — nothing was sent. Re-attach the correct file.")
            if st.get("md5") and st["md5"] != ref["pin_md5"]:
                raise RuntimeError(f"'{doc['filename']}' has CHANGED in Drive since you approved this "
                                   "card — nothing was sent. Open it, check it, and re-attach.")
        files.append(f"data:{doc['mime']};base64," + _b64.b64encode(documents.read_bytes(doc)).decode())
        file_names.append(doc["filename"])
    files, file_names = (files or None), (file_names or None)
    # per-company send: a brand with its own project sends from its OWN mailbox/client (else Tabscanner legacy)
    slug = (company or {}).get("slug")
    send_company = _inbox_client_company(slug) if slug else None
    send_rt_key, from_addr = None, env["from"]
    _mrt = (task.get("request") or {}).get("mailbox_rt")
    if _mrt and db.setting_get(_mrt):          # reply goes out from the mailbox that received the email
        send_rt_key = _mrt
    elif send_company:
        # per-company send ONLY if that brand actually has its own mailbox token; otherwise fall back to the
        # legacy global send mailbox (e.g. Tabscanner has its own OAuth client file but sends via the shared
        # gmail_send_refresh_token / read inbox — never 500 just because the :slug token was never minted).
        if db.setting_get(f"gmail_send_refresh_token:{slug}"):
            send_rt_key = f"gmail_send_refresh_token:{slug}"
        elif db.setting_get(f"gmail_refresh_token:{slug}"):
            send_rt_key = f"gmail_refresh_token:{slug}"
        if send_rt_key:
            from_addr = from_addr or db.setting_get(f"gmail_send_account:{slug}") or db.setting_get(f"gmail_account:{slug}")
        else:
            send_company = None     # no brand token -> legacy global path (_send_token uses gmail_send_refresh_token)
    th = req.get("thread") or {}               # continue THEIR Gmail thread (threadId + reply headers)
    try:
        res = gmail.send_message(env["to"], env["subject"], c["plain"], from_addr=from_addr, cc=env["cc"],
                                 html=c["html"], inline_images=c["inline"], bcc=env.get("bcc"),
                                 files=files, file_names=file_names, company=send_company, send_rt_key=send_rt_key,
                                 thread_id=th.get("id") or None, in_reply_to=th.get("msg_id") or None,
                                 references=th.get("references") or None)
    except Exception as ex:  # noqa: BLE001 — the send did NOT happen: release the claim so a retry is safe
        store.update_task(task["id"], status="awaiting_approval")
        return {"blocked": True, "error": f"Send failed, nothing went out: {ex}"}
    try:
        crm.log_event(env["to"], "email_sent", f"Email sent: {env['subject']}", company.get("slug"))
    except Exception:  # noqa: BLE001 — CRM history must never block the send
        pass
    try:   # our reply went out -> a PAUSED auto cadence on this deal re-arms at its normal gap
        did = (task.get("request") or {}).get("deal_id") or task.get("deal_id")
        if did:
            crm.resume_followups(int(did))
    except Exception:  # noqa: BLE001 — cadence bookkeeping must never block the send
        pass
    try:   # the guest joins the calendar event only AFTER the email genuinely went — never an invite
        # for an unsent mail (a blocked approve once invited Sunwoo to a meeting the email never confirmed)
        mt2 = (task.get("request") or {}).get("meeting")
        if mt2 and mt2.get("event_id") and not mt2.get("invited"):
            from . import calendar as gcal
            gcal.add_attendee(mt2.get("calendar") or (company or {}).get("slug") or "sensa",
                              mt2["event_id"], env["to"])
            store.update_task(task["id"], request={**(task.get("request") or {}),
                                                   "meeting": {**mt2, "invited": True}})
    except Exception as ex:  # noqa: BLE001 — the email is already sent; surface the invite failure instead
        notifications.notify(f"Email sent, but adding {env['to']} to the calendar event failed: {ex}. "
                             "Add them by hand on the calendar.", "Meeting invite", category="reminder",
                             company_id=task.get("company_id"))
    store.update_task(task["id"], status="done")
    store.log_decision(task["id"], skill["id"], actor, "send",
                       snapshot={"to": env["to"], "cc": env["cc"], "bcc": env.get("bcc"),
                                 "from": env["from"], "subject": env["subject"], "gmail_id": res.get("id")})
    try:   # pipeline loop: log the send on the deal timeline + track the promises this email makes
        pipeline.record_send(task, env, company)
    except Exception:  # noqa: BLE001 — timeline bookkeeping must never block the send
        pass
    # pre-qualification chase clock: a reply to a FUNNEL LEAD (came via intake, qual:email exists) that is NOT an
    # opportunity chase (no deal_id) -> (re)arm the silence chase, so a lead who then goes quiet gets followed up.
    try:
        to = (env.get("to") or "").lower()
        q = db.setting_get(f"qual:email:{to}") if to else None
        if q and q.get("company_id") == company["id"] and not req.get("deal_id"):
            _arm_lead_followup(company["id"], to)
    except Exception:  # noqa: BLE001
        pass
    if auto:
        tg.send(f"[{company['name']} · {skill['name']}] auto-sent a reply to {env['to']}. #{task['id']} done.")
    return {"sent_to": env["to"], "id": res.get("id")}


def _blog_buttons(task_id: int) -> list[list[dict]]:
    return [[tg.button("✅ Publish live", f"ap:{task_id}"),
             tg.button("✎ Correct", f"co:{task_id}"),
             tg.button("✗ Discard", f"sk:{task_id}")]]


def _site_for(task: dict, company: dict):
    """Return a WordPress connection if this task should publish, else None."""
    if task["kind"] != "blog":
        return None
    return wp.for_company(company)


# ---------- task processing ----------

def process_new_tasks() -> None:
    for task in store.tasks_by_status("new"):
        try:
            _run_task(task)
        except Exception as e:  # noqa: BLE001
            store.update_task(task["id"], status="failed")
            tg.send(f"Task #{task['id']} failed: {e}")


def _push_approval(task: dict, skill: dict, company: dict) -> None:
    """Instant lock-screen ping that something needs the owner's yes. No extra Inbox row — the task IS the
    card (no-mirror rule); this is just the push."""
    try:
        label = (task.get("title") or (task.get("request") or {}).get("title")
                 or (skill.get("name") if skill else None) or task.get("kind") or "a task")
        notifications.push_only("Needs your yes", f"{company['name']}: {str(label)[:80]}",
                                url="/", category="approval")
    except Exception:  # noqa: BLE001
        pass


def _run_task(task: dict) -> None:
    skill = store.get_skill(task["skill_id"])
    company = store.get_company(task["company_id"])
    if task["kind"] in ("seo_report", "ppc_report"):   # a scheduled report instance — generate it, don't worker-draft it
        store.update_task(task["id"], status="drafting")
        _run_report_task(task)
        return
    if task["kind"] == "newsletter_scheduled":   # a scheduled newsletter's 1st-of-month arrived
        _run_newsletter_scheduled_task(task, skill, company)
        return
    if task["kind"] == "blog_scheduled":   # a queued blog's publishing day arrived -> publish it live
        _run_blog_scheduled_task(task, skill, company)
        return
    if not skill:   # e.g. an action reminder pointed at a skill that doesn't exist — fail cleanly
        store.update_task(task["id"], status="failed",
                          manager={"summary": "No valid skill assigned — nothing drafted.", "aligned": False})
        tg.send(f"Task #{task['id']} couldn't run: no valid skill assigned.")
        return
    store.update_task(task["id"], status="drafting")

    if task["kind"] == "blog":   # a blog request -> IDEATION: propose readable concept(s) to approve first
        _run_blog_ideation(task, skill, company)   # NO HTML built/staged yet; the build happens on approval
        return

    dreq = _request_for_draft(task)   # inbound attachment refs -> data: URLs, drafter's eyes only
    draft = worker.draft(skill, company, dreq)
    if task.get("kind") in ("email_reply", "email_draft"):
        draft = _ensure_clean_email(skill, company, dreq, draft)
    verdict = manager.check(skill, company, draft, dreq)   # the Manager judges the SAME evidence the worker saw
    if not verdict["aligned"] and verdict["issues"]:
        draft = worker.draft(skill, company, dreq, manager_feedback=verdict["issues"])
        verdict = manager.check(skill, company, draft, dreq)

    task = store.update_task(task["id"], draft=draft, manager=verdict, attempts=task["attempts"] + 1)
    _maybe_extract_meeting(task, draft)   # a confirmed slot in the draft -> calendar+Meet booked on approval

    # Earned autonomy + escalation valve: even on an auto lane, the Manager's verdict must be a clean,
    # confident pass. Anything flagged, escalated, or low-confidence still goes to the owner.
    auto_ok = (skill["authority"] == "auto" and skill["stakes"] == "low"
               and not skill["paused"] and task["kind"] not in MONEY_KINDS
               and task["kind"] not in NEVER_AUTO_KINDS
               and kind_class(task["kind"]) == "internal"   # audit F4: outward NEVER auto-runs, full stop
               and verdict.get("aligned") and not verdict.get("escalate"))
    if auto_ok:
        _execute(task, skill, company, actor="cortex", auto=True)
    else:
        preview = _fmt_email(task, skill, company, verdict) if task["kind"] in EMAIL_KINDS \
            else _fmt(task, skill, company, verdict)
        msg = tg.send(preview, _approval_buttons(task["id"]))
        store.update_task(task["id"], status="awaiting_approval", tg_message_id=msg["message_id"])
        _push_approval(task, skill, company)


def _blog_digest_body(company: dict, posts: list[dict]) -> tuple[str, str]:
    n = len(posts)
    subject = f"{company['name']}: {n} blog post{'s' if n != 1 else ''} ready to review"
    lines = [f"Hi,\n\n{n} new {company['name']} blog post{'s are' if n != 1 else ' is'} staged for review. "
             "Click each link below to read the draft (you'll be asked to log in to WordPress first):\n"]
    for i, p in enumerate(posts, 1):
        lines.append(f"{i}. {p['title']}\n   {p.get('preview') or '(no preview link)'}\n")
    lines.append("\nOnce you've had a look, let Rashad know in Cortex so he can approve. Thanks.")
    return subject, "\n".join(lines)


def send_blog_review_digest(company_id: int, posts: list[dict], dry_run: bool = False) -> dict:
    """Email the company's TEST GROUP one digest of staged blog drafts (titles + wp-login preview links),
    via plain Gmail — one message per recipient (the single-send guard forbids fan-out). Used as the
    test-group review step for a batch of blog drafts; the link logs the reviewer into wp-admin then lands
    on the rendered draft."""
    company = store.get_company(company_id)
    group = newsletter.test_group(company_id)
    posts = [p for p in (posts or []) if p.get("title")]
    if not (company and group and posts):
        return {"sent": 0, "reason": "no company / test group / posts"}
    subject, body = _blog_digest_body(company, posts)
    if dry_run:
        return {"dry_run": True, "recipients": [g["email"] for g in group], "subject": subject, "body": body}
    slug = company.get("slug")
    sent = 0
    for g in group:
        try:
            gmail.send_message(g["email"], subject, body, company=slug)
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    tg.send(f"{company['name']}: emailed {sent}/{len(group)} test-group reviewers about "
            f"{len(posts)} staged blog post{'s' if len(posts) != 1 else ''}.")
    return {"sent": sent, "recipients": len(group)}


def _run_blog_ideation(task: dict, skill: dict, company: dict) -> None:
    """IDEATION: turn a blog request into N FULL TEXT DRAFT cards — the whole post written out as readable
    text (no HTML, no images, nothing staged). The owner reads + iterates the WRITING for free; approving a
    draft is what triggers the formatted build (images + WordPress). One draft -> one card; six -> six cards."""
    from . import blog
    req = task.get("request") or {}
    n = max(1, int(req.get("count") or 1))
    drafts = [blog.compose(company["id"], req.get("brief", "")) for _ in range(n)]
    drafts = [c for c in drafts if c and c.get("title")]
    if not drafts:
        store.update_task(task["id"], status="failed", last_status="no blog draft generated")
        tg.send(f"[{company['name']}] couldn't generate a blog draft — try again or give a brief.")
        return
    total = len(drafts)
    # owner-provided images ride the request through every rewrite — losing them here cost task #305 its banner
    keep = ({"attachments": req["attachments"], "attachment_names": req.get("attachment_names") or []}
            if req.get("attachments") else {})
    for i, c in enumerate(drafts, 1):
        c = blog.add_internal_links(company["id"], c)   # weave contextual internal links into the draft (outbound)
        c = blog.add_service_logos(company["id"], c)    # logos for any external services mentioned + link out
        title = (c.get("title") or "Blog draft").strip()
        text = blog.content_text(c)
        body = (f"Draft {i} of {total}\n\n{text}") if total > 1 else text
        if i == 1:   # reuse the original request task for the first draft
            store.update_task(task["id"], kind="blog_idea", title=title, draft=body,
                              request={"brief": req.get("brief", ""), "content": c, **keep},
                              status="awaiting_approval")
            t = store.get_task(task["id"])
        else:
            t = store.create_task(company["id"], skill["id"], "blog_idea",
                                  {"brief": req.get("brief", ""), "content": c, **keep})
            store.update_task(t["id"], title=title, draft=body, status="awaiting_approval")
        _push_approval(t, skill, company)
    tg.send(f"[{company['name']}] {total} blog draft{'s' if total > 1 else ''} ready to read in your Inbox "
            f"(full text). Iterate the writing, then approve to build the formatted post.")


def _build_blog_bg(task_id: int) -> None:
    """Run the formatted build OFF the approval request (in a thread), so the concept card leaves the Inbox
    immediately and the review card reappears (via the approval push) only when the post is actually ready."""
    try:
        task = store.get_task(task_id)
        if not task:
            return
        _build_blog_from_concept(task, store.get_skill(task["skill_id"]), store.get_company(task["company_id"]))
    except Exception as e:  # noqa: BLE001 — a build error must not vanish the task silently
        try:
            store.update_task(task_id, status="failed", last_status=f"build error: {e}"[:120])
            tg.send(f"Blog build failed (#{task_id}) — concept kept, nothing sent: {e}")
        except Exception:  # noqa: BLE001
            pass


def _build_blog_from_concept(task: dict, skill: dict, company: dict) -> dict:
    """Approve a blog CONCEPT -> build the formatted post, stage it as a hidden (noindex) WordPress draft, and
    turn THIS card into the 'review the formatted post' card (kind='blog'). The card body stays readable text
    + a preview link to the formatted page — NEVER raw HTML."""
    from . import blog, brand as _brand
    site = wp.for_company(company)
    if not site:
        store.update_task(task["id"], status="failed", last_status="WordPress not connected")
        return {"error": f"WordPress not connected for {company['name']}."}
    req = task.get("request") or {}
    content = req.get("content")
    _tmpl = str((_brand.get_brand_kit(company["id"]) or {}).get("template") or "")
    rich = _tmpl.startswith("dark") or _tmpl == "light-saas"
    if content:   # the APPROVED text: render it + generate images ONCE — do NOT re-compose (what he read = what ships)
        art = blog.build_from_content(company["id"], content, attachments=req.get("attachments"),
                                      attachment_names=req.get("attachment_names"))
        verdict = manager.check(skill, company, f"TITLE: {art['title']}\n\n{art['html']}", {"brief": ""})
    elif rich:    # legacy concept card (no stored content) -> compose from the brief
        art = blog.build(company["id"], req.get("brief", ""))
        verdict = manager.check(skill, company, f"TITLE: {art['title']}\n\n{art['html']}", {"brief": req.get("brief", "")})
    else:
        art = worker.draft_article(skill, company, {"brief": req.get("brief", "")})
        verdict = manager.check(skill, company, f"TITLE: {art['title']}\n\n{art['html']}", {"brief": req.get("brief", "")})
    hero_url = (art.get("images") or {}).get("hero")     # set it as the WP featured image -> the theme banner
    _fi = (art.get("content") or {}).get("featured_image") or art.get("featured_image") or {}
    _cat = ((art.get("content") or {}).get("category") or "").split("·")[0].strip()   # WP category (the kicker)
    post = site.stage_draft(art["title"], art["html"], featured_url=hero_url,
                            featured_alt=(_fi.get("alt") or art["title"]), category=_cat)   # unpublished noindex draft
    db.setting_set(f"wp:{task['id']}", {"post_id": post["id"], "preview": post.get("preview"),
                                        "edit": post.get("edit"), "title": art["title"]})
    # stash the content + image URLs so a REVISION edits this exact post (reusing the same images), never rebuilds
    db.setting_set(f"blog_build:{task['id']}", {"content": art.get("content"), "images": art.get("images")})
    # Inbound links are seeded ONLY on publish (never on a draft URL — that would create dead links). Record the
    # PLAN now as stored knowledge + surface it on the review card so the owner sees what will happen on publish.
    inbound_note = ""
    try:
        plan = blog.seed_inbound_links(company, post["id"], post.get("link") or "", art["title"], dry_run=True)
        if plan:
            db.setting_set(f"inbound_plan:{task['id']}", {"post_id": post["id"], "links": plan})
            inbound_note = ("\n\nOn publish, these existing posts will automatically link back to this one:\n"
                            + "\n".join(f"- {p['title']} (anchor: \"{p['anchor']}\")" for p in plan))
    except Exception:  # noqa: BLE001
        pass
    dek = (art.get("dek") or "").strip() or "The formatted post is ready to review."
    store.update_task(task["id"], kind="blog", title=art["title"],
                      draft=f"{dek}\n\nThe formatted post is staged as a hidden draft. Open the preview link "
                            f"below to review it, then approve to schedule." + inbound_note,
                      manager=verdict, attempts=task["attempts"] + 1, status="awaiting_approval")
    t = store.get_task(task["id"])
    msg = tg.send(_fmt_blog(company, skill, art, verdict, post.get("preview")), _blog_buttons(task["id"]))
    store.update_task(task["id"], tg_message_id=msg["message_id"])
    _push_approval(t, skill, company)   # blog tasks ALWAYS go to the owner — never auto-publish (golden rule)
    # The build only runs on the owner's Cortex approval, so the test-group review email IS authorised — send
    # the company's reviewers the title + preview link (the designed test-group review step). The rule is "no
    # sends from a BUILD/TEST without approval"; a real operator approval through Cortex is that approval.
    try:
        send_blog_review_digest(company["id"], [{"title": art["title"], "preview": post.get("preview")}])
    except Exception:  # noqa: BLE001
        pass
    return {"built": art["title"], "preview": post.get("preview")}


def _execute(task: dict, skill: dict, company: dict, actor: str, auto: bool = False) -> dict:
    if task["kind"] in ("newsletter_idea", "newsletter_review", "newsletter_send"):
        # EVERY outward newsletter send (the test send to reviewers, the schedule, the live send) routes
        # through the cockpit confirm — it shows exactly who it reaches + takes the PIN/fingerprint. A plain
        # approve (cockpit OR Telegram) NEVER fires one.
        n = (len(newsletter.test_group(company["id"])) if task["kind"] == "newsletter_idea"
             else len(newsletter.recipients(company["id"])))
        who = "test-group" if task["kind"] == "newsletter_idea" else "recipient"
        return {"needs_confirm": True, "recipients": n,
                "error": f"Confirm with the {who} count ({n:,}) in the cockpit."}
    if task["kind"] in EMAIL_SEND_KINDS:   # email_reply + email_draft both SEND on approval (after the PIN gate)
        return _send_email_reply(task, skill, company, actor, auto)
    if task["kind"] == "blog_idea":   # approve the CONCEPT -> build OFF the request so the concept card LEAVES
        store.update_task(task["id"], status="drafting")   # the Inbox at once; the review card returns when ready
        threading.Thread(target=_build_blog_bg, args=(task["id"],), daemon=True).start()
        return {"building": True,
                "sent_to": "the build — the formatted post returns to your Inbox for review when ready"}
    if task["kind"] == "blog":   # approving the BUILT post QUEUES it to publish on the company's monthly day
        return _schedule_blog(task, skill, company, actor)
    if task["kind"] == "social_shift":   # approving CLEARS the runner to run today's governed shift, then stop
        req = task.get("request") or {}
        acct = req.get("account", "")
        db.setting_set(f"social_shift_ok:{acct}:{req.get('date', '')}",
                       {"approved": True, "plan": req.get("plan"), "by": actor})
        store.update_task(task["id"], status="done")
        store.log_decision(task["id"], skill["id"], actor, "approve", snapshot={"plan": req.get("plan")})
        return {"sent_to": f"the runner — it will work {req.get('persona', 'the account')}'s shift in working hours"}
    if task["kind"] == "social_action":   # a one-off manual action — approving QUEUES it for the runner to perform
        req = task.get("request") or {}
        store.update_task(task["id"], status="queued")
        store.log_decision(task["id"], skill["id"], actor, "approve", snapshot={"action": req.get("action")})
        return {"sent_to": f"the runner — {req.get('persona', 'Paul')} will {req.get('action', 'action')} this shortly"}
    if task["kind"] == "wa_reply":
        # Cloud API if it is configured (the supported transport, sends immediately), else queue the card for
        # the office-box runner to type into WhatsApp Web. Either way nothing moves until this approval.
        req = task.get("request") or {}
        who = req.get("recipient") or req.get("phone") or "the contact"
        text = (task.get("draft") or "").strip()
        if whatsapp.cloud_ready():
            if not (req.get("phone") and text):
                return {"ok": False, "error": "no phone or empty draft - nothing sent"}
            try:
                whatsapp.send_text(req["phone"], text)
            except Exception as e:  # noqa: BLE001 - a failed send must NOT read as approved+sent
                store.update_task(task["id"], last_status=str(e)[:300])
                return {"ok": False, "error": f"WhatsApp send failed: {str(e)[:200]}"}
            store.update_task(task["id"], status="done")
        else:
            store.update_task(task["id"], status="queued")
        store.log_decision(task["id"], skill["id"], actor, "approve", snapshot={"draft": task.get("draft")})
        return {"sent_to": f"WhatsApp — {who}"}
    if task["kind"] == "social_relogin":   # acknowledging the re-login clears the logged-out flag so shifts resume
        db.setting_set(f"social_loggedout:{(task.get('request') or {}).get('account', '')}", False)
        store.update_task(task["id"], status="done")
        return {}
    # Phase 1 string path: 'execute' = mark done + log.
    store.update_task(task["id"], status="done")
    _arm_on_done_reminders(task)   # e.g. an issued invoice card arms its payment follow-up clock
    store.log_decision(task["id"], skill["id"], actor, "auto" if auto else "approve",
                       snapshot={"draft": task.get("draft")})
    if auto:
        tg.send(f"[{company['name']} · {skill['name']}] auto-ran (trusted). #{task['id']} done.")
    return {}


# ---------- telegram handling ----------

def handle_updates() -> None:
    offset = db.setting_get("tg_offset")
    for u in tg.get_updates(offset=offset, timeout=15):
        db.setting_set("tg_offset", u["update_id"] + 1)
        try:
            if "callback_query" in u:
                _on_callback(u["callback_query"])
            elif "message" in u and u["message"].get("text"):
                _on_message(u["message"])
        except Exception as e:  # noqa: BLE001
            tg.send(f"(hiccup handling your tap: {e})")


def _on_callback(cq: dict) -> None:
    tg.answer_callback(cq["id"])
    data = cq.get("data", "")
    action, _, ref = data.partition(":")
    if action == "au":  # accept the auto offer
        skill = store.get_skill(int(ref))
        if skill:
            store.set_authority(skill["id"], "auto")
            tg.send(f"'{skill['name']}' is now on AUTO for low-stakes work. Pause it anytime.")
        return
    if action == "th":  # raise the bar: th:{skill_id}:{n}
        sid, _, num = ref.partition(":")
        if sid.isdigit() and num.isdigit():
            sk = store.set_threshold(int(sid), int(num))
            tg.send(f"Okay — '{sk['name']}' now needs {num} clean approvals in a row before I offer auto.")
        return
    if action == "nlauto":  # monthly newsletter send auto: nlauto:{company_id}:{0|1}
        cid, _, on = ref.partition(":")
        if cid.isdigit():
            set_newsletter_auto(int(cid), on == "1")
            tg.send("Monthly newsletter send is now " + ("AUTO (no more Stage-3 confirm)." if on == "1"
                                                         else "manual — I'll ask you to confirm each one."))
        return
    task = store.get_task(int(ref)) if ref.isdigit() else None
    if not task:
        return
    skill = store.get_skill(task["skill_id"])
    company = store.get_company(task["company_id"])
    if action == "ap":
        _approve(task, skill, company)
    elif action == "sk":
        _skip(task, skill, company)
    elif action == "co":
        store.update_task(task["id"], status="awaiting_correction")
        if task.get("tg_message_id"):
            tg.edit(task["tg_message_id"], f"✎ Correcting '{skill['name']}'. Send me your correction as a message.")
    elif action in ("ry", "rn", "ru"):   # ry=company rule, ru=universal rule, rn=no
        _confirm_rule(task, skill, yes=(action in ("ry", "ru")), universal=(action == "ru"))


def _approve(task: dict, skill: dict, company: dict, stepped_up: bool = False) -> dict:
    # SAFETY (audit F1): the Telegram button must obey the same gates as the cockpit. A stale tap on an
    # already-handled card must never re-send, and outward/money kinds never bypass the step-up once the
    # owner has a device registered — Telegram can't do biometrics, so those route to the cockpit.
    fresh = store.get_task(task["id"])
    if not fresh or fresh.get("status") not in ("awaiting_approval", "awaiting_correction"):
        if task.get("tg_message_id"):
            tg.edit(task["tg_message_id"], "Already handled — nothing sent again.")
        return {"blocked": True, "error": "already handled"}
    task = fresh
    # Telegram taps carry no biometrics, so an outward card taps through to the cockpit. The COCKPIT
    # path has already consumed a fresh step-up at the gate (stepped_up=True) - re-checking here blocked
    # legitimate approvals outright (cards 383/384 returned OK and sent nothing).
    if (not stepped_up and kind_class(task.get("kind")) in ("outward", "money")
            and webauthn_auth.is_registered()):
        if task.get("tg_message_id"):
            tg.edit(task["tg_message_id"], "This one needs your PIN/fingerprint — approve it in the cockpit.")
        return {"blocked": True, "error": "step-up required — approve in the cockpit"}
    result = _execute(task, skill, company, actor="owner")
    if result and (result.get("blocked") or result.get("needs_confirm")):
        # a guarded newsletter send did NOT go out — don't claim approval, don't bump the streak
        if task.get("tg_message_id"):
            tg.edit(task["tg_message_id"],
                    f"⚠️ Not sent — {result.get('error', 'confirmation needed')}. Confirm a live send in the cockpit.")
        return result
    skill = store.bump_streak(skill["id"])
    if task.get("tg_message_id"):
        if result and result.get("link"):
            tg.edit(task["tg_message_id"],
                    f"✅ Approved — published live: {result['link']}  (streak {skill['trust_streak']}).")
        elif result and result.get("scheduled"):
            tg.edit(task["tg_message_id"],
                    f"✅ Approved — queued to publish {result['scheduled']} (on the calendar).")
        elif result and result.get("sent_to"):
            tg.edit(task["tg_message_id"],
                    f"✅ Approved — Cortex sent it to {result['sent_to']} (streak {skill['trust_streak']}).")
        else:
            tg.edit(task["tg_message_id"], f"✅ Approved — '{skill['name']}' (streak {skill['trust_streak']}). Done.")
    # Offer auto only for non-blog skills (blog ideation + publishing must never go auto).
    if task["kind"] not in ("blog", "blog_idea") and skill["authority"] == "ask" and skill["trust_streak"] >= skill["auto_threshold"]:
        higher = skill["trust_streak"] + 20
        tg.send(f"'{skill['name']}' has {skill['trust_streak']} clean approvals. "
                f"Put it on auto for low-stakes work, or raise the bar for extra confidence?",
                [[tg.button("Yes, set auto", f"au:{skill['id']}"),
                  tg.button(f"No — raise to {higher}", f"th:{skill['id']}:{higher}")]])
    return result or {}


def _arm_on_done_reminders(task: dict) -> None:
    """A card can carry on_done_reminders: [{title, days}] — armed the moment the owner marks it done
    (e.g. 'invoice issued' -> the payment follow-up clock starts). Dates are code-computed offsets."""
    try:
        specs = (task.get("request") or {}).get("on_done_reminders") or []
        for r in specs[:5]:
            d = datetime.now(timezone.utc) + timedelta(days=int(r.get("days") or 7))
            reminders.create(str(r.get("title") or "Follow-up")[:200], d,
                             company_id=task.get("company_id"), priority="high",
                             target_type="deal" if (task.get("request") or {}).get("deal_id") else "task",
                             target_id=(task.get("request") or {}).get("deal_id") or task["id"])
        if specs:
            notifications.notify(f"Card #{task['id']} done — {len(specs)} follow-up clock(s) armed.",
                                 "Follow-up armed", category="reminder", company_id=task.get("company_id"))
    except Exception:  # noqa: BLE001
        pass


def _skip(task: dict, skill: dict, company: dict) -> None:
    # Blog drafts (concept or formatted): a skip AUTO-DELETES — trash the staged WP draft AND remove the card
    # + its stashed content, so a discarded blog leaves nothing behind (Rashad: skipped drafts auto-delete).
    if task["kind"] in ("blog", "blog_idea"):
        info = db.setting_get(f"wp:{task['id']}") or {}
        note = "Discarded"
        if info.get("post_id"):
            try:
                wp.for_company(company).trash(info["post_id"])
                note = "Discarded — draft removed from the site"
            except Exception:  # noqa: BLE001
                note = "Discarded (couldn't remove the WP draft — check it manually)"
        store.reset_streak(skill["id"])
        if task.get("tg_message_id"):
            tg.edit(task["tg_message_id"], f"✗ {note} and deleted.")
        db.execute("delete from settings where key in (%s,%s)", (f"wp:{task['id']}", f"blog_build:{task['id']}"))
        db.execute("delete from tasks where id=%s", (task["id"],))
        return
    store.update_task(task["id"], status="rejected")
    mt = (task.get("request") or {}).get("meeting") or {}
    if mt.get("event_id") and not mt.get("invited"):
        # a pre-booked (guest-less) meeting dies with its card — phantom bookings must never
        # accumulate and eat availability. No guest was ever invited, so nobody is notified.
        try:
            from . import calendar as gcal
            gcal.delete_event(mt.get("calendar") or (company or {}).get("slug") or "sensa", mt["event_id"])
        except Exception:  # noqa: BLE001
            pass
    store.reset_streak(skill["id"])   # a rejection breaks the clean-approval streak
    store.log_decision(task["id"], skill["id"], "owner", "reject", snapshot={"draft": task.get("draft")})
    if task.get("tg_message_id"):
        tg.edit(task["tg_message_id"], f"✗ Skipped — '{skill['name']}'.")


def _maybe_propose_rule(task: dict, skill: dict, text: str, old: str, new: str) -> None:
    """Run the 'is this a standing rule?' inference OFF the request path. A correction returns as soon as
    the redraft is done; the offer (a second LLM call that can be slow) lands afterwards via Telegram and
    the Inbox (rule:{id}), so a slow/overloaded inference never hangs the cockpit."""
    threading.Thread(target=_infer_rule_offer, args=(task, skill, text, old or "", new or ""),
                     daemon=True).start()


def _infer_rule_offer(task: dict, skill: dict, text: str, old: str, new: str) -> None:
    try:
        rule = worker.infer_rule(skill, text, old, new)
    except Exception:  # noqa: BLE001 — background; never surface
        return
    if rule.get("is_rule") and rule.get("rule"):
        company = store.get_company(task["company_id"])
        co = company["name"] if company else "this company"
        # store the proposal SELF-CONTAINED (skill + company baked in) so it survives the task being archived/
        # deleted and can always be decided + landed in the right skill, even days later.
        db.setting_set(f"rule:{task['id']}", {
            "rule": rule["rule"], "skill_id": skill["id"], "skill_key": skill.get("skill_key"),
            "company_id": task["company_id"], "company": co,
            "skill_name": skill.get("name"), "kind": task.get("kind")})
        tg.send(f"I'm reading your correction as a standing rule:\n\n“{rule['rule']}”\n\n"
                f"Where should it live? '{co}' only, or ALL companies?",
                [[tg.button(f"{co} only", f"ry:{task['id']}"), tg.button("All companies", f"ru:{task['id']}")],
                 [tg.button("No, just this once", f"rn:{task['id']}")]])


def _norm_proposal(task_id: int, raw) -> dict:
    """Normalise a rule:{id} proposal — a self-contained dict, or a legacy plain string enriched from the task."""
    if isinstance(raw, dict):
        return {**raw, "task_id": task_id}
    t = store.get_task(task_id)
    sk = store.get_skill(t["skill_id"]) if t else None
    co = store.get_company(t["company_id"]) if t else None
    return {"task_id": task_id, "rule": raw, "skill_id": (t or {}).get("skill_id"),
            "skill_key": (sk or {}).get("skill_key"), "company_id": (t or {}).get("company_id"),
            "company": (co or {}).get("name"), "skill_name": (sk or {}).get("name"), "kind": (t or {}).get("kind")}


def pending_rule(task_id: int) -> dict:
    """Cockpit polls this after a correction: returns the rule the background inference proposed (if any)."""
    raw = db.setting_get(f"rule:{task_id}")
    p = _norm_proposal(task_id, raw) if raw else {}
    return {"ok": True, "proposed_rule": p.get("rule"),
            "skill_name": p.get("skill_name"), "company": p.get("company")}


def pending_rules() -> list[dict]:
    """EVERY un-decided rule proposal across all companies — so a taught rule can never be silently lost. The
    cockpit surfaces these as standalone 'confirm this rule' cards until the owner decides (company/universal/no)."""
    out = []
    for r in db.query("select key from settings where key like %s", ("rule:%",)):
        tid = r["key"].split(":", 1)[1]
        if not tid.isdigit():
            continue
        raw = db.setting_get(r["key"])
        if raw:
            p = _norm_proposal(int(tid), raw)
            if p.get("rule"):
                out.append(p)
    return out


def _on_message(msg: dict) -> None:
    text = msg["text"].strip()
    if text.startswith("/"):
        return
    pending = db.query("select * from tasks where status='awaiting_correction' order by updated_at desc limit 1")
    if not pending:
        return
    apply_correction(pending[0], text)


def apply_correction(task: dict, text: str) -> None:
    """Redraft a task from the owner's correction (works from Telegram OR the cockpit API)."""
    skill = store.get_skill(task["skill_id"])
    company = store.get_company(task["company_id"])
    store.reset_streak(skill["id"])   # the owner corrected a Manager-passed draft → streak breaks
    old = task.get("draft")
    site = _site_for(task, company)

    if site:
        from . import blog
        info = db.setting_get(f"wp:{task['id']}") or {}
        stored = db.setting_get(f"blog_build:{task['id']}") or {}
        c, imgs = stored.get("content"), stored.get("images")
        if c:   # SURGICAL revision: change ONLY what was asked, REUSE the same images, UPDATE the same WP post
            new_c = blog.revise_surgical(company["id"], c, text)
            art = blog.render(company["id"], new_c, imgs or {})
            db.setting_set(f"blog_build:{task['id']}", {"content": new_c, "images": imgs})
        else:   # legacy card with no stored content — fall back to a full redraft (old behaviour)
            art = worker.draft_article(skill, company, task["request"], correction=text)
        pid = info.get("post_id")
        if pid:
            r = site.update(pid, art["title"], art["html"])  # edit the EXISTING draft in place
            preview = r.get("preview") or info.get("preview")
        else:
            post = site.stage_draft(art["title"], art["html"])
            pid, preview = post["id"], post.get("preview")
        db.setting_set(f"wp:{task['id']}", {"post_id": pid, "preview": preview,
                                            "edit": info.get("edit"), "title": art["title"]})
        # the card body is READABLE text + the preview link — NEVER the raw post HTML (it was showing as code)
        task = store.update_task(task["id"],
                                 draft="Updated with your change. Open the preview link below to review the "
                                       "formatted post, then approve to schedule.",
                                 status="awaiting_approval", attempts=task["attempts"] + 1)
        store.log_decision(task["id"], skill["id"], "owner", "correct", note=text)
        msg2 = tg.send(_fmt_blog(company, skill, art, None, preview), _blog_buttons(task["id"]))
        store.update_task(task["id"], tg_message_id=msg2["message_id"])
        _maybe_propose_rule(task, skill, text, old or "", _html_to_text(art["html"]))   # text, not tags
        return

    if task["kind"] == "blog_idea":   # text-draft stage: SURGICALLY revise the FULL content (text), NO HTML/images
        from . import blog
        c = (task.get("request") or {}).get("content") or {}
        new_c = blog.revise_surgical(company["id"], c, text) if c else blog.compose(company["id"], text)
        new_text = blog.content_text(new_c)
        _r = task.get("request") or {}
        _keep = ({"attachments": _r["attachments"], "attachment_names": _r.get("attachment_names") or []}
                 if _r.get("attachments") else {})
        store.update_task(task["id"], title=(new_c.get("title") or task.get("title")), draft=new_text,
                          status="awaiting_approval",
                          request={"brief": _r.get("brief", ""), "content": new_c, **_keep},
                          attempts=task["attempts"] + 1)
        store.log_decision(task["id"], skill["id"], "owner", "correct", note=text, snapshot={"old": old, "new": new_text})
        _maybe_propose_rule(task, skill, text, old or "", new_text)
        return

    if task["kind"] == "newsletter_idea":   # ideation stage: refine the TEXT idea only; HTML build is a LATER stage
        new = provider.think(
            "You refine a NEWSLETTER IDEA at the ideation stage. Output ONLY a short, plain-text idea/concept "
            "(a few sentences: the suggested topic, the angle, and a CTA). NEVER write HTML, markup, or code, and do "
            "not build the email - this is just the idea; the full HTML build is a separate, later stage.",
            f"Company: {company['name']}.\nCurrent idea:\n{old}\n\nOperator's revision:\n{text}\n\n"
            "Rewrite the idea text to incorporate the revision. Plain text only.",
            model=worker._model_for(skill))   # respect the skill's model tier (Sonnet during the trial)
        store.update_task(task["id"], draft=new, status="awaiting_approval", attempts=task["attempts"] + 1)
        store.log_decision(task["id"], skill["id"], "owner", "correct", note=text, snapshot={"old": old, "new": new})
        _maybe_propose_rule(task, skill, text, old or "", new or "")   # learn from ideation feedback too
        return

    # "No email needed here" is valid feedback, not a revision request: dismiss the card instead of
    # stubbornly redrafting (bit card #330 — a tender that answers through the supplier portal), and
    # still run the rule inference so the owner gets the add-as-rule offer (universal or local).
    if task["kind"] in ("email_reply", "email_draft", "project_plan"):
        u = _understand_correction(task, text)                     # ONE reading of the owner's words
        if u.get("no_reply"):
            store.update_task(task["id"], status="rejected")
            store.log_decision(task["id"], skill["id"], "owner", "dismiss_no_reply", note=text,
                               snapshot={"old": old})
            if task.get("tg_message_id"):
                tg.edit(task["tg_message_id"], f"\u2717 Dismissed - no reply needed ('{skill['name']}').")
            _maybe_propose_rule(task, skill, text, old or "",
                                "(no email sent - the owner said no reply is needed)")
            return
        task, text = _apply_understood(task, u, text)              # every channel applied deterministically
        if task["kind"] != "project_plan":
            task = _prebook_meeting(task) or task   # a stamped meeting -> real Meet link for the drafter
    dreq = _request_for_draft(task)
    new = worker.draft(skill, company, dreq, correction=text, prev_draft=old)
    new = _ensure_clean_email(skill, company, dreq, new, prev=old)
    # corrections bypass the Manager by design (the owner is reviewing personally) — clear any verdict
    # from the PREVIOUS pass so a stale flag never scares the owner off his own corrected draft
    task = store.update_task(task["id"], draft=new, status="awaiting_approval", manager=None,
                             attempts=task["attempts"] + 1)
    _maybe_extract_meeting(task, new)
    store.log_decision(task["id"], skill["id"], "owner", "correct", note=text, snapshot={"old": old, "new": new})
    msg2 = tg.send(_fmt(task, skill, company, None), _approval_buttons(task["id"]))
    store.update_task(task["id"], tg_message_id=msg2["message_id"])
    _maybe_propose_rule(task, skill, text, old or "", new or "")


def _stamp_when(phrase: str):
    """CODE resolves the owner's time phrase (rebuild Stage 3: one date-stamper, deterministic first).
    Weekday names, 'tomorrow', 'today' and explicit dates never touch a model — a weekday always means
    the NEXT occurrence of that day. Anything else falls back to the shared LLM parser (still validated)."""
    tz = timezone(timedelta(hours=4))
    now = datetime.now(tz)
    ph = (phrase or "").strip().lower()
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, d in enumerate(days):
        if d in ph:
            ahead = (i - now.weekday()) % 7 or 7
            return (now + timedelta(days=ahead)).replace(hour=9, minute=0, second=0, microsecond=0)
    if "tomorrow" in ph:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    if "today" in ph:
        return now.replace(hour=min(now.hour + 2, 20), minute=0, second=0, microsecond=0)
    m = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+(january|february|march|april|may|june|july|august|"
                  r"september|october|november|december)", ph)
    if m:
        months = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
                  "october", "november", "december"]
        mo = months.index(m.group(2)) + 1
        yr = now.year + (1 if (mo, int(m.group(1))) < (now.month, now.day) else 0)
        try:
            return datetime(yr, mo, int(m.group(1)), 9, 0, tzinfo=tz)
        except ValueError:
            return None
    try:
        return reminders.parse_when(phrase)
    except Exception:  # noqa: BLE001
        return None


def _understand_correction(task: dict, text: str) -> dict:
    """ONE reading of the owner's correction (rebuild Stage 3), replacing four sequential model calls
    that could disagree. The model splits his words into channels; CODE applies every channel
    deterministically (real mailboxes, real library files, code-stamped dates)."""
    senders = _company_senders(task["company_id"])
    roster = ", ".join(f"{w} <{v['email']}>" for w, v in senders.items()) or "(none)"
    try:
        out = provider.think_json(
            worker._now_line() + " You read the OWNER'S spoken feedback on an email draft. It may mix "
            "several channels. Split it FAITHFULLY into JSON: "
            '{"no_reply": true only if he says NO email should be sent at all (dismiss it), '
            '"reply_instruction": "<everything that concerns the email reply content itself>", '
            f'"from": "<first name of a new sender if he asks to change who it is sent from; known team: {roster}>", '
            '"cc_add": ["<first names to add on cc>"], '
            '"cc_remove": ["<first names or exact emails to drop from cc>"], '
            '"attach_documents": ["<standing company documents he asks to attach, e.g. trade licence>"], '
            '"detach_documents": ["<attached documents he asks to REMOVE from the email, by name>"], '
            '"reminders": [{"title": "<self-contained>", "when_phrase": "<his EXACT time words verbatim, '
            "e.g. 'Monday morning', 'tomorrow', '31 August' - never resolve it yourself\"}], "
            '"prep": [{"title": "<short imperative>", "brief": "<internal work to prepare, incl. stated '
            'deadline>"}]} '
            "prep is ONLY for work outside this email (build a document, research something). Changes to "
            "THIS email or card — its attachments, cc, sender, content — always go through their own "
            "channels above, NEVER into prep. "
            "- empty/false for anything not asked. Never invent dates or names.",
            (text or "")[:1200], model=provider.MODEL_FAST, max_tokens=700,
            purpose="understand-correction",   # Sonnet, owner-approved 2026-08-30: reading his mixed
            # spoken corrections is interpretation, not plumbing — Haiku repeatedly misread it
            company=(store.get_company(task["company_id"]) or {}).get("slug"))
        return out if isinstance(out, dict) else {}
    except Exception:  # noqa: BLE001 - on failure the whole text goes to the drafter, old behaviour
        return {}


def _apply_understood(task: dict, u: dict, text: str) -> tuple:
    """Deterministically apply every non-reply channel of an understood correction; returns the (possibly
    refreshed) task and the text the DRAFTER should receive."""
    req = dict(task.get("request") or {})
    changed, created = False, []
    senders = _company_senders(task["company_id"])
    frm = (u.get("from") or "").strip().lower()
    if frm and frm in senders:
        req["from_email"], req["mailbox_rt"] = senders[frm]["email"], senders[frm]["rt_key"]
        if req.get("thread"):
            req["thread"] = {**req["thread"], "id": ""}
        changed = True
    adds = [senders[c.strip().lower()]["email"] for c in (u.get("cc_add") or [])
            if isinstance(c, str) and c.strip().lower() in senders]
    if adds:
        req["cc_extra"] = sorted(set((req.get("cc_extra") or []) + adds))
        changed = True
    rems = []
    for c in (u.get("cc_remove") or []):
        if isinstance(c, str):
            c = c.strip().lower()
            rems.append(senders[c]["email"] if c in senders else (c if "@" in c else None))
    rems = [r for r in rems if r]
    if rems:
        req["cc_remove"] = sorted(set((req.get("cc_remove") or []) + rems))
        req["cc_extra"] = [e for e in (req.get("cc_extra") or []) if e.lower() not in set(rems)]
        changed = True
    missing = []
    refs = {int(r["id"]): r for r in (req.get("attach_docs") or [])}
    n_refs0 = len(refs)
    _scope = ""
    try:   # a deal-linked card may ONLY attach that project's documents (see documents.find)
        _did = task.get("deal_id") or req.get("deal_id")
        if _did:
            _d = db.one("select title, company from crm_projects where id=%s", (int(_did),))
            _scope = (_d or {}).get("title") or ""
    except Exception:  # noqa: BLE001
        _scope = ""
    for w in (u.get("attach_documents") or [])[:6]:
        hits = documents.find(task["company_id"], str(w), scope=_scope)
        if hits:
            d = hits[0]
            refs[d["id"]] = documents.card_ref(d)
        else:
            missing.append(str(w))
    for w in (u.get("detach_documents") or [])[:6]:   # remove-attachment verb (card 383: it didn't exist,
        wl = str(w).lower()                            # so 'remove the hero loop PDF' could never apply)
        drop = [rid for rid, r in refs.items()
                if wl in (r.get("filename") or "").lower()
                or all(tok in (r.get("filename") or "").lower() for tok in wl.split() if len(tok) > 3)]
        for rid in drop:
            created.append(f"detached '{refs.pop(rid)['filename']}'")
    if len(refs) != n_refs0 or any(c.startswith("detached") for c in created):
        req["attach_docs"] = list(refs.values())
        changed = True
    for r in (u.get("reminders") or [])[:3]:
        try:
            d = _stamp_when(str(r.get("when_phrase") or r.get("date") or ""))
            if d and datetime.now(timezone.utc) < d < datetime.now(timezone.utc) + timedelta(days=90):
                reminders.create(str(r.get("title") or "Follow-up")[:150], d, company_id=task["company_id"],
                                 priority="high", target_type="deal" if req.get("deal_id") else None,
                                 target_id=req.get("deal_id"))
                created.append(f"reminder '{r.get('title')}' on {d:%a %d %b}")
        except Exception:  # noqa: BLE001
            continue
    for p_ in (u.get("prep") or [])[:3]:
        try:
            sk = store.get_skill_by_key(task["company_id"], "sales-quotation") \
                or store.get_skill_by_key(task["company_id"], "sales-first-response")
            store.create_card(task["company_id"], sk["id"], "content",
                              {"brief": "INTERNAL PREP (from the owner's instruction on card "
                                        f"#{task['id']}): {p_.get('brief') or p_.get('title')}",
                               "title": p_.get("title"),
                               **({"deal_id": req["deal_id"]} if req.get("deal_id") else {})},
                              deal_id=req.get("deal_id"),
                              contact=None if req.get("deal_id") else (req.get("inquiry") or {}).get("email"))
            created.append(f"prep card '{p_.get('title')}'")
        except Exception:  # noqa: BLE001
            continue
    notes = []
    if created:
        notes.append("set: " + "; ".join(created))
    if missing:
        notes.append("NOT in the document library (nothing attached): " + ", ".join(missing))
    if created:   # the receipt belongs ON THE CARD, not in a push notification (owner, 30 Aug: routine
        # confirmations of his own instructions are noise - the reminders are visible on the deal)
        req["correction_actions"] = (req.get("correction_actions") or []) + created
        changed = True
    if missing:   # this one is NOT routine: he believes a document is attached and it is not
        notifications.notify(
            f"Card #{task['id']}: NOT in the document library, so nothing was attached - "
            + ", ".join(missing) + ".", "Document not found", category="reminder",
            company_id=task["company_id"], target_type="task", target_id=str(task["id"]))
    if changed:
        task = store.update_task(task["id"], request=req) or task
    reply = (u.get("reply_instruction") or "").strip()
    return task, (reply if (reply and (created or changed)) else text)


def _thread_participants(msg: dict, to_email: str) -> list:
    """The OTHER people already on this email thread (their side + any third parties), so continuing a
    conversation keeps everyone who was on it. Our own team is excluded here - the company's always_cc
    puts them on every email anyway. Never includes the To recipient or automated addresses."""
    from .identity import OWN_COMPANY_DOMAINS
    out, seen = [], {(to_email or "").lower()}
    blob = " ".join([str(msg.get("to") or ""), str(msg.get("cc") or ""), str(msg.get("from") or "")])
    for a in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", blob):
        al = a.lower().strip(".")
        dom = al.split("@")[-1]
        if al in seen or dom in OWN_COMPANY_DOMAINS:
            continue
        if any(k in al for k in ("noreply", "no-reply", "mailer-daemon", "notifications@", "calendar-",
                                 "postmaster", "bounce")):
            continue
        seen.add(al)
        out.append(al)
    return out[:12]


def _company_senders(company_id: int) -> dict:
    """The real people mailboxes this company can send from: {name-or-local: {email, rt_key}}.
    Built from the per-person gmail_account:<slug>:<who> settings — never a catch-all."""
    co = store.get_company(company_id) or {}
    slug = co.get("slug") or ""
    out = {}
    for r in db.query("select key, value from settings where key like %s", (f"gmail_account:{slug}:%",)):
        who = r["key"].split(":")[-1]
        email = (r["value"] if isinstance(r["value"], str) else str(r["value"])).strip('" ')
        rt = f"gmail_refresh_token:{slug}:{who}"
        if email and db.setting_get(rt):
            out[who.lower()] = {"email": email.lower(), "rt_key": rt}
    # spoken-name aliases (voice-to-text renders Rashad as 'Richard'); config so new quirks are data
    for alias, real in (db.setting_get("sender_aliases") or {"richard": "rashad"}).items():
        if real in out and alias not in out:
            out[alias] = out[real]
    return out


def _prebook_meeting(task: dict) -> dict | None:
    """Book the stamped meeting NOW but attendee-less: the event sits on our own calendar with a real
    Google Meet room, so the draft can carry the genuine link for the owner to see. The client is only
    added (and invited by Google) when the owner APPROVES — nothing outward happens before that."""
    try:
        req = dict(task.get("request") or {})
        mt = req.get("meeting")
        if not mt or mt.get("event_id"):
            return None
        from . import calendar as gcal
        co = store.get_company(task["company_id"]) or {}
        slug = co.get("slug") or ""
        # CROSS-BRAND GUARD: a company with no calendar of its own must NOT be booked onto another
        # brand's. A Tabscanner client would have received a Sensa Productions invite from
        # hello@sensa.digital with a Sensa Meet link (owner caught this on card 407, 31 Aug 2026).
        if not db.setting_get(f"calendar_refresh_token:{slug}"):
            notifications.notify(
                f"{co.get('name')} has no calendar connected, so the meeting on card #{task['id']} was "
                "NOT booked and the email carries no link. Connect a calendar for this company (or say "
                "which calendar it should use) and approve again.",
                "Meeting not booked - no calendar", priority="high", category="reminder",
                company_id=task["company_id"], target_type="task", target_id=str(task["id"]))
            return None
        cal_co = slug
        ev = gcal.create_event(cal_co, start=datetime.fromisoformat(mt["start"]),
                               minutes=int(mt.get("minutes") or 30),
                               summary=mt.get("summary") or "Call",
                               description=f"Pre-booked via Cortex (task #{task['id']}); "
                                           "guest added on the owner's approval.", meet=True)
        req["meeting"] = {**mt, "event_id": ev.get("id"), "meet": ev.get("meet") or "", "calendar": cal_co}
        return store.update_task(task["id"], request=req)
    except Exception:  # noqa: BLE001 — pre-booking is best-effort; the link then arrives at send instead
        return None


# A draft that states a clock time is the only one worth asking about. (Deleted by mistake in the
# 29 Aug rebuild while its only use stayed - _maybe_extract_meeting then raised NameError into a bare
# except, so NO meeting was booked from any card between 29 and 31 Aug 2026.)
_TIME_HINT = re.compile(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b|\b\d{1,2}:\d{2}\b", re.I)


def _maybe_extract_meeting(task: dict, draft: str) -> None:
    """When a reply draft CONFIRMS one specific meeting slot with the client, stamp request.meeting so
    the approval that sends the email ALSO books the calendar event + Google Meet (link appended to the
    email at send). Haiku only reads what the draft states; code stamps and validates the datetime."""
    try:
        req = dict(task.get("request") or {})
        if task.get("kind") != "email_reply" or req.get("meeting") or not _TIME_HINT.search(draft or ""):
            return
        out = provider.think_json(
            worker._now_line() + " Does this email CONFIRM a specific meeting day and time with the "
            "recipient (agreed, not merely proposing options)? Report the time EXACTLY AS WRITTEN and "
            "name its timezone - never convert it yourself. Return JSON {\"confirmed\": true|false, "
            "\"start_local\": \"YYYY-MM-DDTHH:MM\" as stated in the email, "
            "\"tz\": \"<IANA zone for the timezone the email states, e.g. Europe/Amsterdam, "
            "America/New_York, Asia/Dubai; use Asia/Dubai ONLY if no other timezone is stated>\", "
            "\"minutes\": 30, \"summary\": \"short meeting title naming the counterpart\"} — "
            "confirmed:false unless ONE exact slot is clearly agreed in the text.",
            (draft or "")[:1500], model=provider.MODEL_ROUTER, purpose="meeting-extract",
            company=(store.get_company(task["company_id"]) or {}).get("slug"))
        if not (isinstance(out, dict) and out.get("confirmed")
                and (out.get("start_local") or out.get("start_iso"))):
            return
        s = str(out.get("start_local") or out.get("start_iso"))
        # CODE converts the timezone (never the model): '11am Amsterdam' is 13:00 in Dubai, and a
        # two-hour error books the client into the wrong slot.
        if "+" in s or s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            _tz = str(out.get("tz") or "Asia/Dubai").strip() or "Asia/Dubai"
            try:
                from zoneinfo import ZoneInfo
                dt = datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(_tz))
            except Exception:  # noqa: BLE001 — unknown zone: fall back to Dubai local
                dt = datetime.fromisoformat(s + "+04:00")
        if dt < datetime.now(timezone.utc) or dt > datetime.now(timezone.utc) + timedelta(days=180):
            return                                     # implausible stamp -> no booking, never a wrong one
        # the SAME slot with the SAME contact may already be booked from an earlier card — reuse that
        # event and its Meet link instead of double-booking (Sunwoo got two invites, 2026-08-27)
        prior = db.one("select request->'meeting' m from tasks where company_id=%s and "
                       "lower(request->'inquiry'->>'email')=lower(%s) and "
                       "request->'meeting'->>'event_id' is not null and request->'meeting'->>'start'=%s "
                       "order by id desc limit 1",
                       (task["company_id"], (req.get("inquiry") or {}).get("email") or "", dt.isoformat()))
        if prior and prior.get("m"):
            req["meeting"] = prior["m"]
        else:
            req["meeting"] = {"start": dt.isoformat(), "minutes": int(out.get("minutes") or 30),
                              "summary": (out.get("summary") or "Intro call")[:80]}
        store.update_task(task["id"], request=req)
    except Exception:  # noqa: BLE001 — booking is a bonus; the draft must never fail because of it
        pass


def reconcile_attachments(task_id: int) -> None:
    """A document was attached to (or removed from) a card that already has a draft: revise the draft
    OFF-THREAD so the wording matches reality ('please find attached' vs 'we will send'), preserving
    everything else. System reconcile — no owner decision logged, manager untouched."""
    def _run():
        try:
            t = store.get_task(task_id)
            if not t or not (t.get("draft") or "").strip() or t.get("status") not in (
                    "awaiting_approval", "awaiting_correction", "new"):
                return
            skill, company = store.get_skill(t["skill_id"]), store.get_company(t["company_id"])
            names = [r.get("filename") for r in ((t.get("request") or {}).get("attach_docs") or [])]
            note = ("(system note, not from the owner) The attachments on this email JUST CHANGED. Files now "
                    "genuinely attached: " + (", ".join(n for n in names if n) or "(none)") + ". Revise ONLY "
                    "the wording that references sending/attaching documents so it matches — reference "
                    "attached files as attached, never as coming later; keep every other sentence as it is.")
            new = worker.draft(skill, company, _request_for_draft(t), correction=note,
                               prev_draft=t.get("draft"))
            store.update_task(task_id, draft=new)
        except Exception:  # noqa: BLE001 — reconcile is a bonus pass; the card stays usable without it
            pass
    threading.Thread(target=_run, daemon=True).start()


_META_LEAK = re.compile(r"\bCORTEX\b\s*[,:]|OWNER TO CONFIRM|\bactions? to set\b|\[internal\b", re.I)


def _ensure_clean_email(skill: dict, company: dict, dreq: dict, draft: str,
                        prev: str | None = None) -> str:
    """HARD output check for email drafts: system/meta text addressed to Cortex or the owner must never
    appear in a client email. One automatic retry with explicit feedback; the send-layer placeholder
    guard remains the final backstop."""
    try:
        if draft and _META_LEAK.search(draft):
            draft = worker.draft(skill, company, dreq, prev_draft=prev, manager_feedback=[
                "Your output contained system/meta text addressed to Cortex or the owner (e.g. 'CORTEX, "
                "two actions to set', 'OWNER TO CONFIRM'). The email body must contain ONLY the message "
                "the client reads. Remove every non-client line; reminders and internal work are handled "
                "by the system, never written into the email."])
    except Exception:  # noqa: BLE001
        pass
    return _ensure_real_links(skill, company, dreq, draft)


_URL_RX = re.compile(r"https?://[^\s<>\")\]]+")


def _ensure_real_links(skill: dict, company: dict, dreq: dict, draft: str) -> str:
    """HARD invented-link check: every URL in an email draft must exist somewhere in the request context
    the drafter was served (media library shelf, thread history, meeting link, brief, notes) or belong to
    the company's own site. A model must never mint a must-be-real value — card 384 shipped two invented
    'library' links. One retry naming the offenders; the Manager gets the same allowlist as a backstop."""
    try:
        found = _URL_RX.findall(draft or "")
        if not found:
            return draft
        ctx = json.dumps(dreq, default=str)
        site = ((company.get("data") or {}).get("website") or "") if isinstance(company, dict) else ""
        bad = [u for u in found
               if u.rstrip(".,;") not in ctx
               and not (site and u.startswith(site.rstrip("/")))]
        if bad:
            return worker.draft(skill, company, dreq, prev_draft=draft, manager_feedback=[
                "Your draft contains links that DO NOT EXIST: " + ", ".join(bad[:5]) + ". You may only "
                "use links given to you in this request (the MEDIA LIBRARY list, the conversation "
                "history, a booked meeting link). Rewrite using only real links, or share no link at "
                "all and offer to send samples."])
    except Exception:  # noqa: BLE001
        pass
    return draft


def _confirm_rule(task: dict, skill: dict, yes: bool, universal: bool = False) -> None:
    raw = db.setting_get(f"rule:{task['id']}")
    rt = raw.get("rule") if isinstance(raw, dict) else raw
    if yes and rt:
        if universal:
            store.add_universal_rule(skill["skill_key"], rt)
            where = "ALL companies (universal)"
        else:
            store.add_rule(skill["id"], rt)
            where = f"'{skill['name']}'"
        store.log_decision(task["id"], skill["id"], "owner", "rule_confirmed",
                           note=("[universal] " if universal else "") + rt)
        tg.send(f"Added to {where}: “{rt}”. I'll follow it from now on.")
    else:
        tg.send("Okay — not adding a rule.")
    db.setting_set(f"rule:{task['id']}", None)


# ---------- programmatic actions (cockpit API surface) ----------

def _load(task_id: int):
    task = store.get_task(task_id)
    if not task:
        return None, None, None
    return task, store.get_skill(task["skill_id"]), store.get_company(task["company_id"])


def approve_task(task_id: int, stepup_token: str | None = None, run_at: str | None = None) -> dict:
    task, skill, company = _load(task_id)
    if not task:
        return {"ok": False, "error": "no such task"}
    if task["status"] not in ("awaiting_approval", "awaiting_correction"):
        return {"ok": False, "error": f"task is '{task['status']}', not awaiting approval"}
    # SAFEGUARD: a newsletter that touches the real list NEVER goes on a plain approve. Stage 2
    # (newsletter_review) SCHEDULES for the 1st; Stage 3 (newsletter_send) SENDS. Both require the
    # operator to echo the exact recipient count first.
    if task["kind"] == "newsletter_idea":
        # the test send goes to real people (your reviewers) — surface exactly WHO, and require the same
        # count-confirm + PIN/fingerprint as any outbound send so a plain approve never fires it.
        g = newsletter.test_group(task["company_id"])
        return {"ok": False, "needs_confirm": True, "action": "test", "company": company["name"],
                "recipients": len(g), "to": [{"email": x["email"], "name": x.get("name")} for x in g]}
    if task["kind"] in ("newsletter_review", "newsletter_send"):
        n = len(newsletter.recipients(task["company_id"]))
        action = "schedule" if task["kind"] == "newsletter_review" else "send"
        info = {"ok": False, "needs_confirm": True, "recipients": n, "company": company["name"], "action": action}
        if action == "schedule":
            info["date"] = _next_newsletter_slot(company["id"]).strftime("%-d %b %Y")
        return info
    if task["kind"] in EMAIL_RENDER_KINDS:
        # A recipient-less email card must never look approvable. Card 398 was composed through the
        # generic task path (no inquiry block), so it sat in the Inbox with an empty To and the reply
        # fallback subject 'Re: your enquiry' (31 Aug 2026).
        _to = ((task.get("request") or {}).get("inquiry") or {}).get("email") or ""
        if "@" not in _to:
            return {"ok": False, "blocked": True,
                    "error": "this card has NO recipient — tell me who it goes to and I'll set it, "
                             "nothing can send until then"}
    gate = _biometric_gate(task["kind"] in _APPROVE_PUBLIC, stepup_token,
                           money=(kind_class(task["kind"]) == "money"))
    if gate:
        return gate
    if run_at:
        # APPROVE AND SCHEDULE (owner, 30 Aug): he authenticates NOW and the send fires later. The card
        # is parked as a one-off scheduled task carrying an approved-send marker, so the clock EXECUTES
        # it (never re-drafts). Everything he approved - draft, envelope, attachments - is frozen as is.
        try:
            when = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=_GST)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "could not read that date/time"}
        if when <= datetime.now(timezone.utc):
            return {"ok": False, "error": "that time is in the past"}
        db.execute("update tasks set status='scheduled', schedule_kind='once', run_at=%s, enabled=true, "
                   "request = request || '{\"approved_send\": true}'::jsonb, updated_at=now() where id=%s",
                   (when, task_id))
        store.log_decision(task_id, skill["id"], "owner", "approved_scheduled",
                           note=when.astimezone(_GST).strftime("%a %-d %b %H:%M"))
        return {"ok": True, "scheduled": when.astimezone(_GST).strftime("%a %-d %b, %H:%M"),
                "task": store.get_task(task_id)}
    result = _approve(task, skill, company, stepped_up=True)
    return {"ok": True, "task": store.get_task(task_id), "result": result}


def confirm_send_task(task_id: int, count: int, stepup_token: str | None = None) -> dict:
    """Confirm a newsletter with the EXACT recipient count. Stage 2 (newsletter_review) -> SCHEDULE for the
    next free 1st; Stage 3 (newsletter_send) -> SEND now (drip). Count must match, so a misclick can't fire."""
    task, skill, company = _load(task_id)
    if not task or task["kind"] not in ("newsletter_idea", "newsletter_review", "newsletter_send"):
        return {"ok": False, "error": "not a newsletter card"}
    is_test = task["kind"] == "newsletter_idea"
    n = (len(newsletter.test_group(task["company_id"])) if is_test
         else len(newsletter.recipients(task["company_id"])))
    try:
        if int(count) != n:
            label = "test group" if is_test else "list"
            return {"ok": False, "error": f"Count mismatch: you entered {count}, the {label} is {n}. Nothing done."}
    except (TypeError, ValueError):
        return {"ok": False, "error": "Enter the exact recipient count to confirm."}
    gate = _biometric_gate(True, stepup_token)   # every outward newsletter send is a public action
    if gate:
        return gate
    if is_test:   # build the issue + send the test to the (now-confirmed) reviewers + drop the review card
        return {"ok": True, "result": newsletter.execute_idea_approval(task, skill, company, "owner")}
    art = db.setting_get(f"newsletter:{task_id}")
    if not art:
        return {"ok": False, "error": "no built newsletter found for this card"}
    if task["kind"] == "newsletter_review":
        return _schedule_newsletter(task, skill, company, art, n)
    return _dispatch_newsletter(task, skill, company, art, n)


def _next_newsletter_slot(company_id: int, hour: int = 9) -> datetime:
    """Next free monthly newsletter slot on the company's publishing day; stacks one issue per month.
    Newsletters follow the same per-company publishing day as blogs (delegates to the universal queue)."""
    return contentqueue.next_slot(company_id, "newsletter_scheduled", hour)


def _schedule_newsletter(task, skill, company, art, n) -> dict:
    """Stage 2 confirm: put the approved issue on the calendar as a one-off task for the next free 1st-of-month."""
    slot = _next_newsletter_slot(company["id"])
    t = db.execute(
        "insert into tasks (company_id,skill_id,kind,request,status,origin,title,schedule_kind,run_at,enabled) "
        "values (%s,%s,'newsletter_scheduled',%s,'scheduled','calendar',%s,'once',%s,true) returning *",
        (company["id"], skill["id"], Json({"subject": art["subject"], "review_task_id": task["id"]}),
         art["subject"], slot))
    db.setting_set(f"newsletter:{t['id']}", art)   # the built issue, keyed by the NEW scheduled task
    store.update_task(task["id"], status="done")
    store.log_decision(task["id"], skill["id"], "owner", "newsletter_scheduled",
                       note=art["subject"], snapshot={"when": slot.isoformat(), "recipients": n})
    db.setting_set(f"newsletter:{task['id']}", None)
    when = slot.strftime("%-d %b %Y")
    return {"ok": True, "result": {"scheduled": when,
                                   "sent_to": f"scheduled for {when} ({n:,} contacts) - now on the calendar"}}


def _publish_day(company_id: int) -> int:
    """The company's fixed monthly publishing day (1-28). Canonical home is contentqueue (shared by every
    content type); kept here as a thin alias for existing callers."""
    return contentqueue.publish_day(company_id)


def _next_blog_slot(company_id: int, hour: int = 9) -> datetime:
    """Next free monthly blog slot on the company's publishing day (delegates to the universal queue)."""
    return contentqueue.next_slot(company_id, "blog_scheduled", hour)


def _schedule_blog(task: dict, skill: dict, company: dict, actor: str) -> dict:
    """Approve a blog -> QUEUE it to publish on the company's next monthly publishing day (instead of going
    live now). It shows on the calendar 'Upcoming' and auto-publishes on the date via promote_due_tasks."""
    info = db.setting_get(f"wp:{task['id']}") or {}
    slot = _next_blog_slot(company["id"])
    db.execute("update tasks set kind='blog_scheduled', status='scheduled', schedule_kind='once', "
               "origin='calendar', run_at=%s, title=%s, enabled=true where id=%s",
               (slot, info.get("title") or "Blog post", task["id"]))
    store.log_decision(task["id"], skill["id"], actor, "blog_scheduled",
                       note=info.get("title"), snapshot={"post_id": info.get("post_id"), "when": slot.isoformat()})
    when = slot.strftime("%-d %b %Y")
    tg.send(f"[{company['name']}] blog '{info.get('title','')}' queued to publish {when} — now on the calendar.")
    return {"scheduled": when, "title": info.get("title")}


def bump_blog_to_front(task_id: int) -> dict:
    """Bump a queued item to the front of its company's queue (others slide back a month). Now generic over
    every content kind — kept under this name for existing callers; delegates to the universal queue."""
    return contentqueue.bump_to_front(task_id)


def _dispatch_newsletter(task, skill, company, art, n) -> dict:
    """Stage 3 confirm: actually send (throttled drip). Counts toward earned-auto (offer at 5)."""
    cid = company["id"]
    recips = newsletter.recipients(cid)
    per_hour = int(db.setting_get("newsletter_per_hour") or newsletter.DEFAULT_PER_HOUR)
    jid = newsletter.enqueue_send(cid, task["id"], art, recips, per_hour)
    store.update_task(task["id"], status="done")
    db.setting_set(f"newsletter:{task['id']}", None)
    streak = int(db.setting_get(f"nl_streak:{cid}") or 0) + 1
    db.setting_set(f"nl_streak:{cid}", streak)
    if streak >= 5 and not db.setting_get(f"nl_auto:{cid}"):
        tg.send(f"You've confirmed {streak} monthly sends for {company['name']}. Put the monthly newsletter "
                f"send on AUTO (skip the Stage-3 confirm from now on)?",
                [[tg.button("Yes, auto", f"nlauto:{cid}:1"), tg.button("Keep confirming", f"nlauto:{cid}:0")]])
    hrs = round(len(recips) / per_hour, 1)
    return {"ok": True, "result": {"sent_to": f"the live list, drip {per_hour}/hour (~{hrs}h for {n:,})",
                                   "queued": True, "job": jid, "streak": streak}}


def set_newsletter_paused(paused: bool) -> dict:
    """Emergency stop for ALL newsletter sending: pauses in-flight drips and blocks scheduled/auto sends."""
    db.setting_set("newsletter_paused", bool(paused))
    tg.send(f"⚠️ Newsletter sending is now {'PAUSED' if paused else 'resumed'}.")
    return {"ok": True, "paused": bool(paused)}


def set_newsletter_auto(company_id: int, on: bool) -> dict:
    db.setting_set(f"nl_auto:{company_id}", bool(on))
    return {"ok": True, "auto": bool(on)}


def newsletter_status() -> dict:
    return {"ok": True, "paused": bool(db.setting_get("newsletter_paused"))}


def set_email_sending_paused(paused: bool) -> dict:
    """Emergency stop for ALL outbound Gmail email (replies from official addresses). Enforced at
    gmail.send_message, so nothing sends while paused."""
    db.setting_set("email_sending_paused", bool(paused))
    tg.send(f"⚠️ Outbound email sending is now {'PAUSED' if paused else 'resumed'}.")
    return {"ok": True, "paused": bool(paused)}


def email_status() -> dict:
    return {"ok": True, "paused": bool(db.setting_get("email_sending_paused"))}


def skip_task(task_id: int) -> dict:
    task, skill, company = _load(task_id)
    if not task:
        return {"ok": False, "error": "no such task"}
    _skip(task, skill, company)
    return {"ok": True, "task": store.get_task(task_id)}


def correct_task(task_id: int, text: str) -> dict:
    task, skill, company = _load(task_id)
    if not task:
        return {"ok": False, "error": "no such task"}
    apply_correction(task, text)
    proposed = db.setting_get(f"rule:{task_id}")   # _maybe_propose_rule stows the inferred rule here
    return {"ok": True, "task": store.get_task(task_id),
            "proposed_rule": proposed, "skill_name": skill["name"] if skill else None,
            "company": company["name"] if company else None}


def decide_rule(task_id: int, add: bool, scope: str = "company") -> dict:
    """Cockpit confirm/dismiss of the rule Cortex inferred from a correction. `scope` is the owner's
    explicit choice: 'company' (this company's skill only) or 'universal' (that skill_key on EVERY company)."""
    raw = db.setting_get(f"rule:{task_id}")
    added = False
    if add and raw:
        p = _norm_proposal(task_id, raw)
        rule, skill_key, skill_id = p.get("rule"), p.get("skill_key"), p.get("skill_id")
        if rule and scope == "universal" and skill_key:
            store.add_universal_rule(skill_key, rule)
            added = True
        elif rule and skill_id:
            store.add_rule(skill_id, rule)
            added = True
        if added:
            try:
                store.log_decision(task_id, skill_id, "owner", "rule_confirmed",
                                   note=("[universal] " if scope == "universal" else "") + rule)
            except Exception:  # noqa: BLE001 — the originating task may be gone; the rule still lands
                pass
    db.setting_set(f"rule:{task_id}", None)
    return {"ok": True, "added": added, "scope": scope}


# ---------- auto-intake: pull new enquiries from Gmail, draft a reply for each ----------

def poll_inquiries() -> dict:
    """The automatic intake (recent window), called on the engine loop."""
    if not EMAIL_ENQUIRY_FALLBACK:                       # sites POST enquiries direct now; email poller off
        return {"made": 0, "filtered": 0, "reason": "direct-webhook"}
    return poll_inquiries_window(days=2)


def triage_inquiry(inq: dict, company_slug: str = "tabscanner") -> dict:
    """Decide if an enquiry is a genuine potential customer/partner worth a reply, or junk (spam, bots,
    gibberish, off-topic, SEO/link-building pitches). Company-aware: each brand triages in its own context."""
    co = store.get_company_by_slug(company_slug)
    ctx = worker._company_context(co) if co else "Tabscanner, a receipt-OCR / data-extraction API."
    try:
        out = provider.think_json(
            "You triage inbound website enquiries for this company.\n" + ctx + "\n\n"
            "Decide if an enquiry is a GENUINE potential customer, partner, or support contact worth a human "
            "reply, or JUNK. Be strict: random/gibberish sender addresses, mismatched names, off-topic "
            "messages, SEO / marketing / link-building / web-design solicitations, and obvious bot spam are JUNK.",
            f"From: {inq.get('name')} <{inq.get('email')}>\nSubject: {inq.get('subject')}\n"
            f"Message:\n{(inq.get('message') or inq.get('snippet') or '').strip()}\n\n"
            'Return JSON: {"genuine": boolean, "category": "lead|partner|support|spam|offtopic|unclear", '
            '"reason": "short phrase"}',
            model=provider.MODEL_ROUTER, purpose="triage", company=company_slug)
        return {"genuine": bool(out.get("genuine")), "category": out.get("category") or "unclear",
                "reason": (out.get("reason") or "").strip()}
    except Exception:  # noqa: BLE001
        return {"genuine": True, "category": "unclear", "reason": "triage unavailable — defaulting to review"}


def poll_inquiries_window(days: int = 2) -> dict:
    """Pull new Tabscanner enquiries, triage out the spam, and for each GENUINE one add the contact to the
    CRM + queue a drafted reply. Deduped by Gmail id so each enquiry is handled exactly once."""
    if not gmail.connected():
        return {"made": 0, "filtered": 0, "reason": "gmail-not-connected"}
    try:
        inqs = gmail.list_inquiries(days=days)
    except Exception as e:  # noqa: BLE001
        return {"made": 0, "filtered": 0, "reason": f"list-failed: {e}"}
    co = store.get_company_by_slug("tabscanner")
    skill = store.get_skill_by_key(co["id"], "sales-first-response") if co else None
    if not (co and skill):
        return {"made": 0, "filtered": 0, "reason": "tabscanner sales-first-response skill missing"}
    seen_list = list(db.setting_get("gmail_processed") or []); seen = set(seen_list)
    filtered_log = db.setting_get("gmail_filtered") or []
    made, filtered = 0, 0
    for inq in inqs:
        gid = inq.get("gmail_id")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        verdict = triage_inquiry(inq)
        if not verdict["genuine"]:                       # spam/junk: filed, NOT drafted, NOT added to CRM
            filtered_log.append({"name": inq.get("name"), "email": inq.get("email"),
                                 "category": verdict["category"], "reason": verdict["reason"]})
            filtered += 1
            continue
        try:
            crm.add_inquiry(inq, "tabscanner")           # genuine -> verified contact in the CRM
        except Exception:  # noqa: BLE001
            pass
        store.create_card(co["id"], skill["id"], "email_reply",
                          {"brief": _email_brief(inq, co), "inquiry": inq, "triage": verdict},
                          contact=inq.get("email"))
        made += 1
        if made >= 10:
            break
    db.setting_set("gmail_processed", (seen_list + [g for g in seen if g not in set(seen_list)])[-1000:])
    if filtered:
        db.setting_set("gmail_filtered", filtered_log[-200:])
    if made:
        tg.send(f"{made} genuine Tabscanner enquir{'ies' if made > 1 else 'y'} in"
                + (f" ({filtered} spam filtered out)" if filtered else "")
                + f" — drafting {'replies' if made > 1 else 'a reply'} for your approval.")
    return {"made": made, "filtered": filtered}


# ---------- generic contact-form intake (per-company, config-driven) ----------
# Some brands' website contact forms only EMAIL the catch-all inbox (no webhook). This reads those
# notification emails, parses the lead, triages spam, and routes genuine ones -> CRM + a drafted reply,
# exactly like Tabscanner's enquiry flow. Add a brand by adding a line to FORM_INTAKE.
# NOTE: all five sites now POST enquiries straight to /api/intake/enquiry (direct webhook), which triages +
# captures via the SAME intake_enquiry() core. The email pollers below are OFF (flag) to avoid double-capture;
# flip EMAIL_ENQUIRY_FALLBACK back on to use them as an email backstop.
EMAIL_ENQUIRY_FALLBACK = False
FORM_INTAKE = {
    "snaprewards": {"rt_key": "gmail_refresh_token:snaprewards", "client": "snaprewards",
                    "subject": "Site contact form", "skill": "sales-first-response"},
    "skyvision": {"rt_key": "gmail_refresh_token:skyvision", "client": "skyvision",
                  "subject": "New enquiry from", "skill": "sales-first-response"},
    # Sensa: auto-draft ON (2026-08-22) — the team (Rashad/Gino/Ayresh) approves from the Inbox.
    "sensa": {"rt_key": "gmail_refresh_token:sensa", "client": "sensa",
              "subject": "New enquiry from", "skill": "sales-first-response", "draft": True},
}


def _form_field(body: str, label: str) -> str:
    m = re.search(rf"^\s*{label}\s*:\s*(.+)$", body, re.I | re.M)
    return m.group(1).strip() if m else ""


def _parse_form_email(e: dict) -> dict:
    """Pull the lead out of a 'Name:/Email:/Phone:/Message:' contact-form notification email."""
    body = e.get("body") or e.get("snippet") or ""
    m = re.search(r"(?is)\bMessage\s*:\s*(.+?)(?:\n--\s|\Z)", body)
    return {"gmail_id": e.get("gmail_id"), "name": _form_field(body, "Name") or e.get("name"),
            "email": _form_field(body, "Email"), "phone": _form_field(body, "Phone"),
            "subject": e.get("subject"), "message": (m.group(1).strip() if m else body.strip())}


_FREE_EMAIL_DOMAINS = crm.FREE_EMAIL   # single definition (crm.py) — the domain-deal fallback uses it too


def qualify_suggest(co: dict, inq: dict) -> dict | None:
    """Run the company's `lead-qualification` skill over ONE genuine enquiry and return a suggested verdict
    {verdict, confidence, reason, bucket}. The SKILL RULES govern the judgement (dumb-waiter doctrine); the
    sender's domain is a supporting signal only. For a corporate but unfamiliar domain the model gets live web
    search so it can judge the company's standing. `bucket` is the handling bucket the rules assign (self_serve /
    conversation / strategic, '' if the rules define none) — strategic flags the owner for manual takeover.
    The owner still makes the final call (Qualify / Not qualified). Never raises into the caller."""
    skill = store.get_skill_by_key(co["id"], "lead-qualification")
    if not skill:
        return None
    email = (inq.get("email") or "").strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    free = (domain in _FREE_EMAIL_DOMAINS) or not domain
    rules = "\n".join(f"- {r}" for r in (skill.get("rules") or [])) or (skill.get("craft") or "")
    ctx = co.get("context") or {}
    system = (
        f"You qualify inbound sales enquiries for {co.get('name')}."
        + (f" Products/services: {ctx.get('products')}." if ctx.get("products") else "")
        + f"\nApply these qualification rules exactly — they define what qualifies and how each kind of lead "
        f"is handled:\n{rules}\n\n"
        "Decide whether THIS enquiry is a qualified opportunity. The rules above govern the judgement; the "
        "sender's email domain (corporate vs free/personal) is a supporting signal, not the verdict. "
        "When you have web search, ALSO research the SENDER as a person (their name + company: LinkedIn, "
        "company site, public profiles) — their role and where they are based — because replies tailor "
        "suggested call times to the lead's timezone. Report only what you actually found; empty strings "
        "otherwise, never a guess. "
        "Return a JSON object: {\"verdict\": \"qualified\" | \"not_qualified\" | \"needs_info\", "
        "\"confidence\": \"low\" | \"medium\" | \"high\", \"reason\": one short sentence under 25 words, "
        "\"bucket\": if the rules define handling buckets, the one this lead falls in: \"self_serve\" | "
        "\"conversation\" | \"strategic\", else \"\", "
        "\"person\": {\"role\": \"their title/role as found\", \"location\": \"city/region/country they appear "
        "to be based in\", \"timezone\": \"IANA timezone or UTC offset for that location\"}, "
        "\"company_name\": the sender company's proper name if identified, else \"\", "
        "\"est\": {\"amount\": a ROUND estimated total deal value per year, ONLY when you can ground it in "
        "stated volumes, the rules' pricing, or clearly researched program scale — 0 when you cannot ground "
        "it (an ungrounded figure is worse than none), \"currency\": ISO code, \"basis\": one short line on "
        "how it was grounded}, "
        "\"ball_in_our_court\": true if their message asks US to do something next (send a proposal, quotation, "
        "pricing, samples or more info) or otherwise puts the next step on us, false if we are waiting on them}.")
    user = (
        f"Sender: {inq.get('name') or ''} <{email}>\n"
        f"Email domain: {domain or '(none)'} ({'free/personal email' if free else 'corporate domain'})\n"
        f"Company stated: {inq.get('company_name') or '(none)'}\n"
        f"Enquiry: {(inq.get('message') or '')[:1200]}")
    model = provider.resolve_model(skill.get("model"))
    try:
        if free:
            out = provider.think_json(system, user, model=model, max_tokens=240,
                                      purpose="qualify", company=co.get("slug"))
        else:                                          # corporate domain -> let it look the company + person up
            out = provider.research_json(system, user, model=model, max_searches=4, max_tokens=900)
    except Exception:                                  # noqa: BLE001 -- a failed suggestion must never block intake
        try:
            out = provider.think_json(system, user, model=model, max_tokens=240)
        except Exception:                              # noqa: BLE001
            return None
    if not isinstance(out, dict) or not out:
        return None
    v = (out.get("verdict") or "needs_info").strip().lower()
    if v not in ("qualified", "not_qualified", "needs_info"):
        v = "needs_info"
    conf = (out.get("confidence") or "medium").strip().lower()
    bucket = (out.get("bucket") or "").strip().lower().replace("-", "_").replace(" ", "_")
    p = out.get("person") if isinstance(out.get("person"), dict) else {}
    person = {k: str(p.get(k) or "").strip()[:120] for k in ("role", "location", "timezone")}
    e = out.get("est") if isinstance(out.get("est"), dict) else {}
    try:
        est_amount = max(0.0, float(e.get("amount") or 0))
    except (TypeError, ValueError):
        est_amount = 0.0
    est = {"amount": est_amount, "currency": str(e.get("currency") or "USD").strip().upper()[:3],
           "basis": str(e.get("basis") or "").strip()[:200]}
    return {"verdict": v, "confidence": conf if conf in ("low", "medium", "high") else "medium",
            "reason": (out.get("reason") or "").strip()[:300],
            "bucket": bucket if bucket in ("self_serve", "conversation", "strategic") else "",
            "person": person, "company_name": str(out.get("company_name") or "").strip()[:120], "est": est,
            "ball_in_our_court": bool(out.get("ball_in_our_court")),
            "domain": domain, "free_email": free}


def intake_enquiry(slug: str, inq: dict, draft: bool = True) -> dict:
    """Triage ONE website enquiry (parsed fields {name,email,phone,subject,message}) and route it: genuine ->
    CRM contact (+ optional drafted reply); spam/junk -> filed, NEVER CRM'd. Shared by the email FORM_INTAKE
    poller AND the /api/intake/enquiry webhook, so a direct-POST enquiry is classified identically to an emailed one."""
    co = store.get_company_by_slug(slug)
    if not co:
        return {"ok": False, "reason": "unknown company"}
    if not (inq.get("email") or "").strip():
        return {"ok": False, "reason": "no email"}
    verdict = triage_inquiry(inq, slug)
    if not verdict["genuine"]:                       # spam/junk -> filed for audit, NOT CRM'd, NOT drafted
        flog = db.setting_get("enquiry_filtered") or []
        flog.append({"company": slug, "name": inq.get("name"), "email": inq.get("email"),
                     "category": verdict.get("category"), "reason": verdict.get("reason")})
        db.setting_set("enquiry_filtered", flog[-300:])
        return {"ok": True, "captured": False, "category": verdict.get("category"), "reason": verdict.get("reason")}
    try:
        crm.add_inquiry(inq, slug)                   # genuine -> verified CRM contact (dedup by email)
    except Exception:  # noqa: BLE001
        pass
    sug = None                                       # domain-led qualification suggestion (owner still decides)
    try:
        sug = qualify_suggest(co, inq)
        if sug:
            em = (inq.get("email") or "").strip().lower()
            db.setting_set(f"qual:{co['id']}:{em}", sug)
            db.setting_set(f"qual:email:{em}", {**sug, "company_id": co["id"]})
    except Exception:  # noqa: BLE001
        pass
    try:   # high-confidence qualified -> auto-create the pipeline entry (account + opportunity + estimate);
        opp = crm.auto_opportunity(inq.get("email"), slug, sug, inq)   # never blocks intake, reversible
        if opp:
            _notify_new_opportunity(co, opp, "auto-qualified from a new enquiry")
    except Exception:  # noqa: BLE001
        pass
    strategic = bool(sug and sug.get("bucket") == "strategic")
    if draft:   # EVERY genuine enquiry gets a drafted reply for approval — strategic leads included (the
                # trained lead-qualification rule sets the senior register; nothing sends without the owner)
        skill = store.get_skill_by_key(co["id"], "sales-first-response")
        if skill:
            store.create_card(co["id"], skill["id"], "email_reply",
                              {"brief": _email_brief(inq, co), "inquiry": inq, "triage": verdict,
                               "qual_suggest": sug})
        if strategic:   # heads-up so the owner watches this thread personally
            tg.send(f"[{co['name']}] STRATEGIC lead: {inq.get('name') or ''} <{inq.get('email')}> — "
                    f"{(sug or {}).get('reason') or 'enterprise potential'}. Reply drafted for your approval.")
    return {"ok": True, "captured": True, "category": verdict.get("category"),
            "qualification": (sug or {}).get("verdict")}


def _poll_one_form(slug: str, cfg: dict, days: int = 3) -> dict:
    co = store.get_company_by_slug(slug)
    skill = store.get_skill_by_key(co["id"], cfg.get("skill", "sales-first-response")) if co else None
    if not (co and skill):
        return {"reason": "company/skill missing"}
    key = f"form_processed:{slug}"
    seen_list = list(db.setting_get(key) or []); seen = set(seen_list)
    emails = gmail.list_recent(days=days, limit=30, rt_key=cfg["rt_key"], company=cfg.get("client"),
                               q=f'subject:"{cfg["subject"]}"', skip=seen)
    made = filtered = 0
    for e in emails:
        gid = e.get("gmail_id")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        if re.match(r"(?i)^\s*(re|fwd|fw)\s*:", e.get("subject") or ""):   # a reply/forward, not a fresh enquiry
            continue
        inq = _parse_form_email(e)
        res = intake_enquiry(slug, inq, draft=cfg.get("draft", True))   # shared triage+capture core
        if not res.get("ok") or res.get("reason") == "no email":
            continue
        if res.get("captured"):
            made += 1
        else:
            filtered += 1
        if made >= 10:
            break
    db.setting_set(key, (seen_list + [g for g in seen if g not in set(seen_list)])[-1000:])
    if made:
        action = "drafting for your approval" if cfg.get("draft", True) else "added to the CRM"
        tg.send(f"{made} genuine {co['name']} contact-form enquir{'ies' if made > 1 else 'y'}"
                + (f" ({filtered} spam filtered)" if filtered else "") + f" — {action}.")
    return {"made": made, "filtered": filtered}


def poll_company_forms() -> dict:
    """Read every configured + connected company's contact-form emails; route genuine leads to a drafted
    reply in the Inbox, filter spam. Runs on the 60s loop alongside the catch-all classifier."""
    if not EMAIL_ENQUIRY_FALLBACK:                       # sites POST enquiries direct now; email poller off
        return {"reason": "direct-webhook"}
    out = {}
    for slug, cfg in FORM_INTAKE.items():
        if not db.setting_get(cfg["rt_key"]):
            continue
        try:
            out[slug] = _poll_one_form(slug, cfg)
        except Exception as ex:  # noqa: BLE001
            tg.send(f"(form intake hiccup [{slug}]: {ex})")
    return out


# ---------- website waitlist / registration intake (opt-in, NOT an enquiry) ----------
# Some brands' websites email a "New <brand> waitlist signup" notification on each signup. This reads
# those, parses the person, and captures them to the CRM as a waitlist subscriber (org-tagged, waitlist
# flag set, newsletter opt-in). NO spam triage and NO drafted reply — a signup is an explicit opt-in, not
# an enquiry. Add a brand by adding a line to WAITLIST_INTAKE.
WAITLIST_INTAKE = {
    "filmspoke": {"rt_key": "gmail_refresh_token:filmspoke", "client": "filmspoke",
                  "subject": "waitlist signup", "source": "FilmSpoke waitlist"},
}


def run_opportunity_followups() -> dict:
    """SYSTEM-WIDE: walk every AUTO opportunity whose next follow-up is due. Each fires the cadence's action
    (chase/checkin) as a deal-linked drafted card for approval, then arms the next step; when the sequence is
    exhausted the opportunity is marked Lost. The cadence is per-company config (crm.get_cadence), not code."""
    due = db.query("select * from crm_projects where automation='auto' and next_followup is not null "
                   "and next_followup <= now() order by next_followup limit 50")
    fired = []
    for opp in due:
        try:
            r = crm.advance_followup(opp["id"])
            if not r:
                continue
            if r.get("action") == "quiet":     # WON work: sequence done, the project just goes quiet
                tg.send(f"'{opp['title']}': follow-up sequence finished. The project stays as it is — "
                        "no more automatic nudges until something changes.")
                continue
            if r.get("action") in ("lost", "dormant"):
                tg.send(f"Opportunity '{opp['title']}' moved to Dormant — full follow-up sequence (incl. "
                        "revivals) exhausted. It is never auto-Lost; wake it any time from the deal.")
                continue
            _spawn_followup_card(opp, r["action"])
            fired.append([opp["id"], r["action"]])
        except Exception as e:  # noqa: BLE001
            tg.send(f"(follow-up #{opp.get('id')} hiccup: {e})")
    return {"fired": fired}


def _deal_thread_context(co: dict, email: str, limit: int = 5) -> str:
    """The recent REAL correspondence with this contact (newest first, trimmed), read from the company's
    sales/send mailbox — so a follow-up references what was actually said, never a generic chase. Fail-soft:
    an unreadable mailbox returns '' and the follow-up still goes out (just less informed)."""
    try:
        slug = co.get("slug")
        rt = None
        for k in (f"gmail_send_refresh_token:{slug}", f"gmail_refresh_token:{slug}"):
            if db.setting_get(k):
                rt = k
                break
        if not rt:
            return ""
        msgs = gmail.list_recent(days=180, limit=limit, rt_key=rt,
                                 q=f"(from:{email} OR to:{email}) newer_than:180d",
                                 company=_inbox_client_company(slug))
        lines = []
        for m in msgs:
            body = re.sub(r"\s+", " ", (m.get("body") or m.get("snippet") or "")).strip()[:900]
            if body:
                lines.append(f"[{m.get('date') or ''} | from {m.get('email') or m.get('from') or ''} | "
                             f"{(m.get('subject') or '')[:80]}]\n{body}")
        return "\n---\n".join(lines[:limit])
    except Exception:  # noqa: BLE001
        return ""


def _spawn_followup_card(opp: dict, action: str) -> None:
    """Create a deal-linked follow-up card. Drafts a chase via the company's first-response skill when there's a
    contact to chase; otherwise drops a nudge notification on the opportunity. Drafting quality is the skill's
    job; this code only fetches the shelves — deal notes, live reminders, the real thread — for the drafter."""
    co = store.get_company_by_slug(crm._slug_for_org(opp.get("company")))
    if not co:
        return
    contacts = opp.get("contacts") or []
    primary = next((c for c in contacts if c.get("primary")), contacts[0] if contacts else None)
    email = (primary or {}).get("email") or opp.get("contact_email")
    if email and db.one("select id from tasks where company_id=%s and kind='email_reply' and "
                        "status in ('new','drafting','awaiting_approval','awaiting_correction','sending') "
                        "and lower(request->'inquiry'->>'email')=lower(%s) limit 1", (co["id"], email)):
        return   # an email card for this contact is already in flight — never stack chases (audit F10)
    skill = store.get_skill_by_key(co["id"], "sales-first-response")
    label = {"checkin": "check-in", "revive": "revival"}.get(action, "follow-up")
    if email and skill:
        stage = opp.get("stage")
        if stage == "Final Payment":
            # MONEY, not readiness: the work is delivered and a balance is outstanding. Patience here
            # reads as not caring about being paid (card 394 drafted a 'where do things stand' note on a
            # 60k receivable). Polite and warm, but the ask is unambiguous.
            brief = (f"PAYMENT FOLLOW-UP on '{opp['title']}' — the work is DELIVERED and the final balance "
                     "is still outstanding. This is an accounts chase, not a check-in: be warm and "
                     "professional, but ASK DIRECTLY about the outstanding payment, reference what was "
                     "last said about it in the correspondence below, and ask for a payment date or the "
                     "status in their system. Do NOT ask 'where do things stand' vaguely, do not offer a "
                     "call INSTEAD of asking, and never re-sell or pitch anything.")
        elif stage in crm.WON_STAGES:
            brief = (f"{label.title()} on the PROJECT '{opp['title']}' — this is WON work, not a pitch. We "
                     "are waiting on the client's readiness, not chasing a decision: warm, patient, no "
                     "sales pressure and no re-selling. Goal: find out where they stand and agree the "
                     "next step.")
        else:
            brief = f"{label.title()} on the opportunity '{opp['title']}'. Goal: get a reply / book a meeting."
        if action == "revive":
            brief = (f"Long-gap REVIVAL of the dormant opportunity '{opp['title']}' — months since their "
                     "last reply. The REVIVAL standing rules on the sales-followup skill govern the tone "
                     "and shape; the correspondence below is what they originally asked about.")
        note = (opp.get("note") or "").strip()
        if note:
            brief += f"\nNOTES ON THE OPPORTUNITY (from the team — factor them in): {note[:800]}"
        try:
            rems = db.query("select title from reminders where target_type='deal' and target_id=%s "
                            "and status in ('pending','snoozed') order by due_at limit 3", (str(opp["id"]),))
            if rems:
                brief += "\nOPEN REMINDERS on this deal: " + "; ".join(r["title"] for r in rems)
        except Exception:  # noqa: BLE001
            pass
        # a revival hinges on WHY the deal faded, which often sits deep in the thread — serve more of it
        thread = _deal_thread_context(co, email, limit=10 if action == "revive" else 5)
        if thread:
            brief += ("\nRECENT CORRESPONDENCE with them (newest first — reference it, stay consistent "
                      "with it, and never repeat a chase they already answered):\n" + thread[:5000])
        inq = {"name": (primary or {}).get("name") or "", "email": email, "message": ""}
        # the situation is SYSTEM knowledge, never 'their words' (contract: inquiry.message = client only)
        t = store.create_card(co["id"], skill["id"], "email_reply",
                              {"brief": brief, "inquiry": inq, "followup": action, "deal_id": opp["id"],
                               "system_note": f"Automated {label} on the opportunity '{opp['title']}'. "
                                              "They have not replied to our last message."},
                              contact=email, deal_id=opp["id"])
        if t:
            db.execute("update tasks set deal_id=%s where id=%s", (opp["id"], t["id"]))
    else:
        notifications.notify(f"Follow-up due ({label}) — {opp['title']}", "Opportunity follow-up",
                             category="reminder", company_id=co["id"], target_type="deal", target_id=str(opp["id"]))


def _parse_waitlist_email(e: dict) -> dict:
    """Pull the signup out of a 'Name:/Email:/Company:' waitlist notification email."""
    body = e.get("body") or e.get("snippet") or ""
    return {"gmail_id": e.get("gmail_id"), "name": _form_field(body, "Name"),
            "email": _form_field(body, "Email"), "company": _form_field(body, "Company")}


def _poll_one_waitlist(slug: str, cfg: dict, days: int = 30) -> dict:
    co = store.get_company_by_slug(slug)
    if not co:
        return {"reason": "company missing"}
    key = f"waitlist_processed:{slug}"
    seen_list = list(db.setting_get(key) or []); seen = set(seen_list)
    emails = gmail.list_recent(days=days, limit=30, rt_key=cfg["rt_key"], company=cfg.get("client"),
                               q=f'subject:"{cfg["subject"]}"', skip=seen)
    cid_row = db.one("select id from companies where slug=%s", (slug,))
    made = 0
    for e in emails:
        gid = e.get("gmail_id")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        reg = _parse_waitlist_email(e)
        if not reg.get("email"):                 # couldn't parse a signup email -> skip
            continue
        try:
            res, _ = crm.add_registration(reg, slug, source=cfg.get("source", f"{co['name']} waitlist"),
                                          waitlist=True)
        except Exception:  # noqa: BLE001
            continue
        if res not in ("added", "matched"):
            continue
        made += 1
        try:    # grouped FYI card in the Inbox (one rolling card per company until dismissed)
            from . import notifications
            notifications.notify(
                "New waitlist signup", f"{reg.get('name') or reg['email']} → {co['name']} waitlist",
                priority="fyi", category="lead", dedup_key=f"waitlist:{slug}",
                company_id=(cid_row["id"] if cid_row else None),
                target_type="contact", target_id=reg["email"],
                item={"name": reg.get("name") or reg["email"], "email": reg["email"], "cat": "waitlist"})
        except Exception:  # noqa: BLE001
            pass
    db.setting_set(key, (seen_list + [g for g in seen if g not in set(seen_list)])[-1000:])
    if made:
        tg.send(f"{made} new {co['name']} waitlist signup{'s' if made > 1 else ''} — added to the CRM.")
    return {"made": made}


def poll_waitlists() -> dict:
    """Read every configured + connected brand's waitlist-signup emails; capture each as a CRM waitlist
    subscriber. Runs on the 60s loop alongside the classifier and contact-form intake."""
    out = {}
    for slug, cfg in WAITLIST_INTAKE.items():
        if not db.setting_get(cfg["rt_key"]):
            continue
        try:
            out[slug] = _poll_one_waitlist(slug, cfg)
        except Exception as ex:  # noqa: BLE001
            tg.send(f"(waitlist intake hiccup [{slug}]: {ex})")
    return out


# ---------- inbox classifier (the sales-triage universal skill, on Haiku) ----------

INBOX_CATEGORIES = ["client", "lead", "partner", "support", "freelancer", "vendor", "recruitment",
                    "finance", "marketing", "spam", "personal", "automated"]
# these become CRM contacts. "client" = an existing is_client contact, set deterministically from the CRM
# (see classify_email) rather than guessed — it overrides the content-based category.
_INBOX_CRM = {"client", "lead", "partner", "support", "freelancer", "vendor", "recruitment", "finance"}
# every inbound contact we add is newsletter-eligible (Rashad 2026-06-18: they contacted us, it's a general
# newsletter, and unsubscribe + complaint->opt-out keep it self-correcting). Own knob in case we re-scope.
_INBOX_NEWSLETTER = set(_INBOX_CRM)
# each company's main catch-all inbox -> used to derive its OWN domain (never CRM our own / internal senders)
INBOXES = {"tabscanner": "api@tabscanner.com", "sensa": "hello@sensa.digital",
           "snaprewards": "loyalty@snap-rewards.com", "filmspoke": "create@filmspoke.ai",
           "skyvision": "fly@skyvision.film"}


# our own company domains (a sender on any of these is US, never a customer) — kept in sync with the scrape rules
from .identity import OWN_COMPANY_DOMAINS   # single definition (identity.py) — meetnotes shares it
_PLACEHOLDER_RE = re.compile(r"^linked\d+@")


def _internal_index() -> dict:
    """Everything that means 'this sender is US / internal' on inbound, so the classifier never CRMs ourselves:
    own company domains, the Instantly burner SENDING domains (settings.own_sending_domains), the named internal
    roster (settings.internal_people), and every active newsletter test-group member. Read live so it stays current."""
    domains = set(OWN_COMPANY_DOMAINS)
    domains |= {str(d).lower().strip() for d in (db.setting_get("own_sending_domains") or [])}
    emails = {str(e).lower().strip() for e in (db.setting_get("internal_people") or [])}
    try:
        emails |= {r["email"].lower() for r in db.query("select lower(email) email from newsletter_test_group where active")}
    except Exception:  # noqa: BLE001
        pass
    return {"domains": domains, "emails": emails}


def _is_internal(addr: str, own_domain: str = "", index: dict | None = None) -> bool:
    a = (addr or "").lower().strip()
    d = a.split("@")[-1]
    if own_domain and (d == own_domain or d.endswith("." + own_domain)):
        return True
    if _PLACEHOLDER_RE.match(a):                 # LinkedIn placeholder identity, not a real inbound sender
        return True
    if index and (d in index["domains"] or a in index["emails"]):
        return True
    return False


def classify_email(company: dict, email: dict) -> dict:
    """Classify ONE inbound email via the `sales-triage` universal skill, on Haiku. Reads the skill's rules
    + the company context, so the intelligence lives in the skill. Returns {category, to_crm, reason}."""
    skill = store.get_skill_by_key(company["id"], "sales-triage")
    system = "\n\n".join(filter(None, [
        "You are Cortex's inbox classifier for this company's main catch-all inbox.",
        worker._company_context(company),
        worker._rules_block(skill) if skill else "",
        ("Classify the email into EXACTLY ONE category from: " + ", ".join(INBOX_CATEGORIES) + ". "
         "Guidance: freelancer = a contractor/agency offering THEIR services to us; recruitment = a person "
         "seeking a JOB with us (a CV, 'are you hiring?', wants to join the team); finance = invoices, "
         "payments, statements, billing or account matters (in either direction). "
         'Return JSON {"category":"<one>","to_crm":boolean,"reason":"<short phrase>",'
         '"summary":"<1-2 sentences for the CRM note: who the sender is and what they want>",'
         '"market":"<a short industry/market label, e.g. video production, e-commerce, real estate, '
         'music/audio, hospitality; empty string if genuinely unclear>"}. to_crm is true for '
         "lead/partner/support/freelancer/vendor/recruitment, false for marketing/spam/personal/automated."),
    ]))
    user = (f"From: {email.get('name')} <{email.get('email')}>\nSubject: {email.get('subject')}\n\n"
            + (email.get("body") or email.get("snippet") or "").strip()[:2500])
    try:
        out = provider.think_json(system, user, model=provider.MODEL_ROUTER,
                                  purpose="inbox-classify", company=company.get("slug"), cache=True)
    except Exception:  # noqa: BLE001
        return {"category": "unclear", "to_crm": False, "reason": "classify error"}
    cat = (out.get("category") or "unclear").strip().lower()
    if cat not in INBOX_CATEGORIES:
        cat = "unclear"
    return {"category": cat, "to_crm": cat in _INBOX_CRM, "reason": (out.get("reason") or "").strip(),
            "summary": (out.get("summary") or "").strip(), "market": (out.get("market") or "").strip()}


_DELIVERY_STAGES = {"Booked", "Production", "Final Payment", "Recurring"}


def _rt_for_sender(co: dict, email: str) -> str | None:
    """The mailbox token that can genuinely send AS this address: a team member's own mailbox, else the
    company send mailbox when the address is its send-as identity. None = let the send path resolve."""
    email = (email or "").lower()
    for v in _company_senders(co["id"]).values():
        if v["email"] == email:
            return v["rt_key"]
    slug = co.get("slug") or ""
    sa = db.setting_get(f"gmail_send_account:{slug}")
    if sa and str(sa).strip('" ').lower() == email and db.setting_get(f"gmail_send_refresh_token:{slug}"):
        return f"gmail_send_refresh_token:{slug}"
    return None


def _belongs_to_other_company(e: dict, slug: str) -> bool:
    """True when this email is ADDRESSED to another of our companies and not to this one — the team's
    mailboxes alias across domains (rashad@skyvision.film delivers into the same account as
    rashad@sensa.digital), so every company's sweep sees every domain's mail. The recipients decide
    which company owns the conversation; without this, one client reply spawns a card per company
    (Dawn Christine: 4 duplicate cards across Sensa + SkyVision, 2026-08-26)."""
    rcpt = f"{e.get('to') or ''} {e.get('cc') or ''}".lower()
    if not rcpt.strip():
        return False                       # bcc/undisclosed recipients: can't tell, let it through
    own = (INBOXES.get(slug, "") or "").split("@")[-1]
    if own and ("@" + own) in rcpt:
        return False                       # addressed to us (possibly among others): ours to handle
    return any(("@" + a.split("@")[-1]) in rcpt for s, a in INBOXES.items() if s != slug)

# attachment types the drafter reads NATIVELY (image blocks + PDF document blocks); office documents
# (docx/xlsx/xls/pptx/csv/txt) are text-extracted by doctext at draft time instead
_READABLE_ATT_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif", "application/pdf"}


def _inbound_att_refs(e: dict, rt_key: str | None, client: str | None) -> list[dict]:
    """LIGHT references to the readable attachments on an inbound email (no bytes in the DB — they are
    fetched fresh from Gmail at draft time by _request_for_draft). Caps: 4 files, 8MB each."""
    refs = []
    for a in (e.get("attachments") or []):
        mime, fn = (a.get("mime") or "").lower(), a.get("filename") or ""
        readable = mime in _READABLE_ATT_MIMES or doctext.kind_for(mime, fn)
        if readable and 0 < int(a.get("size") or 0) <= 8_000_000:
            refs.append({"gmail_id": e.get("gmail_id"), "att_id": a["att_id"], "mime": a["mime"],
                         "filename": fn, "rt_key": rt_key, "client": client})
        if len(refs) >= 4:
            break
    return refs


def _request_for_draft(task: dict) -> dict:
    """The task's request with inbound attachment refs resolved to data: URLs, for the drafter's eyes only.
    The DB row keeps just the refs; the send path never sees these bytes (so a client's own files can never
    be attached back onto our reply). A failed fetch drops that file — drafting proceeds regardless."""
    req = dict(task.get("request") or {})
    refs = req.get("inbound_attachments") or []
    if not refs:
        return _draft_context_for_reply(task, req)
    datas = list(req.get("attachments") or [])
    texts = []
    for r in refs[:4]:
        try:
            b = gmail.get_attachment(r["gmail_id"], r["att_id"],
                                     rt_key=r.get("rt_key") or req.get("mailbox_rt") or "gmail_refresh_token",
                                     company=r.get("client"))
            if not b or len(b) > 8_000_000:
                continue
            mime, fn = (r.get("mime") or "").lower(), r.get("filename") or ""
            if mime in _READABLE_ATT_MIMES:      # image/PDF -> the model reads it natively
                datas.append(f"data:{mime};base64," + _b64.b64encode(b).decode())
            elif doctext.kind_for(mime, fn):     # office doc -> extracted text block
                txt = doctext.extract(mime, fn, b)
                texts.append({"filename": fn or mime,
                              "text": txt or "(this attachment could not be read — say so rather than guessing "
                                             "its contents, and ask them to resend as PDF if it matters)"})
        except Exception:  # noqa: BLE001
            continue
    if datas:
        req["attachments"] = datas
    if texts:
        req["attachment_texts"] = texts
    return _draft_context_for_reply(task, req)


def _adopt_existing_thread(task: dict, req: dict, manifest: list) -> None:
    """THREAD ADOPTION: a follow-up or deal-linked draft to a known contact must continue the REAL Gmail
    thread from the mailbox that owns it (thread-true reply + thread-sticky sender) — never open a fresh
    conversation from the default sender. Card 383 would have started a new thread from Gino while the
    live SFORS thread sat in Rashad's mailbox. Adopted keys are PERSISTED to the task request because the
    send path reads the stored request, not the draft-time copy. Fail-soft throughout."""
    email = ((req.get("inquiry") or {}).get("email") or "").strip()
    # WHEN TO CONTINUE rather than start fresh: whenever there is established work with this person -
    # a follow-up, a deal-linked card, or an owner-composed email about a deal. A cold new topic with
    # no work context still opens its own thread (owner, 31 Aug: "continue the last relevant thread so
    # the recipients and the people in copy stay right").
    if not email or (req.get("thread") or {}).get("id"):
        return
    # A LIVE CONVERSATION is itself the work context: writing to someone we are mid-thread with must
    # continue that thread even when the card carries no deal (card 414 was composed in Talk, so it
    # would have opened a new conversation with a fake 'RE:' subject - owner, 31 Aug 2026). A genuinely
    # cold contact has no recent thread, so nothing changes for them.
    _recent_days = 45 if (req.get("followup") or req.get("deal_id")) else 21
    from email.utils import parsedate_to_datetime
    co = store.get_company(task.get("company_id")) or {}
    senders = _company_senders(task["company_id"])
    # An owner-chosen sender keeps his sender: we then look for the thread in THAT mailbox only,
    # instead of abandoning continuation altogether (which is what left card 398 with no thread).
    _chosen = (req.get("from_email") or "").strip().lower()
    pool = [v for v in senders.values() if not _chosen or v["email"] == _chosen] or list(senders.values())
    hits, seen_rt = {}, set()            # rt_key -> (dt, sender, newest message in THAT mailbox)
    for s in pool:
        if s["rt_key"] in seen_rt:
            continue                     # alias entries (richard->rashad) point at the same mailbox
        seen_rt.add(s["rt_key"])
        try:
            msgs = gmail.list_recent(limit=1, rt_key=s["rt_key"],
                                     q=f"(from:{email} OR to:{email}) newer_than:{_recent_days}d",
                                     company=_inbox_client_company(co.get("slug") or ""))
            m = msgs[0] if msgs else None
            if not m or not m.get("thread_id"):
                continue
            _subj = (m.get("subject") or "").lower()
            if (m.get("auto_marker") or any(_subj.startswith(p) for p in (
                    "invitation:", "accepted:", "declined:", "updated invitation:", "canceled:",
                    "cancelled:", "notes:", "automatic reply", "out of office", "delivery status"))):
                continue                 # a calendar/auto thread is not the conversation
            try:
                dt = parsedate_to_datetime(m.get("date") or "")
            except Exception:  # noqa: BLE001
                dt = None
            hits[s["rt_key"]] = (dt, s, m)
        except Exception:  # noqa: BLE001
            continue
    if not hits:
        return
    best = max(hits.values(), key=lambda x: (x[0] is not None, x[0]))
    # THREAD-STICKY SENDER: a cc'd copy is not ownership (card 388 went out under Gino because his
    # mailbox held a cc of Rashad's send). The owner is whoever SENT the newest our-side message —
    # its From if that is one of our senders, else the first of our senders in its To/Cc.
    _, s, m = best
    ours = {v["email"].lower(): v for v in senders.values()}
    frm = (m.get("email") or "").lower()
    if frm in ours:
        owner = ours[frm]
    else:
        owner = next((ours[a.lower()] for a in re.findall(r"[\w.+-]+@[\w.-]+",
                      (m.get("to") or "") + " " + (m.get("cc") or "")) if a.lower() in ours), s)
    if _chosen and owner["email"] != _chosen:
        owner = next((v for v in senders.values() if v["email"] == _chosen), owner)
    if owner["rt_key"] != s["rt_key"]:
        if owner["rt_key"] not in hits:
            return                       # owner's mailbox has no local copy: no safe threadId to reply with
        _, s, m = hits[owner["rt_key"]]  # threadIds are mailbox-local: take them from the OWNER's mailbox
    s = owner
    req["thread"] = {"id": m["thread_id"], "msg_id": m.get("msg_id") or "",
                     "references": m.get("references") or ""}
    req["thread_cc"] = _thread_participants(m, email)
    req["from_email"], req["mailbox_rt"] = s["email"], s["rt_key"]
    subj = re.sub(r"^(?:(?:re|fwd?)\s*:\s*)+", "", (m.get("subject") or ""), flags=re.I).strip()
    if subj:
        req["inquiry"] = {**(req.get("inquiry") or {}), "subject": subj}
    manifest.append(f"existing_thread({s['email'].split('@')[0]})")
    try:
        if task.get("id"):
            db.execute(
                "update tasks set request = request || %s::jsonb where id=%s",
                (json.dumps({"thread": req["thread"], "from_email": req["from_email"],
                             "mailbox_rt": req["mailbox_rt"], "inquiry": req["inquiry"]}), task["id"]))
    except Exception:  # noqa: BLE001
        pass


def _draft_context_for_reply(task: dict, req: dict) -> dict:
    """THE CONTEXT ASSEMBLER (rebuild Stage 1). Every email the system drafts — inbound replies,
    Talk-composed outbound, follow-ups, post-meeting, chases, reminder-spawned — passes through here at
    draft time and receives the full shelf set for its contact: the real thread (incl. our sent mail),
    Gemini meeting notes, any booked meeting, the deal timeline, and the owner's past corrections on this
    relationship. A manifest of what was served is stamped on the card, so 'what did the drafter see?'
    is always answerable. Each shelf is fail-soft: a fetch hiccup skips that shelf, never the draft."""
    email = ((req.get("inquiry") or {}).get("email") or "").strip()
    if task.get("kind") not in ("email_reply", "email_draft") or not email:
        return req
    manifest = []
    _adopt_existing_thread(task, req, manifest)
    co = store.get_company(task.get("company_id")) or {}
    if req.get("attachment_texts") or (req.get("inbound_attachments") and req.get("attachments")):
        manifest.append("inbound_files")
    if req.get("attach_docs"):
        manifest.append("outgoing_attachments")
    try:
        hist = _deal_thread_context(co, email, limit=4)
        if hist:
            req["thread_history"] = hist
            manifest.append("thread_history")
    except Exception:  # noqa: BLE001 — context is best-effort, drafting proceeds regardless
        pass
    try:
        mn = meetnotes.latest_for_contact(task["company_id"], email)
        if mn and mn.get("summary"):
            when = mn["starts_at"].strftime("%d %b %Y") if mn.get("starts_at") else ""
            req["meeting_notes"] = (mn.get("title") or "Meeting") + f" ({when}):\n" + mn["summary"]
            manifest.append("meeting_notes")
    except Exception:  # noqa: BLE001
        pass
    try:
        fm = db.one("select request->'meeting' m from tasks where company_id=%s and id<>%s and "
                    "lower(request->'inquiry'->>'email')=lower(%s) and "
                    "request->'meeting'->>'event_id' is not null and "
                    "(request->'meeting'->>'start')::timestamptz > now() order by id desc limit 1",
                    (task["company_id"], task.get("id"), email))
        if fm and fm.get("m"):
            req["existing_meeting"] = fm["m"]
            manifest.append("booked_meeting")
    except Exception:  # noqa: BLE001
        pass
    try:   # the deal's whole timeline — what was promised, asked, and where it stands
        did = req.get("deal_id") or task.get("deal_id")
        if not did:
            ds = crm.active_deals_for_email(email, co.get("slug"))
            did = ds[0]["id"] if len(ds) == 1 else None
        if did:
            from . import pipeline
            tl = pipeline.deal_context(int(did))
            if tl:
                req["deal_timeline"] = tl
                manifest.append("deal_timeline")
    except Exception:  # noqa: BLE001
        pass
    try:   # notes the team saved on the CONTACT (deal notes already ride the deal_timeline shelf)
        c = db.one("select history from crm_master where lower(email)=lower(%s)", (email,))
        evs = [h for h in ((c or {}).get("history") or []) if h.get("event") == "note"][-5:]
        if evs:
            req["contact_notes"] = "\n".join(
                f"- {h.get('ts', '')[:10]}: {h.get('text', '')[:250]}" for h in evs)
            manifest.append("contact_notes")
    except Exception:  # noqa: BLE001
        pass
    try:   # the owner's past corrections on THIS relationship — a taught lesson is never re-learned
        notes = db.query(
            "select d.note from decisions d join tasks t on t.id=d.task_id where t.company_id=%s and "
            "d.action='correct' and coalesce(d.note,'')<>'' and "
            "lower(t.request->'inquiry'->>'email')=lower(%s) and d.task_id<>%s "
            "order by d.id desc limit 5", (task["company_id"], email, task.get("id") or 0))
        if notes:
            req["owner_feedback"] = "\n".join("- " + n["note"][:250] for n in notes)
            manifest.append("owner_feedback")
    except Exception:  # noqa: BLE001
        pass
    try:   # the media library — REAL portfolio links, so 'share sample work' can never be invented
        rows = db.query(
            "select distinct on (cat) cat, title, watch_url, rating from ("
            "  select title, watch_url, rating, views, jsonb_array_elements_text(categories) cat"
            "  from media_assets where company_id=%s and coalesce(watch_url,'')<>'' and status='live'"
            "  and privacy in ('public','unlisted')) x "
            "where cat not in ('version-variant','internal-test') "
            "order by cat, rating desc nulls last, views desc nulls last", (task["company_id"],))
        if rows:   # category-balanced: the BEST film of every genre (a global top-N drowns niche
            # categories — 118 films tie at 7/10, so fashion/beauty never surfaced for a beauty client)
            req["media_library"] = "\n".join(
                f"- {r['title']} [{r['cat']}]: {r['watch_url']}" for r in rows)
            manifest.append("media_library")
    except Exception:  # noqa: BLE001
        pass
    try:   # stamp the manifest on the card (field-level jsonb_set: no read-modify-write clobber)
        if task.get("id"):
            db.execute("update tasks set request = jsonb_set(request, '{context_manifest}', %s::jsonb) "
                       "where id=%s", (json.dumps(manifest), task["id"]))
    except Exception:  # noqa: BLE001
        pass
    return req


def _pause_or_reschedule_followups(co: dict, deals: list, sender: str, body: str) -> None:
    """The contact wrote to us -> every armed auto-chase clock on their deals pauses (the ball is now in
    OUR court; it re-arms when our reply sends). If their email STATES a timeframe ('give us a couple of
    weeks', 'ready after Ramadan'), Haiku reads the phrase, CODE stamps the actual date, the clock re-arms
    for then, and a card tells the owner exactly what was decided and from which words. Fail-soft."""
    try:
        armed = [d for d in (deals or []) if d.get("automation") == "auto" and d.get("next_followup")]
        if not armed:
            return
        wait = None
        try:
            out = provider.think_json(
                "An email from a contact we are chasing. Does it STATE when we should next follow up or "
                "check back (e.g. 'give us two weeks', 'we should be ready next month', 'after the summit "
                "in October')? Return JSON {\"wait_days\": <integer days from today, conservative>, "
                "\"quote\": \"<their exact words, under 15 words>\"} — or {\"wait_days\": null} if no "
                "timeframe is stated. Never guess one that is not in the text.",
                body[:2000], model=provider.MODEL_ROUTER, purpose="followup-wait", company=co.get("slug"))
            if isinstance(out, dict) and isinstance(out.get("wait_days"), int) and 0 < out["wait_days"] <= 366:
                wait = out
        except Exception:  # noqa: BLE001
            wait = None
        for d in armed:
            crm.pause_followups(d["id"])
            if wait:
                when = datetime.now(timezone.utc) + timedelta(days=wait["wait_days"])
                crm.resume_followups(d["id"], when)
                notifications.notify(
                    f"Follow-up on '{d['title']}' rescheduled to {when.strftime('%d %b %Y')} — they said "
                    f"“{wait.get('quote') or 'a timeframe'}”. Adjust on the deal if that's wrong.",
                    "Follow-up cadence", category="reminder", company_id=co["id"],
                    target_type="deal", target_id=str(d["id"]))
            else:
                notifications.notify(
                    f"Auto follow-ups on '{d['title']}' paused — {sender} replied; the cadence re-arms "
                    "when your reply sends.", "Follow-up cadence", category="reminder",
                    company_id=co["id"], target_type="deal", target_id=str(d["id"]))
    except Exception:  # noqa: BLE001 — cadence bookkeeping must never block the reply draft
        pass


def _draft_direct_reply(co: dict, e: dict, cls: dict, rt_key: str | None, address: str | None) -> None:
    """A substantive lead/client/finance email sent DIRECTLY to a monitored mailbox -> draft the reply as an
    approval card, from the mailbox that received it. Skips thin mail (a signature and nothing else). If the
    sender already has an OPEN reply card, the new email SUPERSEDES it: the card is updated with the latest
    message and redrafted — a fresh mail from them must never be silently swallowed (bit MAH Gold, Aug 2026)."""
    try:
        sender = (e.get("email") or "").strip()
        body = (e.get("body") or e.get("snippet") or "").strip()
        if not sender or len(body) < 40:
            return
        dup = db.one("select id, request from tasks where company_id=%s and kind='email_reply' and "
                     "status in ('new','drafting','awaiting_approval','awaiting_correction') and "
                     "lower(request->'inquiry'->>'email')=lower(%s) limit 1", (co["id"], sender))
        deals = crm.active_deals_for_email(sender, co.get("slug"))
        # machine-generated mail (bills, notifications, anything bulk/no-reply) never gets a drafted
        # reply UNLESS the sender is on an active deal (a real counterpart whose system mailed us).
        local = sender.split("@")[0].lower()
        robot = e.get("auto_marker") or re.match(
            r"^(no[-._]?reply|do[-._]?not[-._]?reply|notifications?|alerts?|mailer|bounce|newsletter|"
            r"customer[-._]?care|billing|statements?)($|[.+_-])", local)
        if robot and not deals:
            return
        deal = deals[0] if len(deals) == 1 else None   # attach a deal_id only when it is unambiguous
        _pause_or_reschedule_followups(co, deals, sender, body)
        # PROJECT correspondence (deal already in delivery) drafts on the company's general email-handling
        # skill (+ its related project skills' rules), not the sales lane — that is where project-management
        # behaviour gets trained. Opportunity-stage and no-deal mail stays on sales-first-response.
        in_delivery = bool(deal and deal.get("stage") in _DELIVERY_STAGES)
        skill = (store.get_skill_by_key(co["id"], "email-handling") if in_delivery else None) \
            or store.get_skill_by_key(co["id"], "sales-first-response")
        if not skill:
            return
        subj = (e.get("subject") or "").strip()
        is_thread = subj.lower().startswith(("re:", "fwd:", "fw:"))
        inq = {"name": e.get("name") or "", "email": sender, "subject": subj or "your email",
               "message": body[:4000]}
        what = {"lead": "a direct enquiry", "client": "an email from an existing client",
                "finance": "a finance/billing email"}.get(cls["category"], "an email")
        brief = (f"This is {what} sent directly to {address or 'our'} mailbox. Read it and draft our reply "
                 f"in the company voice, addressing exactly what they said.")
        if deal:
            brief += (f" CONTEXT: this sender belongs to the ACTIVE project/deal '{deal['title']}' "
                      f"(stage: {deal['stage']}). Reply as their project contact, consistent with that work; "
                      "do not treat them as a new lead.")
            try:   # the deal's timeline rides the brief, so the draft knows the whole flow so far
                _tl = pipeline.deal_context(deal["id"])
                if _tl:
                    brief += "\n\n" + _tl
            except Exception:  # noqa: BLE001
                pass
            try:   # pipeline loop: log the inbound + catch stated client deadlines (once per message)
                _gid = e.get("gmail_id") or ""
                if not _gid or not db.one(
                        "select 1 from tasks where company_id=%s and request->>'gmail_id'=%s limit 1",
                        (co["id"], _gid)):
                    pipeline.record_inbound(e, deal, co)
            except Exception:  # noqa: BLE001
                pass
        elif len(deals) > 1:
            names = "; ".join(f"'{d['title']}' (stage: {d['stage']})" for d in deals)
            brief += (f" CONTEXT: this sender's company has SEVERAL active projects with us: {names}. "
                      "Read their email to tell which one this thread is about and reply as their project "
                      "contact for THAT project; do not treat them as a new lead and do not mix projects up.")
        if cls["category"] == "finance":
            brief += (" This is a MONEY matter: acknowledge precisely, commit to nothing financial without "
                      "the owner, and never state amounts that are not in the email itself.")
        # The CATCH-ALL mailbox (hello@ etc.) never sends: replies to mail it received route through the
        # company's send mailbox + reply_from person (their token, their signature). Personal mailboxes
        # (gino@/rashad@/ayresh@) still reply as themselves.
        catchall = (address or "").lower() == (INBOXES.get(co.get("slug"), "") or "").lower()
        from_email = None if catchall else address
        mailbox_rt = None if catchall else rt_key
        # HIGH-VALUE ROUTING (config lives in the company profile; the CONDITION is code, because a
        # threshold check is a must-be-real computation): an opportunity whose deal value meets
        # data.high_value_threshold drafts FROM data.high_value_from — the Director handles it
        # personally — with data.high_value_cc copied on the send (applied in _email_envelope).
        hv = False
        try:
            _prof = profile.get(co["id"]) or {}
            _hvt = float(_prof.get("high_value_threshold") or 0)
            _hvf = (_prof.get("high_value_from") or "").strip().lower()
            if _hvt and _hvf and deal and float(deal.get("value") or 0) >= _hvt:
                hv = True
                from_email, mailbox_rt = _hvf, _rt_for_sender(co, _hvf)
        except Exception:  # noqa: BLE001
            hv = False
        # THREAD-STICKY SENDER: a conversation someone on the team is already having stays THEIRS.
        # Whoever we last sent to this contact AS is who replies — never silently switched to the
        # company default mid-thread (a Rashad<->client thread must not flip to Gino).
        if is_thread:
            last = db.one("select d.snapshot->>'from' f from decisions d join tasks t on t.id=d.task_id "
                          "where t.company_id=%s and d.action='send' and lower(d.snapshot->>'to')=lower(%s) "
                          "and d.snapshot->>'from' is not null order by d.id desc limit 1",
                          (co["id"], sender))
            lf = ((last or {}).get("f") or "").strip().lower()
            if lf and lf != (from_email or "").lower():
                from_email, mailbox_rt = lf, _rt_for_sender(co, lf)
        req = {"brief": brief, "inquiry": inq,
               "from_email": from_email, "mailbox_rt": mailbox_rt,
               "gmail_id": e.get("gmail_id") or ""}   # source message id -> the backfill sweep dedups on it
        if hv:
            req["high_value"] = True
            _hvd = _prof.get("high_value_attach_doc")   # RFP-class first contact: the company profile rides along
            if _hvd and not is_thread:
                _doc = db.one("select id, filename, mime, size from company_documents where id=%s", (int(_hvd),))
                if _doc:
                    req["attach_docs"] = [dict(_doc)]
        atts = _inbound_att_refs(e, rt_key, _inbox_client_company(co.get("slug")))
        if atts:
            req["inbound_attachments"] = atts
            req["brief"] += (" Their email includes attachment(s): "
                             + ", ".join(a["filename"] or a["mime"] for a in atts)
                             + " — they are provided to you; READ them before drafting and address their content.")
        if is_thread:
            req["thread_reply"] = True          # continuation: natural reply, no reference box
        if deal:
            req["deal_id"] = deal["id"]
        if e.get("thread_id"):                  # reply ON their Gmail thread, not a fresh conversation
            # threadIds are mailbox-local: when the reply routes through the send mailbox instead of the
            # catch-all that received it, thread by the global reply headers only
            req["thread_cc"] = _thread_participants(e, e.get("email") or "")
            req["thread"] = {"id": "" if catchall else e["thread_id"], "msg_id": e.get("msg_id") or "",
                             "references": e.get("references") or ""}
        # a NEW lead with no deal: qualify + auto-create the Opportunity, same as the enquiry lane —
        # "it definitely is an opportunity" should never depend on which door the email came through.
        if cls["category"] == "lead" and not deals and not dup:
            try:
                sug = qualify_suggest(co, inq)
                if sug and sug.get("verdict") == "qualified":
                    db.setting_set(f"qual:{co['id']}:{sender.lower()}", sug)
                    db.setting_set(f"qual:email:{sender.lower()}", {**sug, "company_id": co["id"]})
                    opp = crm.auto_opportunity(sender, co.get("slug"), sug, inq)
                    if opp:
                        req["deal_id"] = opp["id"]
                        _notify_new_opportunity(co, opp, "auto-qualified from direct email")
            except Exception:  # noqa: BLE001 — qualification is best-effort; the reply card must exist regardless
                pass
        # RE-CHECK the open-card state at WRITE time: qualification, attachment refs and the sticky-sender
        # lookup above can take many seconds, and the same email lands in several team mailboxes — a sibling
        # copy may have written a card in that gap (bit Sunwoo/ECBD: cards #358+#359, 2026-08-27).
        dup = db.one("select id, request from tasks where company_id=%s and kind='email_reply' and "
                     "status in ('new','drafting','awaiting_approval','awaiting_correction') and "
                     "lower(request->'inquiry'->>'email')=lower(%s) order by id desc limit 1",
                     (co["id"], sender))
        if dup:                                 # supersede the stale open card with their latest email
            oldreq = dup.get("request") or {}
            old = (oldreq.get("inquiry") or {}).get("message") or ""
            norm = lambda s: re.sub(r"\s+", " ", s or "").strip()[:600]   # noqa: E731
            if norm(old) == norm(inq["message"]):
                return                          # same email, another team mailbox's copy — keep the first card
            _base = lambda s: re.sub(r"^\s*((re|fwd|fw)\s*:\s*)+", "", (s or "").lower()).strip()  # noqa: E731
            same_conv = _base((oldreq.get("inquiry") or {}).get("subject")) == _base(inq.get("subject"))
            if same_conv and oldreq.get("mailbox_rt") and oldreq.get("mailbox_rt") != rt_key:
                # same conversation seen via another team mailbox: the card's sending mailbox is STICKY to
                # first receipt — threadIds are mailbox-local, so its thread/gmail ids stay from THAT mailbox
                req["from_email"] = oldreq.get("from_email") or req.get("from_email")
                req["mailbox_rt"] = oldreq["mailbox_rt"]
                req["gmail_id"] = oldreq.get("gmail_id") or req.get("gmail_id")
                if oldreq.get("thread"):
                    req["thread"] = oldreq["thread"]
            if old and old[:200] not in inq["message"]:
                req["brief"] += " Their EARLIER message in this thread (already on a card, reply to BOTH): " \
                                + old[:1500]
            store.update_task(dup["id"], status="new", draft=None, request=req)
            return
        store.create_card(co["id"], skill["id"], "email_reply", req, contact=sender)
    except Exception as ex:  # noqa: BLE001 — NEVER silent (audit: a swallowed failure here loses a client
        # email forever once it is marked seen). Alert + tell the caller NOT to mark it seen, so it retries.
        try:
            tg.send(f"(reply-card failed for {e.get('email')}: {str(ex)[:120]} — will retry next sweep)")
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


def poll_inbox(company_slug: str = "tabscanner", rt_key: str = "gmail_refresh_token",
               days: int = 2, limit: int = 40, commit: bool = True, company: str | None = None,
               address: str | None = None) -> dict:
    """Read a company's catch-all inbox (non-form mail; form notifications are handled by poll_inquiries),
    classify each email on the sales-triage skill, and route the meaningful ones into the CRM. Deduped per
    mailbox. commit=False is a dry run (classify + report, no CRM writes)."""
    co = store.get_company_by_slug(company_slug)
    if company is None:
        company = _inbox_client_company(company_slug)   # the OAuth client that minted this inbox's token
    if not co or not db.setting_get(rt_key):
        return {"processed": 0, "results": [], "reason": "no company / inbox not connected"}
    q = f'in:inbox newer_than:{days}d -subject:"New enquiry from"'
    own_domain = INBOXES.get(company_slug, "").split("@")[-1].lower()
    intl = _internal_index()                       # own/burner domains + internal roster + test group
    key = f"inbox_processed:{company_slug}"
    seen_list = list(db.setting_get(key) or []); seen = set(seen_list)
    emails = gmail.list_recent(days=days, limit=limit, rt_key=rt_key, q=q, skip=seen, company=company)
    results, added = [], 0
    for e in emails:
        gid = e.get("gmail_id")
        if not gid or gid in seen:
            continue
        if _is_internal(e.get("email"), own_domain, intl):   # our own / internal address: never classify or CRM
            if commit:
                seen.add(gid)
            results.append({"from": e.get("email"), "subject": (e.get("subject") or "")[:60],
                            "category": "internal", "to_crm": False, "reason": "own/internal address"})
            continue
        if _belongs_to_other_company(e, company_slug):   # another brand's conversation (aliased mailboxes)
            if commit:
                seen.add(gid)
            results.append({"from": e.get("email"), "subject": (e.get("subject") or "")[:60],
                            "category": "other-company", "to_crm": False,
                            "reason": "addressed to another of our companies"})
            continue
        cls = classify_email(co, e)
        # DETERMINISTIC client override: a sender on an ACTIVE deal/project is project correspondence,
        # whatever label the model picked (Haiku filed a MAH Gold project brief as 'support' -> no draft,
        # Aug 2026). Code decides from the CRM; the model only labels what the CRM doesn't know.
        if cls["category"] not in ("client", "lead", "finance") and e.get("email"):
            try:
                if crm.open_deal_for_email(e["email"], company_slug) or \
                        crm.open_deal_for_domain(e["email"], company_slug):
                    cls = {**cls, "category": "client", "to_crm": True,
                           "reason": "active deal/project in CRM (deterministic override)"}
            except Exception:  # noqa: BLE001 — a CRM hiccup must never break classification
                pass
        card_ok = True
        if commit and cls["category"] in ("lead", "client", "finance"):
            # NO-DRAFT POLICY: the owner's own rules can say a kind of message never gets a drafted
            # reply (support handled by Ben, etc.). That decision belongs HERE - a rule on the drafting
            # skill can never stop a card that already exists, which is why it kept being ignored.
            _skip = policy.should_skip(co, e)
            if _skip:
                results.append({"from": e.get("email"), "subject": (e.get("subject") or "")[:60],
                                "category": cls["category"], "to_crm": cls.get("to_crm"),
                                "reason": f"no draft - {_skip['reason']}"})
                seen.add(gid)
                continue
            card_ok = _draft_direct_reply(co, e, cls, rt_key=rt_key, address=address) is not False
        if commit:
            if cls["to_crm"] and e.get("email"):
                stage = "Engaged" if cls["category"] in ("lead", "partner", "support") else "Cold"
                try:
                    st, _ = crm.add_inbound_contact({"email": e["email"], "name": e["name"]},
                                                    company_slug, cls["category"], stage=stage,
                                                    newsletter=cls["category"] in _INBOX_NEWSLETTER,
                                                    summary=cls.get("summary"), market=cls.get("market"))
                    if st == "added":
                        added += 1
                except Exception:  # noqa: BLE001
                    pass
            if card_ok:      # a failed card leaves the mail unseen -> retried next sweep, never lost
                seen.add(gid)
        results.append({"from": e.get("email"), "subject": (e.get("subject") or "")[:60], **cls})
    if commit:
        db.setting_set(key, (seen_list + [g for g in seen if g not in set(seen_list)])[-3000:])
    return {"processed": len(results), "added_to_crm": added, "results": results}


def backfill_missed_client_drafts(slug: str = "sensa", days: int = 7, limit: int = 60,
                                  cap: int = 15) -> dict:
    """One-off RECOVERY sweep for the pre-override gap: re-read a company's registered mailboxes IGNORING
    the seen-set and draft replies for mail from senders on an ACTIVE deal that never got a card.
    Drafts approval cards only — never sends, never touches the CRM or the seen-sets. Run manually."""
    co = store.get_company_by_slug(slug)
    if not co:
        return {"reason": "no company"}
    own_domain = INBOXES.get(slug, "").split("@")[-1].lower()
    intl = _internal_index()
    made, skipped = 0, []
    for entry in inbox_registry():
        if entry.get("slug") != slug or not _inbox_connected(entry):
            continue
        rt = entry.get("rt_key") or _default_rt_key(slug)
        try:
            msgs = gmail.list_recent(days=days, limit=limit, rt_key=rt,
                                     q=f'in:inbox newer_than:{days}d -subject:"New enquiry from"',
                                     company=_inbox_client_company(slug))
        except Exception as ex:  # noqa: BLE001 — one dead mailbox must not kill the sweep
            skipped.append(f"{entry.get('address')}: {ex}")
            continue
        for m in reversed(msgs):               # oldest first, so a superseding card ends on the NEWEST mail
            sender = (m.get("email") or "").strip()
            gid = m.get("gmail_id") or ""
            if not sender or _is_internal(sender, own_domain, intl):
                continue
            if gid and db.one("select id from tasks where company_id=%s and kind='email_reply' and "
                              "request->>'gmail_id'=%s limit 1", (co["id"], gid)):
                continue                       # this exact message already has (or had) a card
            try:
                deal = crm.open_deal_for_email(sender, slug) or crm.open_deal_for_domain(sender, slug)
            except Exception:  # noqa: BLE001
                deal = None
            if not deal:
                continue
            cls = {"category": "client", "to_crm": False,
                   "reason": "backfill: active deal/project in CRM"}
            _draft_direct_reply(co, m, cls, rt_key=rt, address=entry.get("address"))
            made += 1
            if made >= cap:
                return {"drafted": made, "capped": True, "skipped": skipped}
    return {"drafted": made, "skipped": skipped}


# ---------- inbox registry: data-driven, so a NEW address auto-joins the classifier loop ----------
# The 60s loop polls EVERY connected inbox in this registry. Adding an inbox is data, not code:
# the OAuth onboarding flow calls register_inbox() + stores that inbox's read token, and the loop
# picks it up on the next cycle — no edit here. (`_inbox_connected` is the access seam the OAuth
# plan fills in: today = a stored Gmail refresh token; could become domain-wide delegation.)

def _default_rt_key(slug: str) -> str:
    # Every inbox gets its own namespaced token key (Tabscanner migrated off the legacy global key 2026-06-23,
    # so it now uses its own per-company Internal OAuth client + :tabscanner token like every other company).
    return f"gmail_refresh_token:{slug}"


def _inbox_client_company(slug: str) -> str | None:
    """Which per-company OAuth client minted this inbox's token — the slug if it has its own client file on
    the box, else None (the shared Cortex-system client, e.g. Tabscanner). Auto-detects new companies."""
    return slug if os.path.exists(f"/etc/cortex/google_oauth_client_{slug}.json") else None


def inbox_registry() -> list[dict]:
    """The configured catch-all inboxes [{slug, address, rt_key}]. Stored in settings (data-driven);
    seeded from the INBOXES map the first time so existing config carries over."""
    reg = db.setting_get("inbox_registry")
    if not reg:
        reg = [{"slug": s, "address": a, "rt_key": _default_rt_key(s)} for s, a in INBOXES.items()]
        db.setting_set("inbox_registry", reg)
    return reg


def register_inbox(slug: str, address: str, rt_key: str | None = None) -> dict:
    """Plug a new inbox into the classifier (called by the OAuth onboarding flow). Idempotent."""
    entry = {"slug": slug, "address": address, "rt_key": rt_key or _default_rt_key(slug)}
    reg = [e for e in inbox_registry() if not (e.get("slug") == slug and e.get("address") == address)]
    reg.append(entry)
    db.setting_set("inbox_registry", reg)
    INBOXES.setdefault(slug, address)   # keep the own-domain lookup in sync
    return entry


def _inbox_connected(entry: dict) -> bool:
    """Does Cortex have read access to this inbox yet? (Access seam — extend for the OAuth plan.)"""
    return bool(db.setting_get(entry.get("rt_key", "")))


def poll_all_inboxes() -> dict:
    """Classify + CRM-route every CONNECTED inbox in the registry. Unconnected ones are skipped
    silently, so the loop never errors on an inbox we don't have access to yet."""
    polled = []
    for e in inbox_registry():
        if not _inbox_connected(e):
            continue
        try:
            poll_inbox(e["slug"], e.get("rt_key") or _default_rt_key(e["slug"]), address=e.get("address"))
            polled.append(e["slug"])
        except Exception as ex:  # noqa: BLE001
            tg.send(f"(inbox classify hiccup [{e.get('slug')}]: {ex})")
    return {"polled": polled}


def _bare_email(s: str) -> str:
    m = re.search(r"<([^>]+@[^>]+)>", s or "")
    if m:
        return m.group(1).strip().lower()
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", s or "")
    return m.group(0).strip().lower() if m else ""


def _reply_followup_brief(inq: dict, co: dict | None = None) -> str:
    """Frame a lead's REPLY (ongoing thread) as a 'draft the next message' brief for the worker."""
    return ("This is a REPLY from a lead in an ONGOING sales conversation (not a new website enquiry). Read "
            "their message and draft OUR next reply, continuing the thread — the THREAD-continuation "
            "standing rules on the sales-followup skill govern how. Plain-text email body only.\n\n"
            f"Their name: {inq.get('name') or 'there'}\n"
            f"Their email: {inq.get('email') or '(unknown)'}\n"
            f"Their latest message:\n{(inq.get('message') or '').strip()}"
            ) + _booking_slots_brief(co)


def poll_sales_replies(slug: str = "sensa") -> dict:
    """Read the SALES mailbox where replies to our outbound land (e.g. gino@sensa.digital, via the send token) for
    NEW replies from leads we are ALREADY in conversation with, and for each: draft OUR next reply (continue the
    qualifying thread) + re-run qualification on the new info, both into the Inbox. Tightly filtered — only acts on
    a 'Re:' from a captured lead of THIS company, never the rest of the mailbox. Drafts only; nothing auto-sends."""
    co = store.get_company_by_slug(slug)
    if not co:
        return {"reason": "no company"}
    send_rt, client = f"gmail_send_refresh_token:{slug}", slug
    if not db.setting_get(send_rt):
        if slug == "tabscanner" and db.setting_get("gmail_send_refresh_token"):
            send_rt, client = "gmail_send_refresh_token", None   # legacy global Tabscanner send mailbox
        else:
            return {"reason": "no sales mailbox token"}
    seen = list(db.setting_get(f"sales_replies_seen:{slug}") or [])
    seen_set = set(seen)
    idx = _internal_index()
    own_domain = (INBOXES.get(slug, "") or "").split("@")[-1]
    try:
        msgs = gmail.list_recent(days=14, limit=30, rt_key=send_rt, company=client, q="newer_than:14d", skip=seen_set)
    except Exception as e:  # noqa: BLE001
        return {"reason": f"read failed: {e}"}
    skill = store.get_skill_by_key(co["id"], "sales-first-response")
    made = 0
    for m in msgs:
        gid = m.get("gmail_id")
        if not gid or gid in seen_set:
            continue
        seen.append(gid); seen_set.add(gid)
        frm = _bare_email(m.get("from") or m.get("from_email") or "")
        subj = (m.get("subject") or "").strip()
        body = (m.get("body") or m.get("snippet") or "").strip()
        if not frm or _is_internal(frm, own_domain, idx):           # never us / internal
            continue
        if not subj.lower().startswith("re:"):                       # only a reply to a thread we started
            continue
        if _belongs_to_other_company(m, slug):                       # aliased mailboxes: not this brand's thread
            continue
        prior = db.setting_get(f"qual:email:{frm}")                  # ONLY continue threads with leads from OUR
        if not prior or prior.get("company_id") != co["id"]:         # sales funnel (came through enquiry intake) —
            continue                                                 # never cold mail / vendors pitching us
        if db.one("select id from tasks where company_id=%s and kind='email_reply' and "
                  "status in ('new','drafting','awaiting_approval','awaiting_correction') and "
                  "lower(request->'inquiry'->>'email')=lower(%s) limit 1", (co["id"], frm)):
            continue    # an open reply card for this sender already exists (the inbox sweep got there) — never double up
        c = db.one("select first_name, last_name from crm_master where lower(email)=lower(%s)", (frm,))
        nm = ((((c or {}).get("first_name")) or "") + " " + (((c or {}).get("last_name")) or "")).strip()
        name = nm if re.search(r"[A-Za-z]", nm) else frm.split("@")[0]
        inq = {"name": name, "email": frm, "message": body,
               "subject": subj if subj.lower().startswith("re:") else f"Re: {subj}"}
        if skill:
            fu = {"brief": _reply_followup_brief(inq, co), "inquiry": inq, "thread_reply": True,
                  "qual_suggest": prior}
            if m.get("thread_id"):
                fu["thread"] = {"id": m["thread_id"], "msg_id": m.get("msg_id") or "",
                                "references": m.get("references") or ""}
            fu_atts = _inbound_att_refs(m, send_rt, client)
            if fu_atts:
                fu["inbound_attachments"] = fu_atts
                fu["brief"] += (" Their reply includes attachment(s): "
                                + ", ".join(a["filename"] or a["mime"] for a in fu_atts)
                                + " — they are provided to you; READ them before drafting.")
            store.create_card(co["id"], skill["id"], "email_reply", fu, contact=frm)
        try:                                                         # re-qualify on the new info
            sug = qualify_suggest(co, inq)
            if sug:
                db.setting_set(f"qual:{co['id']}:{frm}", sug)
                db.setting_set(f"qual:email:{frm}", {**sug, "company_id": co["id"]})
                opp = crm.auto_opportunity(frm, slug, sug, inq)      # idempotent — skips if a deal is open
                if opp:
                    _notify_new_opportunity(co, opp, "auto-qualified from the reply thread")
        except Exception:  # noqa: BLE001
            pass
        db.setting_set(f"lead_fu:{co['id']}:{frm}", None)   # they replied -> stop the silence chase
        made += 1
    db.setting_set(f"sales_replies_seen:{slug}", seen[-500:])
    if made:
        tg.send(f"[{co['name']}] {made} lead repl{'y' if made == 1 else 'ies'} -> drafted the next message in your Inbox.")
    return {"drafted": made}


def poll_all_sales_replies() -> dict:
    """Read EVERY company's sales/send mailbox for lead replies -> re-qualify + draft the next message.
    A company without a send-mailbox token is skipped silently (fail-soft), so wiring a new send mailbox
    auto-joins the loop on the next cycle."""
    polled = []
    for slug in INBOXES:
        try:
            r = poll_sales_replies(slug)
            if not (r or {}).get("reason"):
                polled.append(slug)
        except Exception as ex:  # noqa: BLE001
            tg.send(f"(sales reply poll hiccup [{slug}]: {ex})")
    return {"polled": polled}


# ---------- pre-qualification SILENCE chase: don't let a captured lead fade away -----------------------
# A lead got in touch, we replied, they went quiet (and aren't qualified yet). Chase them on a cadence until
# they respond, then drop. CADENCE IS CONFIG (per-company `lead_followup_cadence` override else this default).
LEAD_CADENCE = {
    "skip_weekends": True,
    "steps": [
        {"after_days": 3, "repeat": 3, "action": "chase"},   # 3 chases, 3 days apart (skipping weekends)
        {"after_days": 7, "repeat": 2, "action": "chase"},   # then weekly for two weeks
    ],                                                       # then drop (mark dormant)
}


def _lead_cadence(co: dict) -> dict:
    cad = (profile.get(co["id"]) or {}).get("lead_followup_cadence") if co else None
    return cad if (cad and cad.get("steps")) else LEAD_CADENCE


def _arm_lead_followup(cid: int, email: str, step: int | None = None) -> None:
    """Arm/refresh the silence-chase clock for a funnel lead (we just contacted them). Keeps the current step,
    restarting the gap from now, unless a step is given. No-op once the cadence is exhausted."""
    co = store.get_company(cid)
    key = f"lead_fu:{cid}:{email.lower()}"
    cur = db.setting_get(key) or {}
    s = step if step is not None else int(cur.get("step") or 0)
    nxt = crm._schedule_point(_lead_cadence(co), s)
    if nxt is not None:
        db.setting_set(key, {"step": s, "next_at": nxt.isoformat()})


def _lead_chase_brief(inq: dict, co: dict | None = None) -> str:
    return ("This is a SILENCE CHASE: the lead got in touch, we replied, they have not responded yet. Draft "
            "the nudge — the SILENCE-chase standing rules on the sales-followup skill govern the tone and "
            "shape. Plain-text body only.\n\n"
            f"Their name: {inq.get('name') or 'there'}\n"
            f"Their email: {inq.get('email')}"
            ) + _booking_slots_brief(co)


def _spawn_lead_chase(co: dict, email: str) -> None:
    skill = store.get_skill_by_key(co["id"], "sales-first-response")
    if not skill:
        return
    c = db.one("select first_name, last_name from crm_master where lower(email)=lower(%s)", (email,))
    nm = ((((c or {}).get("first_name")) or "") + " " + (((c or {}).get("last_name")) or "")).strip()
    name = nm if re.search(r"[A-Za-z]", nm) else email.split("@")[0]
    inq = {"name": name, "email": email, "subject": "Re: your enquiry", "message": ""}
    store.create_card(co["id"], skill["id"], "email_reply",
                      {"brief": _lead_chase_brief(inq, co), "inquiry": inq, "lead_chase": True,
                       "system_note": "Silence chase: this lead has not replied to our last message."},
                      contact=email)


def run_lead_followups() -> dict:
    """Chase funnel leads who went silent BEFORE qualifying: per LEAD_CADENCE draft a gentle nudge into the Inbox,
    then DROP (mark dormant) once exhausted. Self-clears a lead that has since been qualified or disqualified."""
    now = datetime.now(timezone.utc)
    fired = []
    for r in db.query("select key, value from settings where key like %s", ("lead_fu:%",)):
        v = r["value"] or {}
        try:
            if not v.get("next_at") or datetime.fromisoformat(v["next_at"]) > now:
                continue
        except Exception:  # noqa: BLE001
            continue
        parts = r["key"].split(":")                       # lead_fu:{cid}:{email}
        if len(parts) < 3:
            db.setting_set(r["key"], None); continue
        cid, email = int(parts[1]), parts[2]
        co = store.get_company(cid)
        if not co:
            continue
        cl = (db.one("select classification from crm_master where lower(email)=lower(%s)", (email,)) or {}).get("classification")
        if cl == "not_qualified":                          # disqualified -> stop chasing
            db.setting_set(r["key"], None); continue
        pts = crm.cadence_points(_lead_cadence(co))
        step = int(v.get("step") or 0)
        if step >= len(pts):                               # cadence exhausted -> drop them
            db.setting_set(r["key"], None)
            try:
                crm.set_contact_stage(email, "Dormant/dead")
            except Exception:  # noqa: BLE001
                pass
            tg.send(f"[{co['name']}] lead {email} dropped — no reply after {len(pts)} chases.")
            continue
        _spawn_lead_chase(co, email)
        nstep = step + 1
        nxt = crm._schedule_point(_lead_cadence(co), nstep) or crm._roll_weekend(now + timedelta(days=7), True)
        db.setting_set(r["key"], {"step": nstep, "next_at": nxt.isoformat()})
        fired.append([email, step])
    return {"fired": fired}


# ---------- scheduled tasks (recurring jobs -> Inbox) ----------

REPORT_SKILL_KEY = "content-onpage-seo"   # the SEO report lands under the company's SEO lane


def _generate_seo_report(company: str, days: int = 28) -> dict:
    """Generate the per-company SEO/traffic report; returns the pieces needed to fill a report card."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    rep = seo_report.generate(company, days=days, out_dir=REPORTS_DIR)
    co = store.get_company_by_slug(company)
    if not co:
        raise ValueError(f"unknown company {company}")
    skill = store.get_skill_by_key(co["id"], REPORT_SKILL_KEY)
    req = {"kind": "seo_report", "company": company, "file": rep["path"],
           "title": rep["title"], "summary": rep["summary"], "days": days}
    return {"company_id": co["id"], "skill_id": skill["id"] if skill else None,
            "request": req, "summary": rep["summary"]}


def deliver_seo_report(company: str, days: int = 28) -> dict:
    """One-off path (manual 'generate now' / Talk run_report): generate the report and drop a fresh card."""
    g = _generate_seo_report(company, days)
    return db.execute(
        "insert into tasks (company_id,skill_id,kind,request,draft,status) "
        "values (%s,%s,'report',%s,%s,'awaiting_approval') returning *",
        (g["company_id"], g["skill_id"], Json(g["request"]), g["summary"]))


PPC_SKILL_KEY = "ads-google-search"   # the PPC report lands under the company's Google ads lane


def _generate_ppc_report(company: str, days: int = 1) -> dict:
    """Generate the daily Google Ads PPC report; returns the pieces needed to fill a report card."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    rep = ppc_report.generate(company, days=days, out_dir=REPORTS_DIR)
    co = store.get_company_by_slug(company)
    if not co:
        raise ValueError(f"unknown company {company}")
    skill = store.get_skill_by_key(co["id"], PPC_SKILL_KEY)
    req = {"kind": "ppc_report", "company": company, "file": rep["path"],
           "title": rep["title"], "summary": rep["summary"], "days": days}
    return {"company_id": co["id"], "skill_id": skill["id"] if skill else None,
            "request": req, "summary": rep["summary"]}


def deliver_ppc_report(company: str = "sensa", days: int = 1) -> dict:
    """One-off path: generate the PPC report now and drop a downloadable card in the Inbox."""
    g = _generate_ppc_report(company, days)
    return db.execute(
        "insert into tasks (company_id,skill_id,kind,request,draft,status) "
        "values (%s,%s,'report',%s,%s,'awaiting_approval') returning *",
        (g["company_id"], g["skill_id"], Json(g["request"]), g["summary"]))


QUOTES_DIR = "/opt/coretex/quotations"     # generated quotation PDFs (persisted, served to the Inbox)
QUOTE_SKILL_KEY = "sales-quotation"        # quotes land under the company's Sales & Inquiries lane


def deliver_quotation(company: str, *, preset: str = "ai-production", customer: str = "",
                      total: float | None = None, total_inclusive: bool = False, sections: list | None = None,
                      title: str | None = None, note: str | None = None, fmt: str = "both",
                      contact_email: str | None = None) -> dict:
    """Render a quotation and drop it in the Inbox as a downloadable card (kind='quotation'). `fmt` = 'both'
    (default: editable .xlsx + ready-to-send .pdf), 'xlsx', or 'pdf'; both share one quote number. Delivery
    copies stored in R2 under <slug>/quotations/draft/. Prices come from the request (a stated total split by
    weight, or explicit line units); the model never invents a figure. See quotation.py."""
    os.makedirs(QUOTES_DIR, exist_ok=True)
    co = store.get_company_by_slug(company)
    if not co:
        raise ValueError(f"unknown company {company}")
    kw = dict(customer=customer, total=total, total_inclusive=total_inclusive, sections=sections,
              title=title, note=note, contact_email=contact_email, out_dir=QUOTES_DIR)
    want_pdf = fmt in ("both", "pdf")
    want_xlsx = fmt in ("both", "xlsx")
    # The house-format .xlsx is the single source of truth; the PDF is that same sheet converted by
    # LibreOffice, so both files match exactly (fonts + layout). Always render the xlsx (it's the PDF source).
    x = quotation.generate_xlsx(company, preset, **kw)
    number = x["number"]
    pdf_path = None
    if want_pdf:
        try:
            pdf_path = quotation.xlsx_to_pdf(x["path"], QUOTES_DIR)
        except Exception as e:  # noqa: BLE001 — never lose the card if the converter hiccups
            tg.send(f"Quotation {number} PDF conversion failed ({e}); the spreadsheet is still delivered.")
    xlsx_path = x["path"] if want_xlsx else None   # pdf-only still renders the xlsx as the source, just not delivered

    def _r2(path, kind):
        if not path:
            return None
        try:   # R2 is the delivery library (Company Standard); never let an upload hiccup lose the Inbox card
            return media.put_file(company, "quotations", path, status="draft")
        except Exception as e:  # noqa: BLE001
            tg.send(f"Quotation {number} {kind} rendered but R2 upload failed ({e}); the Inbox download works.")
            return None

    drive_note = _push_quote_to_client_drive(co, customer, number, x, pdf_path)
    # ...and straight into the DOCUMENT LIBRARY, so it can be attached to an email card immediately
    # instead of waiting for the hourly client-folder sync (owner, 31 Aug: "update the quotation to
    # today's date and share them both").
    try:
        if pdf_path:
            with open(pdf_path, "rb") as _fh:
                _doc = documents.save(co["id"], co.get("slug") or company,
                                      (drive_note or {}).get("file_name") or os.path.basename(pdf_path),
                                      "application/pdf", _fh.read(),
                                      kind="quotation", uploaded_by=f"quotation:{number}", push=False)
            # DRIVE IS THE CANONICAL HOME: point the library row at the file just filed in the client
            # folder, so the box copy is only a cache of it (owner, 31 Aug) - not a second original.
            _fid = (drive_note or {}).get("file_id") or (drive_note or {}).get("id")
            if _doc and _fid:
                db.execute("update company_documents set drive_id=%s, client=%s, verified_at=now() "
                           "where id=%s", (_fid, (drive_note or {}).get("client") or customer, _doc["id"]))
    except Exception:  # noqa: BLE001 — the card and the Drive copy still stand
        pass
    skill = store.get_skill_by_key(co["id"], QUOTE_SKILL_KEY)
    req = {"kind": "quotation", "company": company, "client_drive": drive_note,
           "file": pdf_path, "r2_url": _r2(pdf_path, "pdf"),
           "xlsx_file": xlsx_path, "xlsx_r2_url": _r2(xlsx_path, "xlsx"),
           "number": number, "title": x["title"], "summary": x["summary"], "customer": customer,
           "preset": preset, "total": x["total"], "currency": x["currency"], "blanks": x["blanks"]}
    return db.execute(
        "insert into tasks (company_id,skill_id,kind,request,draft,status,origin,title) "
        "values (%s,%s,'quotation',%s,%s,'awaiting_approval','talk',%s) returning *",
        (co["id"], skill["id"] if skill else None, Json(req), x["summary"], x["title"]))


def _push_quote_to_client_drive(co: dict, customer: str, number: str, x: dict, pdf_path: str | None) -> dict:
    """House filing rule: every quotation version lands in the client's folder under SENSA CLIENTS on the
    shared drive, named `<number> vN - YYYY-MM-DD`, so the team can review and relabel amendments. The
    client folder is found-or-created with duplicate protection; a near-duplicate name blocks filing and
    is reported on the card instead. Fail-soft: a Drive hiccup never loses the quotation card."""
    from . import drive as _drive
    try:
        prof = profile.get(co["id"]) or {}
        parent = (prof.get("clients_drive_folder") or "").strip()
        client = (customer or "").split(",")[0].strip()
        if not parent or not client:
            return {"filed": False, "reason": "no clients_drive_folder configured" if not parent else "no client name"}
        tok = _drive.access_token()
        f = _drive.ensure_client_folder(client, parent, token=tok)
        if not f.get("id"):
            return {"filed": False, "reason": "near-duplicate client folders — pick one and file manually",
                    "candidates": f.get("candidates")}
        # version = count of existing files for this quote number, + 1
        import httpx as _hx
        r = _hx.get(f"{_drive.API}/files", params={
            "q": f"'{f['id']}' in parents and name contains '{number}' and trashed=false",
            "includeItemsFromAllDrives": "true", "supportsAllDrives": "true", "fields": "files(id)"},
            headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        n = (len(r.json().get("files", [])) // 2) + 1 if r.status_code == 200 else 1
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # House naming convention (searchable in Drive): Client - Project - Quotation <number> vN - date
        project = (x.get("project") or "").strip()
        if not project:
            d = db.one("select title from crm_projects p join crm_master m on m.account_id=p.account_id "
                       "where lower(m.email)=lower(%s) and p.stage not in ('Lost','Close & review') "
                       "order by p.id desc limit 1", ((x.get("contact_email") or ""),)) if x.get("contact_email") else None
            project = ((d or {}).get("title") or "").split(" - ")[-1].strip()
        base = " - ".join(v for v in (client, project, f"Quotation {number}") if v)
        uploaded = []
        pdf_id = pdf_name = None
        _XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        for path, mime, ext in ((x.get("path"), _XLSX, "xlsx"),
                                (pdf_path, "application/pdf", "pdf")):
            if path and os.path.exists(path):
                name = f"{base} v{n} - {stamp}.{ext}"
                _fid = _drive.upload_to_folder(f["id"], name, mime, open(path, "rb").read(), token=tok)
                uploaded.append(name)
                if ext == "pdf":     # the library points at THIS file as the canonical original
                    pdf_id, pdf_name = _fid, name
        try:   # the living ALL VERSIONS workbook: one spreadsheet, a tab per iteration, updated in place
            vp = quotation.build_versions_workbook(x.get("company") or co.get("slug"), number, QUOTES_DIR)
            if vp:
                _drive.upsert_in_folder(f["id"], f"{base} - ALL VERSIONS.xlsx", _XLSX,
                                        open(vp, "rb").read(), token=tok)
                uploaded.append("ALL VERSIONS workbook updated")
        except Exception:  # noqa: BLE001 — the per-version files are already filed
            pass
        return {"filed": True, "folder": f["name"], "folder_id": f["id"], "client": f["name"],
                "folder_created": f.get("created"), "version": n, "files": uploaded,
                "file_id": pdf_id, "file_name": pdf_name}
    except Exception as e:  # noqa: BLE001
        return {"filed": False, "reason": str(e)[:120]}


def _run_report_task(task: dict) -> None:
    """A scheduled report INSTANCE (a 'seo_report'/'ppc_report' child spawned by the unified clock): generate
    the report and turn THIS task into the finished report card in place — one row per occurrence."""
    req = task.get("request") or {}
    company = req.get("company") or (store.get_company(task["company_id"]) or {}).get("slug")
    if task["kind"] == "ppc_report" or req.get("kind") == "ppc_report":
        g = _generate_ppc_report(company, req.get("days", 1))
    else:
        g = _generate_seo_report(company, req.get("days", 28))
    store.update_task(task["id"], kind="report", skill_id=g["skill_id"],
                      request=g["request"], draft=g["summary"], status="awaiting_approval")


def _run_newsletter_scheduled_task(task: dict, skill: dict | None, company: dict | None) -> None:
    """A scheduled newsletter's 1st-of-month arrived (this one-off task was promoted to 'new'). If the company
    is on AUTO, drip it out now; else turn THIS task into the Stage-3 'confirm to send' card in the Inbox."""
    company = company or store.get_company(task["company_id"])
    cid = task["company_id"]
    art = db.setting_get(f"newsletter:{task['id']}")
    if not art:
        store.update_task(task["id"], status="failed", last_status="no built newsletter found")
        return
    if db.setting_get(f"nl_auto:{cid}"):
        recips = newsletter.recipients(cid)
        per_hour = int(db.setting_get("newsletter_per_hour") or newsletter.DEFAULT_PER_HOUR)
        newsletter.enqueue_send(cid, task["id"], art, recips, per_hour)
        store.update_task(task["id"], kind="newsletter_send", schedule_kind=None, run_at=None,
                          status="done", last_status="auto-sent")
        tg.send(f"📤 Auto-sending {company['name']} newsletter '{art['subject']}' to {len(recips):,} "
                f"contacts (drip {per_hour}/hr).")
        return
    n = len(newsletter.recipients(cid))
    store.update_task(task["id"], kind="newsletter_send", schedule_kind=None, run_at=None,
                      draft=f"Subject: {art['subject']}\n\nScheduled for today. Confirm to send to the full "
                            f"{company['name']} list ({n:,} contacts).", status="awaiting_approval")
    tg.send(f"🗓 {company['name']} newsletter due today: '{art['subject']}'. Confirm the send in your Inbox.")


def _run_blog_scheduled_task(task: dict, skill: dict | None, company: dict | None) -> None:
    """A queued blog's publishing day arrived -> publish the staged WordPress draft live (go_live)."""
    info = db.setting_get(f"wp:{task['id']}") or {}
    pid = info.get("post_id")
    site = wp.for_company(company) if company else None
    result = site.go_live(pid) if (site and pid) else {}
    store.update_task(task["id"], status="done", last_status="published")
    store.log_decision(task["id"], task.get("skill_id"), "system", "blog_published",
                       snapshot={"post_id": pid, "link": result.get("link"), "title": info.get("title")})
    if company:
        tg.send(f"🗓 [{company['name']}] blog '{info.get('title','')}' published live on schedule. "
                f"{result.get('link','')}")
        # INBOUND internal linking: now it is a real published URL, seed back-links from relevant existing posts.
        if pid and result.get("link"):
            try:
                from . import blog
                rep = blog.seed_inbound_links(company, pid, result["link"], info.get("title") or "")
                title = info.get("title", "")
                if rep:
                    lines = "\n".join(f"- {r['title']} (anchor: \"{r['anchor']}\")" for r in rep)
                    body = f"{len(rep)} existing post(s) now link to '{title}':\n{lines}"
                    tg.send(f"🔗 [{company['name']}] {body}")
                    notifications.notify(f"Internal links seeded: {len(rep)} post(s) now link to '{title}'",
                                         body, category="report", company_id=company["id"],
                                         target_type="task", target_id=str(task["id"]))
                else:
                    tg.send(f"🔗 [{company['name']}] no inbound links seeded for '{title}' (no clearly relevant posts).")
            except Exception as e:  # noqa: BLE001
                tg.send(f"(inbound link seeding hiccup: {e})")


# ---------- Phase 3: the unified clock (scheduled tasks live in `tasks`, not scheduled_tasks) ----------

def _spawn_recurring_child(template: dict) -> dict:
    """One occurrence of a recurring template -> a fresh immediate task that flows through the normal
    draft -> manager -> Inbox pipeline (its own approval + history). The template itself never executes."""
    return db.execute(
        "insert into tasks (company_id,skill_id,kind,request,status,origin,title,parent_id) "
        "values (%s,%s,%s,%s,'new',%s,%s,%s) returning *",
        (template["company_id"], template["skill_id"], template["kind"],
         Json(template.get("request") or {}), template.get("origin") or "calendar",
         template.get("title"), template["id"]))


def run_template_now(tid: int) -> dict | None:
    """Manually fire a scheduled/recurring task template right now: spawn a child the engine picks up
    (its schedule/next_run is untouched — a manual run doesn't move the cadence)."""
    t = store.get_task(tid)
    if not t or t.get("schedule_kind") is None:
        return None
    return _spawn_recurring_child(t)


def promote_due_tasks() -> None:
    """Unified clock (60s tick): turn DUE scheduled tasks (held in `tasks` with status='scheduled') into work.
      one-off   (schedule_kind='once', run_at<=now)  -> flip to 'new' (runs once via process_new_tasks).
      recurring (schedule_kind='recurring', next_run<=now) -> spawn a child 'new' task + bump next_run.
    No-op until the Calendar/Talk (or the 3.4 migration) creates scheduled tasks."""
    for t in db.query("select id, coalesce((request->>'approved_send')::bool, false) ap from tasks "
                      "where schedule_kind='once' and status='scheduled' and coalesce(enabled,true)=true "
                      "and run_at is not null and run_at <= now()"):
        if t["ap"]:      # already owner-approved (approve & schedule): EXECUTE the frozen draft
            try:
                task, skill, company = _load(t["id"])
                db.execute("update tasks set status='awaiting_approval', last_run=now(), updated_at=now() "
                           "where id=%s", (t["id"],))
                task = store.get_task(t["id"])
                res = _approve(task, skill, company, stepped_up=True)
                tg.send(f"⏱ Scheduled send fired: {(task.get('title') or 'card')[:60]} — "
                        f"{(res or {}).get('sent_to') or 'done'}")
            except Exception as e:  # noqa: BLE001 — never lose the card: park it back for manual approval
                db.execute("update tasks set status='awaiting_approval', updated_at=now() where id=%s", (t["id"],))
                tg.send(f"⚠️ Scheduled send FAILED for card {t['id']}: {e}. It is back in the Inbox.")
            continue
        db.execute("update tasks set status='new', last_run=now(), updated_at=now() where id=%s", (t["id"],))
    for t in db.query("select * from tasks where schedule_kind='recurring' and coalesce(enabled,true)=true "
                      "and next_run is not null and next_run <= now()"):
        try:
            _spawn_recurring_child(t)
            nr = schedule.next_run(t.get("cadence") or "weekly", t.get("weekday") or 0,
                                   8 if t.get("hour") is None else t["hour"], t.get("minute") or 0)
            db.execute("update tasks set last_run=now(), next_run=%s, last_status='ok', updated_at=now() "
                       "where id=%s", (nr, t["id"]))
        except Exception as e:  # noqa: BLE001 — one bad template must not stall the rest
            db.execute("update tasks set last_status=%s where id=%s", (f"error: {e}"[:120], t["id"]))


def drain_newsletter_sends() -> None:
    """Push the next throttled batch of any in-flight newsletter, and alert when one finishes or auto-pauses."""
    for ev in newsletter.drain_send_jobs():
        co = store.get_company(ev["company_id"])
        coname = co["name"] if co else ""
        if ev["status"] == "done":
            t = store.get_task(ev["task_id"]) if ev.get("task_id") else None
            if t:
                store.log_decision(ev["task_id"], t["skill_id"], "owner", "newsletter_sent",
                                   note=ev["subject"], snapshot={"recipients": ev["sent"]})
            tg.send(f"✅ Newsletter fully sent: '{ev['subject']}' -> {ev['sent']:,}/{ev['total']:,} {coname} contacts.")
        elif ev["status"] == "paused":
            tg.send(f"⚠️ Newsletter PAUSED (bounce spike): '{ev['subject']}' at "
                    f"{ev['sent']:,}/{ev['total']:,} {coname}, {ev.get('bounces')} bounces. Check the list/domain.")


def _recover_stranded_tasks() -> None:
    """On startup, rescue any task orphaned in 'drafting' by the previous process. The engine restarts on every
    deploy; a long compose caught mid-flight would otherwise sit in 'drafting' forever and never reach the Inbox.
    Already has a draft -> surface it for approval; nothing drafted yet -> requeue it to run again cleanly."""
    rows = db.query("select id, draft from tasks where status='drafting'")
    for r in rows:
        store.update_task(r["id"], status="awaiting_approval" if (r.get("draft") or "").strip() else "new")
    if rows:
        tg.send(f"Recovered {len(rows)} task(s) stranded in 'drafting' by a restart.")
    # a card stuck in 'sending' means a crash happened MID-SEND: the email may or may not have left.
    # Never auto-retry that (a duplicate to a client is worse than a delay) — surface it for a human check.
    stuck = db.query("select id from tasks where status='sending'")
    for r in stuck:
        notifications.notify(f"Card #{r['id']} was interrupted MID-SEND by a restart. Check the mailbox's "
                             "Sent folder: if the email went, mark the card done; if not, set it back to "
                             "awaiting approval. It will NOT retry on its own.", "Interrupted send",
                             priority="high", category="reminder", target_type="task", target_id=str(r["id"]))
    if stuck:
        tg.send(f"⚠️ {len(stuck)} card(s) interrupted mid-send — check the Inbox notification before acting.")


def run(poll_idle: float = 1.0) -> None:
    tg.send("\U0001F9E0 Cortex engine online.")
    try:
        _recover_stranded_tasks()
    except Exception as e:  # noqa: BLE001
        tg.send(f"(startup recovery hiccup: {e})")
    last_poll = 0.0
    while True:
        try:
            process_new_tasks()
        except Exception as e:  # noqa: BLE001 — a single bad task / model timeout must NEVER kill the engine
            tg.send(f"(process hiccup: {e})")
        try:
            handle_updates()
        except Exception as e:  # noqa: BLE001
            tg.send(f"(updates hiccup: {e})")
        now = time.time()
        if now - last_poll >= 60:        # check Gmail for new enquiries + run any due scheduled tasks
            last_poll = now
            try:
                poll_inquiries()
            except Exception as e:  # noqa: BLE001
                tg.send(f"(enquiry poll hiccup: {e})")
            try:
                poll_all_inboxes()  # classify + CRM-route EVERY connected inbox (data-driven registry)
            except Exception as e:  # noqa: BLE001
                tg.send(f"(inbox classify hiccup: {e})")
            try:
                poll_company_forms()  # per-company contact-form intake -> triage -> CRM + drafted reply
            except Exception as e:  # noqa: BLE001
                tg.send(f"(form intake hiccup: {e})")
            try:
                poll_waitlists()  # website waitlist signups -> CRM waitlist subscribers (opt-in, no reply)
            except Exception as e:  # noqa: BLE001
                tg.send(f"(waitlist intake hiccup: {e})")
            try:
                reminders.fire_due()    # fire due reminders -> nudge notification or spawn an action task
            except Exception as e:  # noqa: BLE001
                tg.send(f"(reminder fire hiccup: {e})")
            try:
                documents.sync_all()    # hourly: pick up files hand-dropped into any CORTEX/Documents folder
            except Exception:  # noqa: BLE001 — background sync; never noisy
                pass
            try:
                meetnotes.sweep()       # hourly: Gemini meeting notes -> CRM + drafting context
            except Exception:  # noqa: BLE001
                pass
            try:
                store.promote_queued()  # conveyor: a dealt-with step releases the next queued card
            except Exception:  # noqa: BLE001
                pass
            try:
                pipeline.sweep_sent()   # sent folders: manual sends -> deal timelines, untracked flagged
            except Exception:  # noqa: BLE001
                pass
            try:
                from . import nurture
                nurture.sweep()         # account-level keep-warm touches (silent while live work exists)
            except Exception:  # noqa: BLE001
                pass
            try:
                run_opportunity_followups()   # advance AUTO opportunities' chase cadence -> drafted follow-up cards
            except Exception as e:  # noqa: BLE001
                tg.send(f"(opportunity follow-up hiccup: {e})")
            try:
                poll_all_sales_replies()   # every company's sales mailbox: lead replies -> draft the next message
            except Exception as e:  # noqa: BLE001
                tg.send(f"(sales reply poll hiccup: {e})")
            try:
                run_lead_followups()   # chase funnel leads who went silent BEFORE qualifying -> drafted nudge
            except Exception as e:  # noqa: BLE001
                tg.send(f"(lead follow-up hiccup: {e})")
            try:
                promote_due_tasks()   # the one unified clock — recurring templates + one-off scheduled tasks
            except Exception as e:  # noqa: BLE001
                tg.send(f"(promote hiccup: {e})")
            try:
                contentqueue.check_refills()   # rolling-N: nudge to ideate more when any content queue runs low
            except Exception as e:  # noqa: BLE001
                tg.send(f"(queue refill hiccup: {e})")
            try:
                drain_newsletter_sends()
            except Exception as e:  # noqa: BLE001
                tg.send(f"(newsletter drip hiccup: {e})")
        time.sleep(poll_idle)
