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
import os
import re
import secrets
import threading
import time
from datetime import datetime

from psycopg.types.json import Json

from . import (crm, db, gmail, manager, newsletter, notifications, profile, provider, reminders,
               schedule, seo_report, store, webauthn_auth, worker)
from .integrations import telegram as tg, wordpress as wp

MONEY_KINDS = {"payment", "invoice_send"}  # never auto, regardless of trust
EMAIL_KINDS = {"email_reply"}              # an inbound-reply, sent via Gmail on approval
EMAIL_SEND_KINDS = {"email_reply", "email_draft"}    # ALL kinds that actually SEND an email on approval
EMAIL_RENDER_KINDS = EMAIL_KINDS | {"email_draft"}   # rendered as an email (envelope + logo) in the Inbox
NEVER_AUTO_KINDS = {"newsletter_idea", "newsletter_review", "newsletter_send", "email_reply", "email_draft"}  # outward sends always need the owner
# PUBLIC actions (go OUT to the public) — approving these needs a biometric step-up (see
# feedback_public_actions_biometric). Internal items use the normal approve. Split by where the action fires:
_APPROVE_PUBLIC = {"email_reply", "email_draft", "newsletter_idea", "blog"}   # the action happens in approve_task
_CONFIRM_PUBLIC = {"newsletter_review", "newsletter_send"}        # the action happens in confirm_send_task

# Phase 3.2 — central kind -> security class (the single source of truth for gating; merged spec §3a).
#   internal : may auto-run on an auto lane with a clean manager verdict.
#   outward  : goes OUT to the public — NEVER auto; Inbox + PIN/biometric step-up to approve.
#   money    : never auto; human + PIN.
# Existing gate constants above stay the enforcers for now; this map is wired into the unified pipeline in
# 3.4/3.5. The assertion below keeps the two in lock-step so they can never silently diverge.
KIND_CLASS = {
    "content": "internal", "draft": "internal", "research": "internal", "summary": "internal",
    "report": "internal", "seo_report": "internal", "crm_update": "internal", "internal_note": "internal",
    "email_reply": "outward", "email_draft": "outward", "email_send": "outward", "blog": "outward",
    "newsletter_idea": "outward", "newsletter_review": "outward", "newsletter_send": "outward",
    "social_post": "outward", "dm_reply": "outward", "sms": "outward",
    "payment": "money", "invoice_send": "money", "refund": "money",
}


def kind_class(kind: str) -> str:
    """Security class for a task kind. Unknown kinds default to 'outward' (fail SAFE — never auto-send)."""
    return KIND_CLASS.get(kind, "outward")


# What tapping Approve ACTUALLY DOES, per kind — surfaced verbatim on the Inbox button so the consequence
# is never ambiguous (email sends, blog publishes, newsletter schedules/sends). Add a line when you add a kind.
APPROVE_ACTION = {
    "email_reply": "Approve & send", "email_draft": "Approve & send",
    "blog": "Approve & publish",
    "newsletter_idea": "Approve & build", "newsletter_review": "Approve & schedule",
    "newsletter_send": "Approve & send",
}


def approve_label(kind: str) -> str:
    return APPROVE_ACTION.get(kind, "Approve")


def is_auto_eligible(kind: str) -> bool:
    """Only 'internal' kinds may ever auto-run (and even then only on an auto lane with a clean verdict)."""
    return kind_class(kind) == "internal"


# guard: every currently-gated kind must be classed outward/money (so centralising can't loosen a gate)
assert all(kind_class(k) in ("outward", "money") for k in (NEVER_AUTO_KINDS | MONEY_KINDS)), \
    "KIND_CLASS drift: a never-auto/money kind is not classed outward/money"


def _biometric_gate(is_public: bool, stepup_token: str | None) -> dict | None:
    """For a PUBLIC approval, require a fresh fingerprint: returns a needs_biometric response if one wasn't
    provided, else None to proceed (consuming the step-up). No-op until a device is registered, so enabling
    this can never lock the operator out of existing flows."""
    if not is_public or not webauthn_auth.stepup_enabled():
        return None
    if webauthn_auth.consume_stepup(stepup_token):
        return None
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

