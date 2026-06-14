"""The engine — ties it together.

Loop:  process new tasks (worker -> manager -> approval/auto)  +  handle Telegram
taps and corrections (approve / correct->redraft->learn-rule / skip), updating the
trust streak and offering auto at the threshold.
"""
from __future__ import annotations

import time

from . import db, manager, store, worker
from .integrations import telegram as tg

MONEY_KINDS = {"payment", "invoice_send"}  # never auto, regardless of trust


def _fmt(task: dict, skill: dict, company: dict, verdict: dict | None) -> str:
    head = f"[{company['name']} · {skill['name']}]  ·  needs your yes"
    draft = task.get("draft") or ""
    if len(draft) > 3500:
        draft = draft[:3500] + "\n…(truncated for preview)"
    extra = ""
    if verdict and verdict.get("issues"):
        extra = "\n\n⚠ Manager: " + "; ".join(verdict["issues"])
    return f"{head}\n\n{draft}{extra}"


def _approval_buttons(task_id: int) -> list[list[dict]]:
    return [[tg.button("✅ Approve", f"ap:{task_id}"),
             tg.button("✎ Correct", f"co:{task_id}"),
             tg.button("✗ Skip", f"sk:{task_id}")]]


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

    draft = worker.draft(skill, company, task["request"])
    verdict = manager.check(skill, company, draft, task["request"])
    if not verdict["aligned"] and verdict["issues"]:
        draft = worker.draft(skill, company, task["request"], manager_feedback=verdict["issues"])
        verdict = manager.check(skill, company, draft, task["request"])

    task = store.update_task(task["id"], draft=draft, manager=verdict, attempts=task["attempts"] + 1)

    auto_ok = (skill["authority"] == "auto" and skill["stakes"] == "low"
               and not skill["paused"] and task["kind"] not in MONEY_KINDS)
    if auto_ok:
        _execute(task, skill, company, actor="cortex", auto=True)
    else:
        msg = tg.send(_fmt(task, skill, company, verdict), _approval_buttons(task["id"]))
        store.update_task(task["id"], status="awaiting_approval", tg_message_id=msg["message_id"])


def _execute(task: dict, skill: dict, company: dict, actor: str, auto: bool = False) -> None:
    # Phase 1: 'execute' = mark done + log. Phase 2 swaps this for the real WordPress publish.
    store.update_task(task["id"], status="done")
    store.log_decision(task["id"], skill["id"], actor, "auto" if auto else "approve",
                       snapshot={"draft": task.get("draft")})
    if auto:
        tg.send(f"[{company['name']} · {skill['name']}] auto-ran (trusted). #{task['id']} done.")


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
        _skip(task, skill)
    elif action == "co":
        store.update_task(task["id"], status="awaiting_correction")
        if task.get("tg_message_id"):
            tg.edit(task["tg_message_id"], f"✎ Correcting '{skill['name']}'. Send me your correction as a message.")
    elif action in ("ry", "rn"):
        _confirm_rule(task, skill, yes=(action == "ry"))


def _approve(task: dict, skill: dict, company: dict) -> None:
    _execute(task, skill, company, actor="owner")
    skill = store.bump_streak(skill["id"])
    if task.get("tg_message_id"):
        tg.edit(task["tg_message_id"], f"✅ Approved — '{skill['name']}' (streak {skill['trust_streak']}). Done.")
    if skill["authority"] == "ask" and skill["trust_streak"] >= skill["auto_threshold"]:
        tg.send(f"'{skill['name']}' has {skill['trust_streak']} clean approvals. "
                f"Put it on auto for low-stakes work?",
                [[tg.button("Yes, set auto", f"au:{skill['id']}")]])


def _skip(task: dict, skill: dict) -> None:
    store.update_task(task["id"], status="rejected")
    store.log_decision(task["id"], skill["id"], "owner", "reject", snapshot={"draft": task.get("draft")})
    if task.get("tg_message_id"):
        tg.edit(task["tg_message_id"], f"✗ Skipped — '{skill['name']}'.")


def _on_message(msg: dict) -> None:
    text = msg["text"].strip()
    if text.startswith("/"):
        return
    pending = db.query("select * from tasks where status='awaiting_correction' order by updated_at desc limit 1")
    if not pending:
        return
    task = pending[0]
    skill = store.get_skill(task["skill_id"])
    company = store.get_company(task["company_id"])
    old = task.get("draft")
    new = worker.draft(skill, company, task["request"], correction=text)
    task = store.update_task(task["id"], draft=new, status="awaiting_approval", attempts=task["attempts"] + 1)
    store.log_decision(task["id"], skill["id"], "owner", "correct", note=text, snapshot={"old": old, "new": new})
    msg2 = tg.send(_fmt(task, skill, company, None), _approval_buttons(task["id"]))
    store.update_task(task["id"], tg_message_id=msg2["message_id"])
    # learn the rule out loud
    rule = worker.infer_rule(skill, text, old or "", new or "")
    if rule.get("is_rule") and rule.get("rule"):
        db.setting_set(f"rule:{task['id']}", rule["rule"])
        tg.send(f"I'm reading your correction as a standing rule:\n\n“{rule['rule']}”\n\n"
                f"Add it to '{skill['name']}'?",
                [[tg.button("Yes, add rule", f"ry:{task['id']}"), tg.button("No", f"rn:{task['id']}")]])


def _confirm_rule(task: dict, skill: dict, yes: bool) -> None:
    rule = db.setting_get(f"rule:{task['id']}")
    if yes and rule:
        store.add_rule(skill["id"], rule)
        store.log_decision(task["id"], skill["id"], "owner", "rule_confirmed", note=rule)
        tg.send(f"Added to '{skill['name']}': “{rule}”. I'll follow it from now on.")
    else:
        tg.send("Okay — not adding a rule.")
    db.setting_set(f"rule:{task['id']}", None)


def run(poll_idle: float = 1.0) -> None:
    tg.send("\U0001F9E0 Cortex engine online.")
    while True:
        process_new_tasks()
        handle_updates()
        time.sleep(poll_idle)
