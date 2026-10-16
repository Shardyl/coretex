"""Pipeline review: every open opportunity, what Cortex will do next and when, correctable by a note (owner, 16 Oct 2026).

"I'd like to be able to do a manual audit of what's in the pipeline ... what's scheduled to come up as reminders, what
are the next action steps ... very brief ... run through the opportunities and think, okay, that's correct, that's not
correct. And I should be able to provide additional notes if it's not right, save the notes, and it updates those
action steps according to my notes."

`summary(deal)` is code only: the email chase clock, every pending reminder on the deal (his and Cortex's), every open
or scheduled card, and the last contact each way. `apply_note` files his note on the deal (the normal note path: the
chase and touch timing, the waiting email or quotation it redirects) and then applies what it says about the LISTED
steps: one Haiku reading returns verdicts by item id, code resolves every date and makes every change, and each change
is logged on the deal. Nothing is ever sent from here."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import db

_GST = timezone(timedelta(hours=4))
_OPEN = ("queued", "new", "drafting", "awaiting_approval", "awaiting_correction")
_THEM = ("email_in", "whatsapp_in")
_US = ("email_out", "email_out_manual", "touch_made", "whatsapp_out")
_REVIEW_STAGES = ("Opportunity", "Quote")


def _d(x) -> str:
    if not x:
        return ""
    if isinstance(x, str):
        try:
            x = datetime.fromisoformat(x.replace("Z", "+00:00"))
        except ValueError:
            return x[:10]
    if x.tzinfo is None:
        x = x.replace(tzinfo=timezone.utc)
    return x.astimezone(_GST).strftime("%a %d %b")


def _ts(e: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(e.get("ts") or "").replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _rem_kind(r: dict) -> str:
    by, t = str(r.get("created_by") or ""), str(r.get("title") or "")
    if by == "cortex-touch":
        return "WhatsApp or call"
    if t.startswith(("Commitment owed", "OWED")) or by == "cortex-commitment":
        return "Owed"
    if t.startswith("Client deadline") or "deadline" in t.lower()[:40]:
        return "Their deadline"
    if t.startswith("Tender closes"):
        return "Tender closes"
    if not by.startswith("cortex"):
        return "Your reminder"
    return "Reminder"


def _card_line(t: dict) -> str:
    r = t.get("request") or {}
    kind = r.get("kind") or t.get("kind")
    who = (r.get("inquiry") or {}).get("name") or (r.get("inquiry") or {}).get("email") or ""
    if r.get("followup"):
        what = f"Chase email to {who}" if who else "Chase email"
    elif t.get("kind") in ("email_reply", "email_draft"):
        subj = (r.get("inquiry") or {}).get("subject") or t.get("title") or ""
        what = f"Email to {who}: {subj}" if who else f"Email: {subj}"
    elif t.get("kind") == "quotation":
        what = f"Quotation {r.get('number') or ''}".strip()
    elif kind in ("proposal", "creative_proposal"):
        what = "Proposal deck"
    elif r.get("prep_action"):
        what = f"Prepare {r['prep_action']}"
    else:
        what = t.get("title") or kind or "Card"
    state = {"scheduled": "scheduled", "awaiting_approval": "waiting for you", "awaiting_correction": "needs your answer",
             "queued": "queued", "new": "drafting", "drafting": "drafting"}.get(t.get("status"), t.get("status"))
    return f"{what[:110]} ({state})"


def summary(deal_id: int) -> dict | None:
    p = db.one("select id, company, title, stage, value, currency, contact_email, automation, next_followup, cadence, "
               "followup_step, history, contacts, owner from crm_projects where id=%s", (int(deal_id),))
    if not p:
        return None
    hist = [e for e in (p.get("history") or []) if not e.get("voided")]
    last_them = next((e for e in reversed(hist) if e.get("event") in _THEM), None)
    last_us = next((e for e in reversed(hist) if e.get("event") in _US), None)
    now = datetime.now(timezone.utc)
    steps = []
    # the email chase clock
    stopped = next((e for e in reversed(hist) if e.get("event") in ("chase_stopped", "chase_paused")), None)
    cad = (p.get("cadence") or {}).get("name") or "standard"
    if p.get("automation") == "auto" and p.get("next_followup"):
        steps.append({"id": "CHASE", "type": "chase", "when": p["next_followup"].isoformat(),
                      "text": f"Email chase ({cad} rhythm, step {int(p.get('followup_step') or 0) + 1})"})
    else:
        why = ("stopped: " + str(stopped.get("text") or "")[:90]) if stopped else (
            "starts when we next email them" if p.get("automation") == "auto" else f"automation {p.get('automation') or 'off'}")
        steps.append({"id": "CHASE", "type": "chase", "when": None, "text": f"No email chase scheduled ({why})"})
    for r in db.query("select id, title, due_at, status, created_by, deliver_by, snooze_until from reminders where "
                      "target_type='deal' and target_id=%s and status in ('pending','snoozed') order by due_at",
                      (str(int(deal_id)),)):
        when = r.get("snooze_until") if r.get("status") == "snoozed" and r.get("snooze_until") else r["due_at"]
        extra = f" (deliver by {_d(r['deliver_by'])})" if r.get("deliver_by") and "deliver by" not in r["title"] else ""
        steps.append({"id": f"R{r['id']}", "type": "reminder", "when": when.isoformat(), "kind": _rem_kind(r),
                      "text": f"{_rem_kind(r)}: {re.sub(r'\\s+', ' ', r['title'])[:140]}{extra}",
                      "mine": not str(r.get("created_by") or "").startswith("cortex")})
    for t in db.query("select id, kind, status, title, request, run_at, created_at from tasks where deal_id=%s and "
                      "(status = any(%s) or (status='scheduled' and run_at > now())) order by id", (int(deal_id), list(_OPEN))):
        steps.append({"id": f"C{t['id']}", "type": "card", "card": t["id"],
                      "when": (t["run_at"] if t.get("status") == "scheduled" and t.get("run_at") else None)
                      and t["run_at"].isoformat(), "text": _card_line(t)})
    steps.sort(key=lambda s: (s["when"] is None and s["type"] != "card", s["when"] or now.isoformat()))
    contact = next((c.get("name") or c.get("email") for c in (p.get("contacts") or [])
                    if (c.get("email") or "").lower() == (p.get("contact_email") or "").lower()), None) \
        or p.get("contact_email") or ""
    note = next((e for e in reversed(hist) if e.get("event") == "context"), None)
    return {"id": p["id"], "title": p["title"], "company": p["company"], "stage": p["stage"],
            "value": float(p["value"]) if p.get("value") is not None else None, "currency": p.get("currency") or "AED",
            "contact": contact, "owner": p.get("owner"),
            "last_them": last_them and {"when": _d(last_them.get("ts")), "text": str(last_them.get("text") or "")[:140]},
            "last_us": last_us and {"when": _d(last_us.get("ts")), "text": str(last_us.get("text") or "")[:140]},
            "last_note": note and {"when": _d(note.get("ts")), "text": str(note.get("text") or "")[:200]},
            "steps": [{**s, "date": _d(s["when"])} for s in steps]}


def rows(company_labels: list[str] | None = None, stages: tuple = _REVIEW_STAGES) -> list[dict]:
    q = "select id from crm_projects where stage = any(%s)"
    params: list = [list(stages)]
    if company_labels:
        q += " and lower(company) = any(%s)"
        params.append([c.lower() for c in company_labels])
    out = [summary(r["id"]) for r in db.query(q + " order by company, value desc nulls last, id", tuple(params))]
    return [s for s in out if s]


_NOTE_SYS = """You read the owner's note on ONE sales opportunity and decide what it says about the LISTED next steps.
Each step has an id (CHASE, R<n> reminder, C<n> card). Return JSON only:
{"items": [{"id": "R12", "verdict": "cancel" | "move" | "keep", "when": "<his words for the new time, only for move>",
            "why": "<6-12 words>"}],
 "chase": "keep" | "stop" | "resume",
 "add": [{"title": "<short reminder title in his terms>", "when": "<his words>"}],
 "stage": "" | "Lost" | "Dormant" | "Booked",
 "understood": "<one sentence: what you will change>"}