def _email_brief(inq: dict) -> str:
    """Frame a website enquiry as a reply-drafting brief for the worker."""
    return ("Draft a reply to this website enquiry. Write it as a clean, professional plain-text email: "
            "normal sentences in short paragraphs. Do NOT use any markdown, no **bold**, no #headings, no "
            "[text](link) markdown links (write URLs plainly). Avoid bullet lists unless genuinely needed, "
            "and if so use a simple hyphen. Do NOT add any closing, sign-off, name or signature, those are "
            "appended automatically. Output ONLY the email body, no subject or headers. Reply directly to "
            "the person, in the company voice, following the standing rules.\n\n"
            f"Their name: {inq.get('name') or 'there'}\n"
            f"Their email: {inq.get('email') or '(unknown)'}\n"
            f"Their message:\n{(inq.get('message') or inq.get('snippet') or '').strip()}")


_EMAIL_RE = r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"


def _rule_recipients(skill: dict) -> tuple[list, list]:
    """Honour CC/BCC addresses the owner stated in this skill's standing rules (e.g. 'CC ben@… and BCC me@…')."""
    if not skill:
        return [], []
    uni, loc = store.effective_rules(skill)
    text = " ".join(list(uni) + list(loc))
    cc = re.findall(r"\bcc\s+" + _EMAIL_RE, text, re.I)     # \bcc won't match inside 'bcc' (no word boundary)
    bcc = re.findall(r"\bbcc\s+" + _EMAIL_RE, text, re.I)
    return cc, bcc


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
            rc, rb = _rule_recipients(store.get_skill(task.get("skill_id")))
            cc_list += rc
            bcc_list += rb
        except Exception:  # noqa: BLE001
            pass
    cc = ", ".join(dict.fromkeys(cc_list))     # dedupe, keep order
    bcc = ", ".join(dict.fromkeys(bcc_list))
    req = task.get("request") or {}
    outbound = bool(req.get("outbound"))   # a Talk-composed email_draft (not a reply) — no "Re:" prefix
    subj = inq.get("subject") or "your enquiry"
    return {"to": inq.get("email") or "", "to_name": inq.get("name") or "",
            "from": (req.get("from_email") or data.get("reply_from") or "").strip() or None,
            "cc": cc or None, "bcc": bcc or None,
            "subject": subj if outbound else ("Re: " + subj),
            "name": inq.get("name") or "", "signature": (data.get("signature") or "").strip()}


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


def compose_reply_body(task: dict, company: dict) -> str:
    """The plain-text body that will be sent: the cleaned reply plus the company signature (text). Also the
    fallback part of the multipart email, so non-HTML clients still get a clean message."""
    env = _email_envelope(task, company)
    body = _strip_signoff(_clean_email_text(task.get("draft") or ""))
    sig = (env.get("signature") or "").strip()
    if sig:
        body = body + "\n\n" + sig
    return body


# ---- HTML email (so the footer logo + formatting render) ----

LOGO_PATH = os.environ.get("CORTEX_TAB_LOGO", "/opt/cortex/assets/tabscanner-logo.png")


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


def _signature_html(plain_sig: str, logo_src: str | None) -> str:
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
    logo = (f"<img src='{logo_src}' alt='Tabscanner' width='150' "
            "style='display:block;border:0;margin:0 0 10px 0'>") if logo_src else ""
    return ("<div style='margin-top:20px;font-family:Arial,Helvetica,sans-serif;font-size:13px;"
            "color:#0A1828;line-height:1.55'><p style='margin:0 0 14px 0'>Best regards,</p>"
            f"{logo}{'<br>'.join(rows)}</div>")


def compose_reply_html(task: dict, company: dict, for_preview: bool = False) -> dict:
    """Returns {plain, html, inline} — the multipart email. For preview the logo is a data-URI (renders in
    the browser); for the real send it's a cid inline image."""
    env = _email_envelope(task, company)
    clean = _strip_signoff(_clean_email_text(task.get("draft") or ""))
    plain_sig = (env.get("signature") or "").strip()
    logo_src, inline = None, []
    if os.path.isfile(LOGO_PATH):
        if for_preview:
            logo_src = "data:image/png;base64," + _b64.b64encode(open(LOGO_PATH, "rb").read()).decode()
        else:
            logo_src, inline = "cid:tabscannerlogo", [("tabscannerlogo", LOGO_PATH)]
    html_body = ("<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#0A1828;"
                 f"line-height:1.6'>{_body_to_html(clean)}{_signature_html(plain_sig, logo_src)}</div>")
    return {"plain": compose_reply_body(task, company), "html": html_body, "inline": inline}


