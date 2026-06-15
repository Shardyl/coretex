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

from . import db, manager, store, worker
from .integrations import telegram as tg, wordpress as wp

MONEY_KINDS = {"payment", "invoice_send"}  # never auto, regardless of trust

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
        msg = tg.send(_fmt(task, skill, company, verdict), _approval_buttons(task["id"]))
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
        else:
            tg.edit(task["tg_message_id"], f"✅ Approved — '{skill['name']}' (streak {skill['trust_streak']}). Done.")
    # Offer auto only for non-blog skills (blog publishing must never go auto).
    if task["kind"] != "blog" and skill["authority"] == "ask" and skill["trust_streak"] >= skill["auto_threshold"]:
        tg.send(f"'{skill['name']}' has {skill['trust_streak']} clean approvals. "
                f"Put it on auto for low-stakes work?",
                [[tg.button("Yes, set auto", f"au:{skill['id']}")]])


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


def run(poll_idle: float = 1.0) -> None:
    tg.send("\U0001F9E0 Cortex engine online.")
    while True:
        process_new_tasks()
        handle_updates()
        time.sleep(poll_idle)
