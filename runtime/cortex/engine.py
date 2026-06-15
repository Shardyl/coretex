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

import re
import secrets
import time

from . import crm, db, gmail, leadqualify, manager, profile, provider, store, worker
from .integrations import telegram as tg, wordpress as wp

MONEY_KINDS = {"payment", "invoice_send"}  # never auto, regardless of trust
EMAIL_KINDS = {"email_reply"}              # the reply is sent via Gmail on approval

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

def _email_brief(inq: dict, qual: dict | None = None) -> str:
    """Frame a website enquiry as a reply-drafting brief for the worker."""
    brief = ("Draft a reply to this website enquiry. Write ONLY the email body — no subject line, no "
             "'From'/'To' headers, and no sign-off signature (the signature is added automatically). "
             "Reply directly to the person, in the company voice, following the standing rules.\n\n"
             f"Their name: {inq.get('name') or 'there'}\n"
             f"Their email: {inq.get('email') or '(unknown)'}\n"
             f"Their message:\n{(inq.get('message') or inq.get('snippet') or '').strip()}")
    if qual:
        brief += (f"\n\nQualification: this lead is {qual.get('tier_label')}. "
                  f"What Cortex found: {qual.get('research') or '(no research)'} "
                  f"Reason: {qual.get('reason') or ''}\n"
                  "Draft a first reply appropriate to that tier (handle each tier however the owner has taught).")
    return brief


def _email_envelope(task: dict, company: dict) -> dict:
    """Who the approved reply goes to / from / cc — resolved from the inquiry + the company profile."""
    inq = (task.get("request") or {}).get("inquiry") or {}
    try:
        data = profile.get(company["id"]) or {}
    except Exception:  # noqa: BLE001
        data = {}
    cc = (data.get("default_cc") or "").strip()
    return {"to": inq.get("email") or "", "from": (data.get("reply_from") or "").strip() or None,
            "cc": cc if "@" in cc else None, "subject": "Re: " + (inq.get("subject") or "your enquiry"),
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


def _send_email_reply(task: dict, skill: dict, company: dict, actor: str, auto: bool) -> dict:
    env = _email_envelope(task, company)
    body = (task.get("draft") or "").rstrip()
    sig = env["signature"]
    if sig and sig.splitlines()[0].lower() not in body.lower():
        body = body + "\n\n" + sig
    res = gmail.send_message(env["to"], env["subject"], body, from_addr=env["from"], cc=env["cc"])
    store.update_task(task["id"], status="done")
    store.log_decision(task["id"], skill["id"], actor, "send",
                       snapshot={"to": env["to"], "cc": env["cc"], "from": env["from"],
                                 "subject": env["subject"], "gmail_id": res.get("id")})
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


def _run_task(task: dict) -> None:
    skill = store.get_skill(task["skill_id"])
    company = store.get_company(task["company_id"])
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
               and verdict.get("aligned") and not verdict.get("escalate"))
    if auto_ok:
        _execute(task, skill, company, actor="cortex", auto=True)
    else:
        preview = _fmt_email(task, skill, company, verdict) if task["kind"] in EMAIL_KINDS \
            else _fmt(task, skill, company, verdict)
        msg = tg.send(preview, _approval_buttons(task["id"]))
        store.update_task(task["id"], status="awaiting_approval", tg_message_id=msg["message_id"])


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


def _execute(task: dict, skill: dict, company: dict, actor: str, auto: bool = False) -> dict:
    if task["kind"] in EMAIL_KINDS:
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
    elif action in ("ry", "rn"):
        _confirm_rule(task, skill, yes=(action == "ry"))


def _approve(task: dict, skill: dict, company: dict) -> None:
    result = _execute(task, skill, company, actor="owner")
    skill = store.bump_streak(skill["id"])
    if task.get("tg_message_id"):
        if result and result.get("link"):
            tg.edit(task["tg_message_id"],
                    f"✅ Published live on Tabscanner: {result['link']}  (streak {skill['trust_streak']}).")
        elif result and result.get("sent_to"):
            tg.edit(task["tg_message_id"],
                    f"✅ Sent to {result['sent_to']} — '{skill['name']}' (streak {skill['trust_streak']}). Done.")
        else:
            tg.edit(task["tg_message_id"], f"✅ Approved — '{skill['name']}' (streak {skill['trust_streak']}). Done.")
    # Offer auto only for non-blog skills (blog publishing must never go auto).
    if task["kind"] != "blog" and skill["authority"] == "ask" and skill["trust_streak"] >= skill["auto_threshold"]:
        higher = skill["trust_streak"] + 20
        tg.send(f"'{skill['name']}' has {skill['trust_streak']} clean approvals. "
                f"Put it on auto for low-stakes work, or raise the bar for extra confidence?",
                [[tg.button("Yes, set auto", f"au:{skill['id']}"),
                  tg.button(f"No — raise to {higher}", f"th:{skill['id']}:{higher}")]])


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
    rule = worker.infer_rule(skill, text, old or "", new or "")
    if rule.get("is_rule") and rule.get("rule"):
        db.setting_set(f"rule:{task['id']}", rule["rule"])
        tg.send(f"I'm reading your correction as a standing rule:\n\n“{rule['rule']}”\n\n"
                f"Add it to '{skill['name']}'?",
                [[tg.button("Yes, add rule", f"ry:{task['id']}"), tg.button("No", f"rn:{task['id']}")]])


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

    new = worker.draft(skill, company, task["request"], correction=text)
    task = store.update_task(task["id"], draft=new, status="awaiting_approval", attempts=task["attempts"] + 1)
    store.log_decision(task["id"], skill["id"], "owner", "correct", note=text, snapshot={"old": old, "new": new})
    msg2 = tg.send(_fmt(task, skill, company, None), _approval_buttons(task["id"]))
    store.update_task(task["id"], tg_message_id=msg2["message_id"])
    _maybe_propose_rule(task, skill, text, old or "", new or "")


