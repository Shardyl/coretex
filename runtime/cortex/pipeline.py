"""Sales-loop integrity — the closed loop between communications and the pipeline.

Every sales email (sent by Cortex OR sent manually by the team) is anchored to its deal, logged on
that deal's timeline, mined for the promises it makes (commitments -> reminders) and the deadlines
the client states (deadlines -> reminders). Drafting reads the same timeline back, so every card is
proposed with the full flow of the opportunity behind it. Policy (cadence, tone, what to promise)
lives in skills; this module is plumbing only — and every must-be-real value (dates, ids) is
computed and stamped by code, never invented by a model.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Json

from . import crm, db, gmail, notifications, provider, reminders, store


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_deal(deal_id: int, event: str, text: str) -> None:
    """Append one event to the deal's timeline. The timeline is the deal's memory: drafting,
    follow-ups and the cockpit all read it."""
    if not deal_id:
        return
    ev = {"ts": _now_iso(), "event": event, "text": (text or "")[:1200]}
    db.execute("update crm_projects set history = history || %s::jsonb, updated_at=now() where id=%s",
               (Json([ev]), int(deal_id)))


def deal_context(deal_id: int, limit: int = 12) -> str:
    """The deal's recent timeline as a brief block, so a draft is written with the whole flow in
    view (what we promised, what they asked, where the deal stands)."""
    d = db.one("select title, stage, value, currency, note, history from crm_projects where id=%s",
               (int(deal_id),))
    if not d:
        return ""
    hist = d.get("history") or []
    lines = [f"- {h.get('ts', '')[:16]} [{h.get('event', '')}] {h.get('text', '')}" for h in hist[-limit:]]
    out = (f"DEAL TIMELINE for '{d['title']}' (stage {d['stage']}"
           + (f", value {d['currency']} {d['value']}" if d.get("value") else "") + "):\n")
    if d.get("note"):
        out += f"Note: {d['note']}\n"
    return out + ("\n".join(lines) if lines else "- (no events logged yet)")


# ---------- commitments (what WE promised in an outbound email) ----------

_DUE_DAYS = {"today": 1, "tomorrow": 1, "week": 5, "none": 3}


def _commitment_due(hint: str) -> datetime:
    """Code computes the reminder date from the extractor's hint. An explicit ISO date is used as
    stated; vague hints map to fixed windows; no hint means a 3-day check-in."""
    h = (hint or "none").strip().lower()
    m = re.match(r"^days:(\d{1,2})$", h)
    if m:
        return datetime.now(timezone.utc) + timedelta(days=min(int(m.group(1)), 30))
    try:
        d = datetime.fromisoformat(h)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if d > datetime.now(timezone.utc):
            return d
    except ValueError:
        pass
    return datetime.now(timezone.utc) + timedelta(days=_DUE_DAYS.get(h, 3))


def extract_commitments(draft: str) -> list[dict]:
    """Promises this email makes to the recipient (things WE now owe). Extraction only — the model
    quotes the email; it never invents obligations or dates."""
    if not (draft or "").strip():
        return []
    try:
        out = provider.think_json(
            "Extract the concrete promises/commitments THIS email makes to its recipient - deliverables or "
            "actions the sender now owes (e.g. 'revised quotation coming', 'will send samples', 'will call "
            "Tuesday'). Politeness ('happy to help', 'any questions, ask') is NOT a commitment. Return "
            '{"commitments":[{"text":"<short restatement>","due_hint":"<ISO date/datetime ONLY if the email '
            "states one; else one of: today | tomorrow | week | days:N | none>\"}]}. Empty list when there "
            "are none. Never invent dates.",
            draft[:3000], model=provider.MODEL_ROUTER, purpose="commitment-extract")
        return [c for c in (out or {}).get("commitments", []) if (c.get("text") or "").strip()][:5]
    except Exception:  # noqa: BLE001 — extraction is best-effort; the send already happened
        return []


def record_send(task: dict, env: dict, company: dict, *, manual: bool = False,
                deal_id: int | None = None, draft: str | None = None) -> None:
    """An outbound sales email went out (Cortex-approved, or detected in a sent folder). Log it on
    the deal timeline and turn its promises into tracked commitments with reminders."""
    req = (task or {}).get("request") or {}
    did = deal_id or req.get("deal_id") or (task or {}).get("deal_id")
    body = draft if draft is not None else ((task or {}).get("draft") or "")
    to = (env.get("to") or "").strip()
    subj = (env.get("subject") or "").strip()
    frm = (env.get("from") or "").strip()
    if not did:
        return
    who = "manually sent" if manual else "sent (Cortex-approved)"
    log_deal(did, "email_out_manual" if manual else "email_out",
             f"{who} from {frm or 'company mailbox'} to {to}: {subj or '(no subject)'}")
    try:   # FIRST settle what this email fulfils, THEN track the new promises it makes
        settle_commitments(int(did), body, to)
    except Exception:  # noqa: BLE001
        pass
    try:   # a quotation/proposal going out advances Opportunity -> Quote
        maybe_advance_on_send(int(did), task, env)
    except Exception:  # noqa: BLE001
        pass
    for c in extract_commitments(body):
        due = _commitment_due(c.get("due_hint"))
        log_deal(did, "commitment", f"OWED to {to}: {c['text']} (check-in {due.date().isoformat()})")
        try:
            reminders.create(f"Commitment owed to {to}: {c['text']} (deal {did})", due,
                             company_id=(company or {}).get("id"), target_type="deal", target_id=did,
                             created_by="cortex-pipeline")
        except Exception:  # noqa: BLE001
            pass


# ---------- client deadlines (what THEY stated in an inbound email) ----------

def extract_deadlines(body: str) -> list[dict]:
    """Deadlines the CLIENT explicitly states ('respond by 1 Sep 3pm', 'need it before Friday's
    board meeting'). Explicit statements only — never inferred urgency."""
    if not (body or "").strip():
        return []
    today = datetime.now(timezone.utc).strftime("%A %Y-%m-%d")
    try:
        out = provider.think_json(
            f"Today is {today}. Extract deadlines this email EXPLICITLY states (a date, day or time by which "
            "the sender needs something). Return "
            '{"deadlines":[{"what":"<what is due>","when_iso":"YYYY-MM-DD or YYYY-MM-DDTHH:MM"}]}. '
            "Resolve weekday names to the next such date. Empty list when no deadline is stated. Never "
            "infer or invent a deadline.",
            body[:3000], model=provider.MODEL_ROUTER, purpose="deadline-extract")
        return [d for d in (out or {}).get("deadlines", []) if d.get("when_iso")][:3]
    except Exception:  # noqa: BLE001
        return []


def record_inbound(e: dict, deal: dict | None, company: dict) -> None:
    """An inbound email on an active deal: log it on the timeline and catch any stated deadline as
    a reminder (fires a day ahead, or same-day when the deadline is nearer than that)."""
    if not deal:
        return
    did = deal.get("id") if isinstance(deal, dict) else int(deal)
    sender = (e.get("email") or "").strip()
    subj = (e.get("subject") or "").strip()
    snippet = re.sub(r"\s+", " ", (e.get("body") or e.get("snippet") or ""))[:220]
    log_deal(did, "email_in", f"from {sender}: {subj or '(no subject)'} - {snippet}")
    for d in extract_deadlines(e.get("body") or ""):
        try:
            when = datetime.fromisoformat(d["when_iso"])
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if when <= now:
            continue
        remind_at = max(when - timedelta(days=1), now + timedelta(minutes=30))
        log_deal(did, "deadline", f"CLIENT DEADLINE {d['when_iso']}: {d['what']} (from {sender})")
        try:
            reminders.create(f"Client deadline {d['when_iso']}: {d['what']} ({sender}, deal {did})",
                             remind_at, company_id=(company or {}).get("id"), target_type="deal",
                             target_id=did, priority="high", created_by="cortex-pipeline")
        except Exception:  # noqa: BLE001
            pass
    try:   # does this email need a deliverable PREPARED (quote revision, proposal...), not just a reply?
        suggest_next_step(e, deal, company)
    except Exception:  # noqa: BLE001
        pass


# ---------- sent-folder sweep (manual sends must not escape the loop) ----------

def _own_domains() -> set[str]:
    """Domains of our own mailboxes — mail between them is internal, never deal correspondence."""
    doms = set()
    for r in db.query("select value from settings where key like %s or key like %s",
                      ("gmail_account%", "gmail_send_account%")):
        v = r["value"] if isinstance(r["value"], str) else str(r["value"] or "")
        v = v.strip('" ')
        if "@" in v:
            doms.add(v.split("@")[-1].lower())
    return doms


def _sweep_mailbox(co: dict, mailbox: str, rt_key: str, client: str | None, own: set[str]) -> int:
    """One mailbox's sent folder: log manual sends against their deals; surface sales mail that has
    no deal. Cortex's own sends are recognised by their gmail id and skipped."""
    seen_key = f"sent_seen:{rt_key}"
    seen = set(db.setting_get(seen_key) or [])
    try:
        msgs = gmail.list_recent(days=2, limit=25, rt_key=rt_key, company=client,
                                 q="in:sent newer_than:2d", skip=seen)
    except Exception:  # noqa: BLE001 — one dead mailbox must not kill the sweep
        return 0
    handled = 0
    for m in msgs:
        gid = m.get("gmail_id")
        seen.add(gid)
        if db.one("select 1 from decisions where snapshot->>'gmail_id'=%s limit 1", (gid,)):
            continue                                 # Cortex sent this one itself — already recorded
        to_hdr = (m.get("to") or "")
        m_to = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", to_hdr)
        to = (m_to.group(0) if m_to else "").lower()
        if not to or to.split("@")[-1] in own:
            continue                                 # internal mail is not deal correspondence
        slug = co.get("slug")
        deals = crm.active_deals_for_email(to, slug)
        deal = deals[0] if deals else crm.open_deal_for_domain(to, slug)
        env = {"to": to, "subject": m.get("subject") or "", "from": mailbox}
        if deal:
            record_send({}, env, co, manual=True, deal_id=deal["id"], draft=m.get("body") or "")
            try:
                crm.resume_followups(int(deal["id"]))   # a human replied — cadence re-arms at its gap
            except Exception:  # noqa: BLE001
                pass
            handled += 1
        else:
            q = db.setting_get(f"qual:email:{to}")
            known = db.one("select 1 from crm_master where lower(email)=lower(%s) "
                           "and coalesce(lead_status,'') <> '' limit 1", (to,))
            if q or known:
                notifications.notify(
                    "Untracked sales email",
                    f"{mailbox} manually emailed {to} ('{(m.get('subject') or '')[:80]}') but this contact "
                    "has NO open opportunity. If this is a live sales conversation, create the opportunity "
                    "so the flow is tracked.", category="crm", company_id=co.get("id"),
                    dedup_key=f"untracked-sales:{to}")
                handled += 1
    db.setting_set(seen_key, sorted(seen)[-500:])
    return handled


def sweep_sent(min_gap_minutes: int = 30) -> int:
    """All companies' sending mailboxes: pick up mail the team sent by hand so the pipeline never
    loses a thread. Self-rate-limited; called from the engine loop."""
    last = db.setting_get("sent_sweep_at")
    now = datetime.now(timezone.utc)
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(minutes=min_gap_minutes):
                return 0
        except ValueError:
            pass
    db.setting_set("sent_sweep_at", now.isoformat())
    own = _own_domains()
    handled = 0
    for r in db.query("select key, value from settings where key like %s", ("gmail_account:%",)):
        parts = r["key"].split(":")                      # gmail_account:<slug>[:<who>]
        slug = parts[1]
        rt_key = "gmail_refresh_token:" + ":".join(parts[1:])
        if not db.setting_get(rt_key):
            continue
        co = store.get_company_by_slug(slug)
        if not co:
            continue
        mailbox = (r["value"] if isinstance(r["value"], str) else str(r["value"] or "")).strip('" ')
        try:
            handled += _sweep_mailbox(co, mailbox, rt_key, slug, own)
        except Exception:  # noqa: BLE001
            continue
    return handled


# ---------- stage engine (deterministic transitions; the model never moves a stage) ----------

def maybe_advance_on_send(deal_id: int, task: dict, env: dict) -> None:
    """A quotation/proposal going out moves a forecast deal Opportunity -> Quote. The trigger is a
    fact (a Quotation/Proposal document attached to the send, or named in the subject), never a guess."""
    d = db.one("select stage from crm_projects where id=%s", (int(deal_id),))
    if not d or d.get("stage") != "Opportunity":
        return
    req = (task or {}).get("request") or {}
    names = " ".join([a.get("filename") or "" for a in req.get("attach_docs") or []]
                     + list(req.get("attachment_names") or [])
                     + [(env or {}).get("subject") or ""]).lower()
    if "quotation" in names or "proposal" in names:
        try:
            crm.set_project_stage(int(deal_id), "Quote")   # logs the stage_change on the timeline itself
        except Exception:  # noqa: BLE001
            pass


PLAN_STAGES = ("Booked", "Production", "Recurring", "Delivered", "Final Payment")


def plan_request(deal_id: int, title: str, stage: str, why: str) -> dict:
    """The brief behind a PROJECT PLAN card - the living plan for one project. It is never sent anywhere:
    it says where the project stands and WHAT HAPPENS NEXT with dates, and confirming it arms those steps.
    Rebuilt from the deal timeline + notes each time, so a note or a WhatsApp update re-plans it."""
    d = db.one("select note from crm_projects where id=%s", (int(deal_id),)) or {}
    rems = db.query("select id, title, to_char(due_at,'DD Mon HH24:MI') w from reminders where "
                    "target_type='deal' and target_id=%s and status in ('pending','snoozed') order by due_at",
                    (str(int(deal_id)),))
    open_steps = "\n".join(f"- [reminder {r['id']}] {r['title']} - due {r['w']}" for r in rems) or "(none)"
    brief = (
        f"PROJECT PLAN for '{title}' (deal {deal_id}, stage {stage}) - triggered because {why}. This is an "
        "INTERNAL plan for Rashad and is never sent to anyone. Write it tight and scannable:\n"
        "1) WHERE IT STANDS - two or three lines of fact from the record below.\n"
        "2) WHAT WE OWE - outstanding deliverables/commitments, with dates where the record states them.\n"
        "3) NEXT STEPS - each a single line naming WHO does it, WHAT it is and WHEN. If we are waiting on "
        "the client, say what for and when we would chase.\n"
        "4) RISKS - only real ones visible in the record (missing brief, slipped date, unpaid invoice).\n"
        "Facts ONLY from the record below: never invent dates, amounts or scope. If something essential is "
        "missing (no brief, no contact), make saying so the first next step.\n")
    if d.get("note"):
        brief += f"\nTEAM NOTES ON THIS PROJECT: {d['note']}\n"
    brief += f"\nALREADY SCHEDULED on this deal (do not duplicate these):\n{open_steps}\n"
    brief += "\nDEAL TIMELINE:\n" + deal_context(deal_id, limit=20)
    return {"brief": brief, "deal_id": int(deal_id), "title": f"Project plan - {title}"}


def replan(deal_id: int, why: str = "the plan was updated") -> int | None:
    """Re-issue the plan for a project: a note added, a WhatsApp/phone update, a stage move, or Talk asking
    for it. ONE open plan card per deal - an existing open card is refreshed, never stacked."""
    d = db.one("select id, title, stage, company from crm_projects where id=%s", (int(deal_id),))
    if not d:
        return None
    co = db.one("select id, slug from companies where lower(name)=lower(%s) or lower(slug)=lower(%s)",
                (d.get("company") or "", d.get("company") or ""))
    sk = store.get_skill_by_key(co["id"], "email-handling") if co else None
    if not (co and sk):
        return None
    req = plan_request(d["id"], d["title"], d["stage"], why)
    ex = db.one("select id from tasks where kind='project_plan' and status in "
                "('new','drafting','awaiting_approval','awaiting_correction') and "
                "(deal_id=%s or request->>'deal_id'=%s) order by id desc limit 1",
                (int(deal_id), str(int(deal_id))))
    if ex:      # refresh the open plan in place: never two plans for one project
        db.execute("update tasks set request=%s, status='new', draft=null, manager=null, updated_at=now() "
                   "where id=%s", (Json(req), ex["id"]))
        return ex["id"]
    t = store.create_card(co["id"], sk["id"], "project_plan", req, deal_id=int(deal_id))
    return (t or {}).get("id")


def on_stage_change(deal: dict, old: str, new: str, actor: str = "system") -> None:
    """Called by crm.set_project_stage AFTER a stage moves. Won = the deal becomes a live project:
    kickoff lands as a reminder-spawned Inbox card so delivery starts tracked, not ad hoc.
    Close & review = the wrap-up (final assets, testimonial ask, portfolio entry) is surfaced.
    actor='owner' means Rashad moved the stage himself: the WORK (kickoff card, clocks) still runs,
    but the narration notification is suppressed - never echo his own click back at him (owner rule
    30 Aug). Notifications fire only when the SYSTEM moved the stage."""
    try:
        did = int(deal.get("id"))
        co = db.one("select id, slug, name from companies where lower(name)=lower(%s) or lower(slug)=lower(%s)",
                    (deal.get("company") or "", deal.get("company") or ""))
        won = set(crm.WON_STAGES)
        title = deal.get("title") or f"deal {did}"
        if new in won and old not in won:
            log_deal(did, "project_start", f"WON at stage {new} - deal is now a live project")
            due = datetime.now(timezone.utc) + timedelta(hours=2)
            reminders.create(
                f"Project plan: {title} (deal {did})", due,
                company_id=(co or {}).get("id"), target_type="deal", target_id=did, priority="high",
                created_by="cortex-pipeline",
                action={"company": (co or {}).get("slug"), "skill": "email-handling",
                        "kind": "project_plan",
                        "request": plan_request(did, title, new, "the deal was just WON")})
            if actor != "owner":
                notifications.notify("Deal won - project kickoff queued",
                                     f"{title} moved to {new}. A kickoff card will land in the Inbox; "
                                     "delivery correspondence now routes on the project lane.",
                                     category="crm", company_id=(co or {}).get("id"),
                                     target_type="deal", target_id=did)
        if new in PLAN_STAGES and old in PLAN_STAGES and new != old:
            replan(did, f"the project moved from {old} to {new}")
        if new == "Close & review" and old != "Close & review":
            log_deal(did, "project_close", "moved to Close & review - wrap-up owed")
            if actor != "owner":
                notifications.notify("Project closing - wrap-up",
                                     f"{title} is in Close & review: confirm final files delivered + paid, "
                                     "ask for the testimonial/review, and add the film to the media library.",
                                     category="crm", company_id=(co or {}).get("id"),
                                     target_type="deal", target_id=did)
    except Exception:  # noqa: BLE001 — stage bookkeeping must never break the stage move itself
        pass


# ---------- commitment fulfilment (a send can settle what an earlier send promised) ----------

def settle_commitments(deal_id: int, sent_body: str, to: str) -> None:
    """When a new email goes out on a deal, check the deal's OPEN commitment reminders: any the model
    judges genuinely fulfilled by this email is marked done and logged. Judgement is per-commitment
    and conservative - unclear stays open."""
    open_rs = db.query("select id, title from reminders where created_by='cortex-pipeline' and "
                       "status='pending' and target_type='deal' and target_id=%s and "
                       "(title like %s or title like %s)",
                       (str(int(deal_id)), "Commitment owed%", "Meeting commitment%"))
    if not open_rs or not (sent_body or "").strip():
        return
    listing = "\n".join(f"[{r['id']}] {r['title']}" for r in open_rs)
    try:
        out = provider.think_json(
            "An email was just sent on a deal. Below are the OPEN commitments previously promised to this "
            "client. Decide which (if any) THIS email genuinely fulfils - the promised thing is actually "
            'delivered/attached/answered in it, not merely mentioned. Return {"fulfilled_ids":[<int>...]}. '
            "Be conservative: when unclear, leave it open (empty list).",
            f"OPEN COMMITMENTS:\n{listing}\n\nEMAIL JUST SENT (to {to}):\n{(sent_body or '')[:3000]}",
            model=provider.MODEL_ROUTER, purpose="commitment-settle")
        ids = {int(i) for i in (out or {}).get("fulfilled_ids", [])}
    except Exception:  # noqa: BLE001
        return
    for r in open_rs:
        if r["id"] in ids:
            reminders.mark_done(r["id"])
            log_deal(deal_id, "commitment_done", r["title"])


# ---------- next-step engine (the inbound decides what we should PREPARE, not just what to reply) ----------

def suggest_next_step(e: dict, deal: dict, company: dict) -> None:
    """An inbound on a deal often needs a deliverable prepared (a revised quotation, a proposal, a
    document), not only a reply. Detect that and spawn the PREP work as its own Inbox card, so the
    reply and the thing it promises both exist. Conservative: most mail needs nothing."""
    gid = (e.get("gmail_id") or "").strip()
    did = int(deal["id"]) if isinstance(deal, dict) else int(deal)
    seen_key = f"nextstep_seen:{did}"
    seen = db.setting_get(seen_key) or []
    if gid and gid in seen:
        return
    try:
        out = provider.think_json(
            "You read one inbound client email on an active deal (timeline below). Decide if it requires an "
            "INTERNAL DELIVERABLE to be prepared beyond a reply - e.g. a revised/new quotation, a proposal, "
            "a document, a booking. Politeness, questions answerable in prose, or FYI mail need nothing. Return "
            '{"action":"none|prepare_quotation|prepare_proposal|prepare_document|other","what":"<one line>"}. '
            "Be conservative - when in doubt, none.",
            deal_context(did) + "\n\nINBOUND EMAIL from " + (e.get("email") or "") + ":\n"
            + ((e.get("body") or "")[:2500]),
            model=provider.MODEL_ROUTER, purpose="next-step")
    except Exception:  # noqa: BLE001
        return
    act = (out or {}).get("action") or "none"
    what = ((out or {}).get("what") or "").strip()
    if gid:
        db.setting_set(seen_key, (seen + [gid])[-50:])
    if act == "none" or not what:
        return
    log_deal(did, "next_step", f"{act}: {what}")
    sk = store.get_skill_by_key(company["id"], "sales-quotation") \
        or store.get_skill_by_key(company["id"], "email-handling")
    if not sk:
        return
    title = deal.get("title") or f"deal {did}"
    brief = (f"NEXT STEP for deal {did} ({title}) - the client latest email requires: {what} ({act}). "
             "Prepare it as a clear internal work-up the owner can approve and act on: exactly what "
             "changes/content is needed, based ONLY on the timeline below and the client's words. Any price "
             "or date the owner has not stated is marked as OWNER TO CONFIRM - never invented.\n\n"
             + deal_context(did) + "\n\nTHEIR EMAIL:\n" + ((e.get("body") or "")[:2000]))
    t = store.create_card(company["id"], sk["id"], "content",
                          {"brief": brief, "deal_id": did, "title": f"Next step: {what[:70]}"},
                          deal_id=did)
    if t:
        db.execute("update tasks set deal_id=%s where id=%s", (did, t["id"]))
        notifications.notify("Next step queued", f"{title}: {what}",
                             category="crm", company_id=company.get("id"),
                             target_type="deal", target_id=did)


# ---------- meetings feed the same loop ----------

def record_meeting(deal_id: int, company_id: int | None, title: str, summary: str) -> None:
    """A meeting on a deal lands on its timeline, and the things OUR side committed to in it become
    tracked commitments - the same loop as email, so meetings stop leaking promises."""
    if not deal_id:
        return
    log_deal(deal_id, "meeting", f"{title}: {(summary or '')[:900]}")
    try:
        out = provider.think_json(
            "From this meeting summary, extract only the action items OUR side (the production company) "
            "committed to - things we now owe the client. Return "
            '{"commitments":[{"text":"<short>","due_hint":"<ISO date ONLY if stated; else one of: today | '
            'tomorrow | week | days:N | none>"}]}. Client-side actions and vague intentions are excluded. '
            "Never invent dates.",
            (summary or "")[:3000], model=provider.MODEL_ROUTER, purpose="meeting-commitments")
        cs = [c for c in (out or {}).get("commitments", []) if (c.get("text") or "").strip()][:5]
    except Exception:  # noqa: BLE001
        cs = []
    for c in cs:
        due = _commitment_due(c.get("due_hint"))
        log_deal(deal_id, "commitment",
                 f"OWED (from meeting {title}): {c['text']} (check-in {due.date().isoformat()})")
        try:
            reminders.create(f"Meeting commitment: {c['text']} (deal {deal_id})", due,
                             company_id=company_id, target_type="deal", target_id=deal_id,
                             created_by="cortex-pipeline")
        except Exception:  # noqa: BLE001
            pass


# ---------- opportunity research (the consultant's first look, owner-approved Fable 5) ----------

def research_opportunity(deal_id: int, company: dict, inquiry: dict) -> str:
    """ONE research pass when an opportunity is born: who the sender is, what their project actually
    touches (locations, logistics, market reality), and 2-3 TRUE insights we can naturally contribute
    in replies - the consultant posture, grounded. Runs Fable 5 with live web search (owner-approved
    exception to the Haiku default, 2026-08-30; fires once per deal, guarded). The brief lands on the
    deal timeline, so every subsequent draft reads it. Returns the brief ('' on any failure)."""
    try:
        did = int(deal_id)
        hist = db.one("select history from crm_projects where id=%s", (did,))
        if any(h.get("event") == "research" for h in (hist or {}).get("history") or []):
            return ""
        out = provider.think_research(
            "You are the research arm of a Dubai production company's sales desk. Given a new enquiry, "
            "produce a compact INSIGHT BRIEF (under 170 words, plain text) for the person drafting our "
            "replies:\n"
            "1. SENDER: who they are / their company, verified via search (one line; say UNVERIFIED if "
            "search finds nothing solid).\n"
            "2. MODE: 'direct' (founder/creator, personal enthusiasm) or 'procurement' (RFP/SOW language, "
            "numbered asks, deadlines).\n"
            "3. INSIGHTS: 2-3 concrete, TRUE, useful facts about what their project touches - locations "
            "and travel realities, permits/permissions, market or industry context, timing realities. "
            "Things a knowledgeable consultant would mention that the sender did not ask about.\n"
            "Facts only - verified or common professional knowledge; mark anything uncertain as UNCERTAIN; "
            "NEVER invent names, numbers or claims. No advice on pricing.",
            f"Company receiving the enquiry: {company.get('name')}\n"
            f"Sender: {inquiry.get('name') or ''} <{inquiry.get('email') or ''}>\n"
            f"Subject: {inquiry.get('subject') or ''}\n\nTheir message:\n{(inquiry.get('message') or '')[:2500]}",
            purpose="lead-research", company=company.get("slug"))
        if out:
            log_deal(did, "research", out[:1200])
        return out
    except Exception:  # noqa: BLE001 — research is a bonus, never a blocker
        return ""
