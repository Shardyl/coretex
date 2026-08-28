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


# ---------- sent-folder sweep (manual sends must not escape the loop) ----------

def _own_domains() -> set[str]:
    """Domains of our own mailboxes — mail between them is internal, never deal correspondence."""
    doms = set()
    for r in db.query("select value from settings where key like 'gmail_account%' "
                      "or key like 'gmail_send_account%'"):
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
    for r in db.query("select key, value from settings where key like 'gmail_account:%'"):
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