def _send_email_reply(task: dict, skill: dict, company: dict, actor: str, auto: bool) -> dict:
    if db.setting_get("email_sending_paused"):   # global kill-switch — keep the card, don't send
        store.update_task(task["id"], status="awaiting_approval")
        return {"blocked": True, "error": "Email sending is PAUSED. Resume it to send this reply."}
    env = _email_envelope(task, company)
    c = compose_reply_html(task, company, for_preview=False)
    req = task.get("request") or {}
    files = req.get("attachments")            # outbound drafts carry real file attachments
    file_names = req.get("attachment_names")  # ...with their original filenames
    # per-company send: a brand with its own project sends from its OWN mailbox/client (else Tabscanner legacy)
    slug = (company or {}).get("slug")
    send_company = _inbox_client_company(slug) if slug else None
    send_rt_key, from_addr = None, env["from"]
    if send_company:
        send_rt_key = (f"gmail_send_refresh_token:{slug}" if db.setting_get(f"gmail_send_refresh_token:{slug}")
                       else f"gmail_refresh_token:{slug}")
        from_addr = from_addr or db.setting_get(f"gmail_send_account:{slug}") or db.setting_get(f"gmail_account:{slug}")
    res = gmail.send_message(env["to"], env["subject"], c["plain"], from_addr=from_addr, cc=env["cc"],
                             html=c["html"], inline_images=c["inline"], bcc=env.get("bcc"),
                             files=files, file_names=file_names, company=send_company, send_rt_key=send_rt_key)
    try:
        crm.log_event(env["to"], "email_sent", f"Email sent: {env['subject']}", company.get("slug"))
    except Exception:  # noqa: BLE001 — CRM history must never block the send
        pass
    store.update_task(task["id"], status="done")
    store.log_decision(task["id"], skill["id"], actor, "send",
                       snapshot={"to": env["to"], "cc": env["cc"], "bcc": env.get("bcc"),
                                 "from": env["from"], "subject": env["subject"], "gmail_id": res.get("id")})
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
    if task["kind"] == "seo_report":   # a scheduled report instance — generate it, don't worker-draft it
        store.update_task(task["id"], status="drafting")
        _run_report_task(task)
        return
    if task["kind"] == "newsletter_scheduled":   # a scheduled newsletter's 1st-of-month arrived
        _run_newsletter_scheduled_task(task, skill, company)
        return
    if not skill:   # e.g. an action reminder pointed at a skill that doesn't exist — fail cleanly
        store.update_task(task["id"], status="failed",
                          manager={"summary": "No valid skill assigned — nothing drafted.", "aligned": False})
        tg.send(f"Task #{task['id']} couldn't run: no valid skill assigned.")
        return
    store.update_task(task["id"], status="drafting")

    site = _site_for(task, company)
    if site:
        _run_blog_task(task, skill, company, site)
        return

    draft = worker.draft(skill, company, task["request"])
    verdict = manager.check(skill, company, draft, task["request"])
    if not verdict["aligned"] and verdict["issues"]:
        draft = worker.draft(skill, company, task["request"], manager_feedback=verdict["issues"])
        verdict = manager.check(skill, company, draft, task["request"])

    task = store.update_task(task["id"], draft=draft, manager=verdict, attempts=task["attempts"] + 1)

    # Earned autonomy + escalation valve: even on an auto lane, the Manager's verdict must be a clean,
    # confident pass. Anything flagged, escalated, or low-confidence still goes to the owner.
    auto_ok = (skill["authority"] == "auto" and skill["stakes"] == "low"
               and not skill["paused"] and task["kind"] not in MONEY_KINDS
               and task["kind"] not in NEVER_AUTO_KINDS
               and verdict.get("aligned") and not verdict.get("escalate"))
    if auto_ok:
        _execute(task, skill, company, actor="cortex", auto=True)
    else:
        preview = _fmt_email(task, skill, company, verdict) if task["kind"] in EMAIL_KINDS \
            else _fmt(task, skill, company, verdict)
        msg = tg.send(preview, _approval_buttons(task["id"]))
        store.update_task(task["id"], status="awaiting_approval", tg_message_id=msg["message_id"])
        _push_approval(task, skill, company)