def _confirm_rule(task: dict, skill: dict, yes: bool) -> None:
    rule = db.setting_get(f"rule:{task['id']}")
    if yes and rule:
        store.add_rule(skill["id"], rule)
        store.log_decision(task["id"], skill["id"], "owner", "rule_confirmed", note=rule)
        tg.send(f"Added to '{skill['name']}': “{rule}”. I'll follow it from now on.")
    else:
        tg.send("Okay — not adding a rule.")
    db.setting_set(f"rule:{task['id']}", None)


# ---------- programmatic actions (cockpit API surface) ----------

def _load(task_id: int):
    task = store.get_task(task_id)
    if not task:
        return None, None, None
    return task, store.get_skill(task["skill_id"]), store.get_company(task["company_id"])


def approve_task(task_id: int) -> dict:
    task, skill, company = _load(task_id)
    if not task:
        return {"ok": False, "error": "no such task"}
    if task["status"] not in ("awaiting_approval", "awaiting_correction"):
        return {"ok": False, "error": f"task is '{task['status']}', not awaiting approval"}
    _approve(task, skill, company)
    return {"ok": True, "task": store.get_task(task_id)}


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
    return {"ok": True, "task": store.get_task(task_id)}


# ---------- auto-intake: pull new enquiries from Gmail, draft a reply for each ----------

def poll_inquiries() -> dict:
    """The automatic intake (recent window), called on the engine loop."""
    return poll_inquiries_window(days=2)


def triage_inquiry(inq: dict) -> dict:
    """Decide if an enquiry is a genuine potential customer/partner worth a reply, or junk (spam, bots,
    gibberish, off-topic). The form gets a lot of spam — only genuine enquiries get a draft + a CRM contact."""
    try:
        out = provider.think_json(
            "You triage inbound website enquiries for Tabscanner, a receipt-OCR / data-extraction API for "
            "developers and businesses (expense, loyalty, fintech, market research). Decide if an enquiry is "
            "a GENUINE potential customer, partner, or support contact worth a human reply, or JUNK. Be "
            "strict: random/gibberish sender addresses, mismatched names, off-topic messages (e.g. sports, "
            "unrelated products), SEO/marketing/link-building solicitations, and obvious bot spam are JUNK.",
            f"From: {inq.get('name')} <{inq.get('email')}>\nSubject: {inq.get('subject')}\n"
            f"Message:\n{(inq.get('message') or inq.get('snippet') or '').strip()}\n\n"
            'Return JSON: {"genuine": boolean, "category": "lead|partner|support|spam|offtopic|unclear", '
            '"reason": "short phrase"}',
            model=provider.MODEL_ROUTER)
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
        qual = leadqualify.qualify(inq, co)              # research the lead, then classify the tier
        if not qual["genuine"]:                          # spam/junk: filed, NOT drafted, NOT added to CRM
            filtered_log.append({"name": inq.get("name"), "email": inq.get("email"),
                                 "category": "spam", "reason": qual["reason"]})
            filtered += 1
            continue
        try:
            crm.add_inquiry(inq, "tabscanner")           # genuine -> verified contact in the CRM
        except Exception:  # noqa: BLE001
            pass
        store.create_task(co["id"], skill["id"], "email_reply",
                          {"brief": _email_brief(inq, qual), "inquiry": inq, "qualification": qual})
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


def run(poll_idle: float = 1.0) -> None:
    tg.send("\U0001F9E0 Cortex engine online.")
    last_poll = 0.0
    while True:
        process_new_tasks()
        handle_updates()
        now = time.time()
        if now - last_poll >= 60:        # check Gmail for new enquiries about once a minute
            last_poll = now
            try:
                poll_inquiries()
            except Exception as e:  # noqa: BLE001
                tg.send(f"(enquiry poll hiccup: {e})")
        time.sleep(poll_idle)