Rules:
- Act ONLY where the note clearly refers to a listed step (by what it is, who it is to, or when). Everything else: keep.
- "cancel" when he says it is wrong, not needed, already done, or must not happen. "move" when he gives a new time;
  copy HIS words into "when" ("next Thursday", "after Eid", "1 November"), never compute a date.
- CHASE timing he states (e.g. "chase them Monday", "every 2 weeks") is applied elsewhere: use "move" on CHASE only
  when he names a new time for it; "stop" when he says stop chasing; "resume" when he says start chasing again.
- "add" only for a new action he explicitly asks to be reminded of. "stage" only when he says the deal is lost,
  dead, on hold for months (Dormant) or won (Booked).
- A fact without an instruction (a call happened, their budget) changes nothing here: return no items."""


def apply_note(deal_id: int, note: str, by: str = "") -> dict:
    """File his note on the deal and apply what it says about the listed steps. Returns {done, understood, summary}."""
    from . import crm, engine, flow, pipeline, provider, reminders
    before = summary(deal_id)
    if not before:
        return {"done": [], "understood": "", "summary": None}
    crm.add_project_note(int(deal_id), note, by=by)       # context + chase/touch timing + waiting email/quotation
    lines = "\n".join(f"{s['id']} ({s['date'] or 'no date'}): {s['text']}" for s in before["steps"])
    try:
        out = provider.think_json(_NOTE_SYS, f"TODAY: {datetime.now(_GST):%a %d %b %Y}\nOPPORTUNITY: {before['title']} "
                                             f"(stage {before['stage']})\n\nNEXT STEPS:\n{lines}\n\nHIS NOTE:\n{note[:2500]}",
                                  model=provider.MODEL_ROUTER, max_tokens=700, purpose="pipeline-review") or {}
    except Exception as e:  # noqa: BLE001
        print(f"[review] note on deal {deal_id}: {type(e).__name__}: {e}", flush=True)
        out = {}
    done, ids = [], {s["id"]: s for s in before["steps"]}
    co = next(({"slug": c["slug"], "id": c["id"]} for c in db.query("select id, slug from companies where kind='owned'")
               if crm._org(c["slug"]).lower() == (before["company"] or "").lower()), {"slug": "", "id": None})

    def when_of(words: str) -> datetime | None:
        try:
            w = reminders.parse_when(words)
        except Exception:  # noqa: BLE001
            w = None
        if not w:
            w = engine._resolve_their_date(co, words, datetime.now(timezone.utc).strftime("%a %d %b %Y"))
        return w if w and w > datetime.now(timezone.utc) else None

    for it in out.get("items") or []:
        key, verdict = str(it.get("id") or "").upper().strip(), str(it.get("verdict") or "keep")
        why = str(it.get("why") or "owner's review note")[:160]
        s = ids.get(key)
        if not s or verdict == "keep":
            continue
        if key == "CHASE":
            if verdict == "move" and it.get("when") and (w := when_of(str(it["when"]))):
                crm.set_next_chase(int(deal_id), w, None, by="owner's review", why=why)
                done.append(f"Email chase moved to {_d(w)}")
            elif verdict == "cancel":
                out["chase"] = "stop"
            continue
        if s["type"] == "reminder":
            rid = int(key[1:])
            if verdict == "cancel":
                db.execute("update reminders set status='cancelled' where id=%s", (rid,))
                done.append(f"Cancelled: {s['text'][:90]}")
            elif verdict == "move" and it.get("when") and (w := when_of(str(it["when"]))):
                db.execute("update reminders set due_at=%s, status='pending', snooze_until=null where id=%s", (w, rid))
                done.append(f"Moved to {_d(w)}: {s['text'][:90]}")
        elif s["type"] == "card" and verdict == "cancel":
            t = db.one("select id, kind, request from tasks where id=%s", (s["card"],))
            rq = (t or {}).get("request") or {}
            if t and t["kind"] != "quotation" and rq.get("kind") not in ("proposal", "creative_proposal"):
                db.execute("update tasks set status='cancelled', updated_at=now(), request = request || %s::jsonb "
                           "where id=%s", (db.Json({"cancelled_reason": f"Pipeline review: {why}"}), t["id"]))
                done.append(f"Cancelled card #{t['id']}: {s['text'][:80]}")
            else:
                done.append(f"Left card #{s['card']} for you to skip (quotations and proposals are never cancelled here)")
    chase = str(out.get("chase") or "keep")
    if chase == "stop":
        db.execute("update crm_projects set next_followup=null where id=%s", (int(deal_id),))
        pipeline.log_deal(int(deal_id), "chase_stopped", f"Owner's review: stop chasing ({note[:140]})")
        done.append("Email chase stopped")
    elif chase == "resume" and not before["steps"][0].get("when"):
        crm.set_next_chase(int(deal_id), datetime.now(timezone.utc) + timedelta(days=1), None, by="owner's review",
                           why=note[:120])
        done.append("Email chase restarted from tomorrow")
    for a in out.get("add") or []:
        w = when_of(str(a.get("when") or ""))
        if w and a.get("title"):
            reminders.create(str(a["title"])[:200], w, company_id=co.get("id"), created_by=by or "owner", target_type="deal",
                             target_id=str(int(deal_id)))
            done.append(f"Reminder added for {_d(w)}: {str(a['title'])[:80]}")
    if out.get("stage") in ("Lost", "Dormant", "Booked"):
        done.append(f"Not moved: you said {out['stage']}. Move the stage on the opportunity to confirm.")
    if done:
        pipeline.log_deal(int(deal_id), "note", "Pipeline review: " + "; ".join(done)[:600])
    try:
        flow.mark(int(deal_id), "owner review note")
    except Exception:  # noqa: BLE001
        pass
    return {"done": done, "understood": str(out.get("understood") or "")[:300], "summary": summary(deal_id)}