def _run_blog_task(task: dict, skill: dict, company: dict, site) -> None:
    art = worker.draft_article(skill, company, task["request"])
    body = f"TITLE: {art['title']}\n\n{art['html']}"
    verdict = manager.check(skill, company, body, task["request"])
    if not verdict["aligned"] and verdict["issues"]:
        art = worker.draft_article(skill, company, task["request"], manager_feedback=verdict["issues"])
        verdict = manager.check(skill, company, f"TITLE: {art['title']}\n\n{art['html']}", task["request"])

    post = site.stage_draft(art["title"], art["html"])  # unpublished WordPress draft
    db.setting_set(f"wp:{task['id']}", {"post_id": post["id"], "preview": post.get("preview"),
                                        "edit": post.get("edit"), "title": art["title"]})

    task = store.update_task(task["id"], draft=art["html"], manager=verdict, attempts=task["attempts"] + 1)
    # blog tasks ALWAYS go to the owner — never auto-publish (golden rule).
    msg = tg.send(_fmt_blog(company, skill, art, verdict, post.get("preview")), _blog_buttons(task["id"]))
    store.update_task(task["id"], status="awaiting_approval", tg_message_id=msg["message_id"])
    _push_approval(task, skill, company)


def _execute(task: dict, skill: dict, company: dict, actor: str, auto: bool = False) -> dict:
    if task["kind"] == "newsletter_idea":   # approve the idea -> build + send to the test group + review card
        return newsletter.execute_idea_approval(task, skill, company, actor)
    if task["kind"] in ("newsletter_review", "newsletter_send"):
        # real-list newsletter actions route through the cockpit count-confirm (schedule / send); a plain
        # approve (incl. Telegram) never fires one.
        n = len(newsletter.recipients(company["id"]))
        return {"needs_confirm": True, "recipients": n,
                "error": f"Confirm with the recipient count ({n:,}) in the cockpit."}
    if task["kind"] in EMAIL_SEND_KINDS:   # email_reply + email_draft both SEND on approval (after the PIN gate)
        return _send_email_reply(task, skill, company, actor, auto)
    site = _site_for(task, company)
    if site:
        info = db.setting_get(f"wp:{task['id']}") or {}
        pid = info.get("post_id")
        result = site.go_live(pid) if pid else {}  # clears the preview password -> public
        store.update_task(task["id"], status="done")
        store.log_decision(task["id"], skill["id"], actor, "publish",
                           snapshot={"post_id": pid, "link": result.get("link"), "title": info.get("title")})
        return result
    # Phase 1 string path: 'execute' = mark done + log.
    store.update_task(task["id"], status="done")
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


def _approve(task: dict, skill: dict, company: dict) -> dict:
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
        elif result and result.get("sent_to"):
            tg.edit(task["tg_message_id"],
                    f"✅ Approved — Cortex sent it to {result['sent_to']} (streak {skill['trust_streak']}).")
        else:
            tg.edit(task["tg_message_id"], f"✅ Approved — '{skill['name']}' (streak {skill['trust_streak']}). Done.")
    # Offer auto only for non-blog skills (blog publishing must never go auto).
    if task["kind"] != "blog" and skill["authority"] == "ask" and skill["trust_streak"] >= skill["auto_threshold"]:
        higher = skill["trust_streak"] + 20
        tg.send(f"'{skill['name']}' has {skill['trust_streak']} clean approvals. "
                f"Put it on auto for low-stakes work, or raise the bar for extra confidence?",
                [[tg.button("Yes, set auto", f"au:{skill['id']}"),
                  tg.button(f"No — raise to {higher}", f"th:{skill['id']}:{higher}")]])
    return result or {}


def _skip(task: dict, skill: dict, company: dict) -> None:
    site = _site_for(task, company)
    note = "Skipped"
    if site:
        info = db.setting_get(f"wp:{task['id']}") or {}
        if info.get("post_id"):
            try:
                site.trash(info["post_id"])
                note = "Discarded — draft removed from Tabscanner"
            except Exception:  # noqa: BLE001
                note = "Discarded (couldn't remove the WP draft — check manually)"
    store.update_task(task["id"], status="rejected")
    store.reset_streak(skill["id"])   # a rejection breaks the clean-approval streak
    store.log_decision(task["id"], skill["id"], "owner", "reject", snapshot={"draft": task.get("draft")})
    if task.get("tg_message_id"):
        tg.edit(task["tg_message_id"], f"✗ {note} — '{skill['name']}'.")


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
        db.setting_set(f"rule:{task['id']}", rule["rule"])
        company = store.get_company(task["company_id"])
        co = company["name"] if company else "this company"
        tg.send(f"I'm reading your correction as a standing rule:\n\n“{rule['rule']}”\n\n"
                f"Where should it live? '{co}' only, or ALL companies?",
                [[tg.button(f"{co} only", f"ry:{task['id']}"), tg.button("All companies", f"ru:{task['id']}")],
                 [tg.button("No, just this once", f"rn:{task['id']}")]])


def pending_rule(task_id: int) -> dict:
    """Cockpit polls this after a correction: returns the rule the background inference proposed (if any)."""
    task = store.get_task(task_id)
    skill = store.get_skill(task["skill_id"]) if task else None
    company = store.get_company(task["company_id"]) if task else None
    return {"ok": True, "proposed_rule": db.setting_get(f"rule:{task_id}"),
            "skill_name": skill["name"] if skill else None,
            "company": company["name"] if company else None}


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
        art = worker.draft_article(skill, company, task["request"], correction=text)
        info = db.setting_get(f"wp:{task['id']}") or {}
        pid = info.get("post_id")
        if pid:
            r = site.update(pid, art["title"], art["html"])  # stays a draft
            preview = r.get("preview") or info.get("preview")
        else:
            post = site.stage_draft(art["title"], art["html"])
            pid, preview = post["id"], post.get("preview")
        db.setting_set(f"wp:{task['id']}", {"post_id": pid, "preview": preview,
                                            "edit": info.get("edit"), "title": art["title"]})
        task = store.update_task(task["id"], draft=art["html"], status="awaiting_approval",
                                 attempts=task["attempts"] + 1)
        store.log_decision(task["id"], skill["id"], "owner", "correct", note=text)
        msg2 = tg.send(_fmt_blog(company, skill, art, None, preview), _blog_buttons(task["id"]))
        store.update_task(task["id"], tg_message_id=msg2["message_id"])
        _maybe_propose_rule(task, skill, text, old or "", art["html"])
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

    new = worker.draft(skill, company, task["request"], correction=text)
    task = store.update_task(task["id"], draft=new, status="awaiting_approval", attempts=task["attempts"] + 1)
    store.log_decision(task["id"], skill["id"], "owner", "correct", note=text, snapshot={"old": old, "new": new})
    msg2 = tg.send(_fmt(task, skill, company, None), _approval_buttons(task["id"]))
    store.update_task(task["id"], tg_message_id=msg2["message_id"])
    _maybe_propose_rule(task, skill, text, old or "", new or "")


def _confirm_rule(task: dict, skill: dict, yes: bool, universal: bool = False) -> None:
    rule = db.setting_get(f"rule:{task['id']}")
    if yes and rule:
        if universal:
            store.add_universal_rule(skill["skill_key"], rule)
            where = "ALL companies (universal)"
        else:
            store.add_rule(skill["id"], rule)
            where = f"'{skill['name']}'"
        store.log_decision(task["id"], skill["id"], "owner", "rule_confirmed",
                           note=("[universal] " if universal else "") + rule)
        tg.send(f"Added to {where}: “{rule}”. I'll follow it from now on.")
    else:
        tg.send("Okay — not adding a rule.")
    db.setting_set(f"rule:{task['id']}", None)


# ---------- programmatic actions (cockpit API surface) ----------

def _load(task_id: int):
    task = store.get_task(task_id)
    if not task:
        return None, None, None
    return task, store.get_skill(task["skill_id"]), store.get_company(task["company_id"])


def approve_task(task_id: int, stepup_token: str | None = None) -> dict:
    task, skill, company = _load(task_id)
    if not task:
        return {"ok": False, "error": "no such task"}
    if task["status"] not in ("awaiting_approval", "awaiting_correction"):
        return {"ok": False, "error": f"task is '{task['status']}', not awaiting approval"}
    # SAFEGUARD: a newsletter that touches the real list NEVER goes on a plain approve. Stage 2
    # (newsletter_review) SCHEDULES for the 1st; Stage 3 (newsletter_send) SENDS. Both require the
    # operator to echo the exact recipient count first.
    if task["kind"] in ("newsletter_review", "newsletter_send"):
        n = len(newsletter.recipients(task["company_id"]))
        action = "schedule" if task["kind"] == "newsletter_review" else "send"
        info = {"ok": False, "needs_confirm": True, "recipients": n, "company": company["name"], "action": action}
        if action == "schedule":
            info["date"] = _next_newsletter_slot(company["id"]).strftime("%-d %b %Y")
        return info
    gate = _biometric_gate(task["kind"] in _APPROVE_PUBLIC, stepup_token)
    if gate:
        return gate
    result = _approve(task, skill, company)
    return {"ok": True, "task": store.get_task(task_id), "result": result}


def confirm_send_task(task_id: int, count: int, stepup_token: str | None = None) -> dict:
    """Confirm a newsletter with the EXACT recipient count. Stage 2 (newsletter_review) -> SCHEDULE for the
    next free 1st; Stage 3 (newsletter_send) -> SEND now (drip). Count must match, so a misclick can't fire."""
    task, skill, company = _load(task_id)
    if not task or task["kind"] not in ("newsletter_review", "newsletter_send"):
        return {"ok": False, "error": "not a newsletter card"}
    n = len(newsletter.recipients(task["company_id"]))
    try:
        if int(count) != n:
            return {"ok": False, "error": f"Count mismatch: you entered {count}, the list is {n}. Nothing done."}
    except (TypeError, ValueError):
        return {"ok": False, "error": "Enter the exact recipient count to confirm."}
    gate = _biometric_gate(True, stepup_token)   # a newsletter schedule/send is always a public action
    if gate:
        return gate
    art = db.setting_get(f"newsletter:{task_id}")
    if not art:
        return {"ok": False, "error": "no built newsletter found for this card"}
    if task["kind"] == "newsletter_review":
        return _schedule_newsletter(task, skill, company, art, n)
    return _dispatch_newsletter(task, skill, company, art, n)


def _next_newsletter_slot(company_id: int, hour: int = 9) -> datetime:
    """Next free 1st-of-month for a company's scheduled newsletters, on the UNIFIED tasks table: if one is
    already scheduled, take the 1st of the month after its latest; else the next upcoming 1st (so issues stack
    one per month)."""
    now = datetime.now(schedule._GST)
    row = db.one("select run_at from tasks where company_id=%s and kind='newsletter_scheduled' "
                 "and schedule_kind='once' and status='scheduled' and run_at is not null "
                 "order by run_at desc limit 1", (company_id,))
    if row and row["run_at"] and row["run_at"] > now:
        return schedule.first_of_next_month(row["run_at"], hour)
    first_this = now.replace(day=1, hour=hour, minute=0, second=0, microsecond=0)
    return first_this if first_this > now else schedule.first_of_next_month(now, hour)


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
    rule = db.setting_get(f"rule:{task_id}")
    task = store.get_task(task_id)
    added = False
    if add and rule and task:
        if scope == "universal":
            skill = store.get_skill(task["skill_id"])
            if skill:
                store.add_universal_rule(skill["skill_key"], rule)
                added = True
        else:
            store.add_rule(task["skill_id"], rule)
            added = True
        if added:
            store.log_decision(task_id, task["skill_id"], "owner", "rule_confirmed",
                               note=("[universal] " if scope == "universal" else "") + rule)
    db.setting_set(f"rule:{task_id}", None)
    return {"ok": True, "added": added, "scope": scope}


# ---------- auto-intake: pull new enquiries from Gmail, draft a reply for each ----------

def poll_inquiries() -> dict:
    """The automatic intake (recent window), called on the engine loop."""
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
    seen = set(db.setting_get("gmail_processed") or [])
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
        store.create_task(co["id"], skill["id"], "email_reply",
                          {"brief": _email_brief(inq), "inquiry": inq, "triage": verdict})
        made += 1
        if made >= 10:
            break
    db.setting_set("gmail_processed", list(seen)[-1000:])
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
FORM_INTAKE = {
    "snaprewards": {"rt_key": "gmail_refresh_token:snaprewards", "client": "snaprewards",
                    "subject": "Site contact form", "skill": "sales-first-response"},
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


def _poll_one_form(slug: str, cfg: dict, days: int = 3) -> dict:
    co = store.get_company_by_slug(slug)
    skill = store.get_skill_by_key(co["id"], cfg.get("skill", "sales-first-response")) if co else None
    if not (co and skill):
        return {"reason": "company/skill missing"}
    key = f"form_processed:{slug}"
    seen = set(db.setting_get(key) or [])
    emails = gmail.list_recent(days=days, limit=30, rt_key=cfg["rt_key"], company=cfg.get("client"),
                               q=f'subject:"{cfg["subject"]}"', skip=seen)
    made = filtered = 0
    for e in emails:
        gid = e.get("gmail_id")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        inq = _parse_form_email(e)
        if not inq.get("email"):     # couldn't parse a lead email -> skip
            continue
        if not triage_inquiry(inq, slug)["genuine"]:   # spam (e.g. SEO pitch) -> filed, no CRM, no draft
            filtered += 1
            continue
        try:
            crm.add_inquiry(inq, slug)
        except Exception:  # noqa: BLE001
            pass
        store.create_task(co["id"], skill["id"], "email_reply", {"brief": _email_brief(inq), "inquiry": inq})
        made += 1
        if made >= 10:
            break
    db.setting_set(key, list(seen)[-1000:])
    if made:
        tg.send(f"{made} genuine {co['name']} contact-form enquir{'ies' if made > 1 else 'y'}"
                + (f" ({filtered} spam filtered)" if filtered else "") + " — drafting for your approval.")
    return {"made": made, "filtered": filtered}


def poll_company_forms() -> dict:
    """Read every configured + connected company's contact-form emails; route genuine leads to a drafted
    reply in the Inbox, filter spam. Runs on the 60s loop alongside the catch-all classifier."""
    out = {}
    for slug, cfg in FORM_INTAKE.items():
        if not db.setting_get(cfg["rt_key"]):
            continue
        try:
            out[slug] = _poll_one_form(slug, cfg)
        except Exception as ex:  # noqa: BLE001
            tg.send(f"(form intake hiccup [{slug}]: {ex})")
    return out


# ---------- inbox classifier (the sales-triage universal skill, on Haiku) ----------

INBOX_CATEGORIES = ["lead", "partner", "support", "freelancer", "vendor", "recruitment",
                    "marketing", "spam", "personal", "automated"]
_INBOX_CRM = {"lead", "partner", "support", "freelancer", "vendor", "recruitment"}   # these become CRM contacts
# every inbound contact we add is newsletter-eligible (Rashad 2026-06-18: they contacted us, it's a general
# newsletter, and unsubscribe + complaint->opt-out keep it self-correcting). Own knob in case we re-scope.
_INBOX_NEWSLETTER = set(_INBOX_CRM)
# each company's main catch-all inbox -> used to derive its OWN domain (never CRM our own / internal senders)
INBOXES = {"tabscanner": "api@tabscanner.com", "sensa": "hello@sensa.digital",
           "snaprewards": "loyalty@snap-rewards.com", "filmspoke": "create@filmspoke.ai",
           "skyvision": "fly@skyvision.film"}


def _is_internal(addr: str, own_domain: str) -> bool:
    d = (addr or "").split("@")[-1].lower().strip()
    return bool(own_domain) and (d == own_domain or d.endswith("." + own_domain))


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
         "seeking a JOB with us (a CV, 'are you hiring?', wants to join the team). "
         'Return JSON {"category":"<one>","to_crm":boolean,"reason":"<short phrase>"}. to_crm is true for '
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
    return {"category": cat, "to_crm": cat in _INBOX_CRM, "reason": (out.get("reason") or "").strip()}


def poll_inbox(company_slug: str = "tabscanner", rt_key: str = "gmail_refresh_token",
               days: int = 2, limit: int = 40, commit: bool = True, company: str | None = None) -> dict:
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
    key = f"inbox_processed:{company_slug}"
    seen = set(db.setting_get(key) or [])
    emails = gmail.list_recent(days=days, limit=limit, rt_key=rt_key, q=q, skip=seen, company=company)
    results, added = [], 0
    for e in emails:
        gid = e.get("gmail_id")
        if not gid or gid in seen:
            continue
        if _is_internal(e.get("email"), own_domain):   # our own / internal address: never classify or CRM
            if commit:
                seen.add(gid)
            results.append({"from": e.get("email"), "subject": (e.get("subject") or "")[:60],
                            "category": "internal", "to_crm": False, "reason": "own/internal address"})
            continue
        cls = classify_email(co, e)
        if commit:
            if cls["to_crm"] and e.get("email"):
                stage = "Engaged" if cls["category"] in ("lead", "partner", "support") else "Cold"
                try:
                    st, _ = crm.add_inbound_contact({"email": e["email"], "name": e["name"]},
                                                    company_slug, cls["category"], stage=stage,
                                                    newsletter=cls["category"] in _INBOX_NEWSLETTER)
                    if st == "added":
                        added += 1
                except Exception:  # noqa: BLE001
                    pass
            seen.add(gid)
        results.append({"from": e.get("email"), "subject": (e.get("subject") or "")[:60], **cls})
    if commit:
        db.setting_set(key, list(seen)[-3000:])
    return {"processed": len(results), "added_to_crm": added, "results": results}


# ---------- inbox registry: data-driven, so a NEW address auto-joins the classifier loop ----------
# The 60s loop polls EVERY connected inbox in this registry. Adding an inbox is data, not code:
# the OAuth onboarding flow calls register_inbox() + stores that inbox's read token, and the loop
# picks it up on the next cycle — no edit here. (`_inbox_connected` is the access seam the OAuth
# plan fills in: today = a stored Gmail refresh token; could become domain-wide delegation.)

def _default_rt_key(slug: str) -> str:
    # Tabscanner keeps the legacy key; every other inbox gets its own namespaced token key.
    return "gmail_refresh_token" if slug == "tabscanner" else f"gmail_refresh_token:{slug}"


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
            poll_inbox(e["slug"], e.get("rt_key") or _default_rt_key(e["slug"]))
            polled.append(e["slug"])
        except Exception as ex:  # noqa: BLE001
            tg.send(f"(inbox classify hiccup [{e.get('slug')}]: {ex})")
    return {"polled": polled}


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


def _run_report_task(task: dict) -> None:
    """A scheduled report INSTANCE (a 'seo_report' child spawned by the unified clock): generate the report
    and turn THIS task into the finished report card in place — one row per occurrence (own approval/history)."""
    req = task.get("request") or {}
    company = req.get("company") or (store.get_company(task["company_id"]) or {}).get("slug")
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
    for t in db.query("select id from tasks where schedule_kind='once' and status='scheduled' "
                      "and coalesce(enabled,true)=true and run_at is not null and run_at <= now()"):
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


def run(poll_idle: float = 1.0) -> None:
    tg.send("\U0001F9E0 Cortex engine online.")
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
                reminders.fire_due()    # fire due reminders -> nudge notification or spawn an action task
            except Exception as e:  # noqa: BLE001
                tg.send(f"(reminder fire hiccup: {e})")
            try:
                promote_due_tasks()   # the one unified clock — recurring templates + one-off scheduled tasks
            except Exception as e:  # noqa: BLE001
                tg.send(f"(promote hiccup: {e})")
            try:
                drain_newsletter_sends()
            except Exception as e:  # noqa: BLE001
                tg.send(f"(newsletter drip hiccup: {e})")
        time.sleep(poll_idle)
