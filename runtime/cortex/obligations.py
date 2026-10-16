"""What Cortex owes, and what completes it (architecture audit, step 2d, 13 Oct 2026).

Cortex sets reminders for things it owes: send the proposal, keep a promise, meet a client's deadline, take a next
step, chase a payment. Before this, almost none of them knew what completes them. "Prepare and send the proposal"
fired after the proposal had gone (MENA Water, deal 155); client deadlines and next steps were never settled by the
email that delivered them; notifications never cleared when the thing they announced was resolved. Only closing the
deal cleaned anything up.

KINDS names each kind of reminder by its title and says how it completes. The three entry points are called by the
code where the thing happens:
    on_sent(deal_id, body, to, subject, attachment_names)   pipeline.record_send, every email that goes out on a deal
    on_stage(deal_id, old, new)                              crm.set_project_stage
    on_task_closed(task_id)                                  store.update_task, when a card leaves the open states
Only reminders Cortex created (created_by 'cortex%') are ever settled here: the owner's own are his.
"""
from __future__ import annotations

import re

from . import db

KINDS = [
    # prefix of the reminder title             how it completes
    {"key": "proposal_owed", "prefix": "Prepare and send the proposal",
     "done_on": "a quotation or proposal sent on the deal, or the deal leaving Opportunity"},
    {"key": "commitment", "prefix": "Commitment owed to",
     "done_on": "an email that actually delivers it (judged)"},
    {"key": "meeting_commitment", "prefix": "Meeting commitment",
     "done_on": "an email that actually delivers it (judged)"},
    {"key": "client_deadline", "prefix": "Client deadline",
     "done_on": "an email that actually delivers what they asked for (judged)"},
    {"key": "next_step", "prefix": "Next step on",
     "done_on": "an email that actually delivers it (judged)"},
    {"key": "conversation_commitment", "created_by": "cortex-commitment",
     "done_on": "an email that actually delivers it (judged)"},
    {"key": "tender_close", "prefix": "Tender closes",
     "done_on": "the deal moving to won work or Lost"},
]
JUDGED = ("commitment", "meeting_commitment", "client_deadline", "next_step", "conversation_commitment")

# ---------------------------------------------------------------------------------------------------------------------
# OWED WORK CLOSES ONLY WITH PROOF (owner, 16 Oct 2026: "make sure this is fundamentally fixed and not a patch job").
# The Inbox tidy closed Shama's Ramadan creative options (R350) on a guess ("the meeting was the deliverable
# checkpoint"), and five more owed items with it. Any part of Cortex could close any reminder with one UPDATE, so the
# only protection was each caller's good judgement. Now the DATABASE refuses: a reminder for something we owe
# (`owed`, set on insert by trigger from KINDS below, never by a caller) can move to done or cancelled only when the
# same UPDATE carries `closed_evidence` of a kind below, with its reference. A guess has no proof to give, so it can
# only FLAG (`suspect`), which the owner confirms on Review next steps. Reopening clears the evidence.
EVIDENCE = {                       # kind -> the key it must carry
    "owner": "who",                # a person closed it: the cockpit, Talk, his review note, his "not needed"
    "sent_email": "ref",           # an email we sent delivered it (the card or the message), quote verified by code
    "client_words": "quote",       # the client wrote that it is done or not needed, quote verified by code
    "thread": "quote",             # the thread shows it kept (our message or theirs), quote verified by code
    "deal_closed": "stage",        # the deal moved to a stage that owes nothing
    "stage": "stage",              # the stage itself completes it (proposal owed -> left Opportunity)
    "duplicate": "kept",           # merged into another open reminder for the same thing
    "rule": "ref",                 # a deterministic owner rule (a promise that waits on the client)
}
_CLOSED = ("done", "cancelled")


def _owed_sql() -> str:
    """The SQL test for 'owed', built from KINDS so the trigger and the Python agree by construction."""
    parts = []
    for k in KINDS:
        if k.get("prefix"):
            parts.append("NEW.title like " + "'" + k["prefix"].replace("'", "''") + "%'")
        if k.get("created_by"):
            parts.append("NEW.created_by = '" + k["created_by"] + "'")
    return ("coalesce(NEW.created_by,'') like 'cortex%' and coalesce(NEW.created_by,'') <> 'cortex-touch' and ("
            + " or ".join(parts) + ")")


_GUARDED = False


def ensure_guard(force: bool = False) -> None:
    """Columns, the insert trigger that marks owed work, the update trigger that refuses a close without proof, and
    a backfill of `owed`. Idempotent; once per process (reminders.ensure_schema runs every minute)."""
    global _GUARDED
    if _GUARDED and not force:
        return
    kinds = ", ".join("'" + k + "'" for k in EVIDENCE)
    with db.connect() as c:
        c.execute("alter table reminders add column if not exists owed boolean not null default false")
        c.execute("alter table reminders add column if not exists closed_evidence jsonb")
        c.execute("alter table reminders add column if not exists closed_why text")
        c.execute("alter table reminders add column if not exists suspect jsonb")
        c.execute(f"""
create or replace function reminders_owed_mark() returns trigger language plpgsql as $f$
begin
  NEW.owed := ({_owed_sql()});
  return NEW;
end $f$;""")
        c.execute(f"""
create or replace function reminders_owed_guard() returns trigger language plpgsql as $f$
begin
  if NEW.status not in ('done','cancelled') then
    NEW.closed_evidence := null;
    NEW.closed_why := null;
    return NEW;
  end if;
  if OLD.owed and OLD.status not in ('done','cancelled') then
    if NEW.closed_evidence is null or NEW.closed_evidence->>'kind' is null
       or NEW.closed_evidence->>'kind' not in ({kinds}) then
      raise exception 'OWED_GUARD: reminder % is owed work; close it through obligations.close with evidence', OLD.id;
    end if;
  end if;
  return NEW;
end $f$;""")
        c.execute("drop trigger if exists reminders_owed_mark on reminders")
        c.execute("create trigger reminders_owed_mark before insert on reminders for each row "
                  "execute function reminders_owed_mark()")
        c.execute("drop trigger if exists reminders_owed_guard on reminders")
        c.execute("create trigger reminders_owed_guard before update on reminders for each row "
                  "execute function reminders_owed_guard()")
        c.execute("update reminders set owed = (" + _owed_sql().replace("NEW.", "") + ") where owed is distinct from ("
                  + _owed_sql().replace("NEW.", "") + ")")
    _GUARDED = True


def evidence(kind: str, **ref) -> dict:
    from datetime import datetime, timezone
    if kind not in EVIDENCE or not str(ref.get(EVIDENCE[kind]) or "").strip():
        raise ValueError(f"evidence '{kind}' needs '{EVIDENCE.get(kind)}'")
    return {"kind": kind, **{k: str(v)[:400] for k, v in ref.items()}, "at": datetime.now(timezone.utc).isoformat()}


def close(rid: int, status: str, ev: dict, why: str = "") -> dict | None:
    """THE way a reminder is closed. `ev` from evidence(). Returns the row, or None when it was already closed."""
    from psycopg.types.json import Json
    if status not in _CLOSED:
        raise ValueError(status)
    return db.execute("update reminders set status=%s, closed_evidence=%s, closed_why=%s where id=%s and "
                      "status not in ('done','cancelled') returning *", (status, Json(ev), (why or "")[:300], int(rid)))


def close_where(where: str, params: tuple, status: str, ev: dict, why: str = "") -> list[dict]:
    """Bulk close (a deal closing, a stage rule). `where` is the SQL after WHERE; open rows only."""
    from psycopg.types.json import Json
    return db.query(f"update reminders set status=%s, closed_evidence=%s, closed_why=%s where ({where}) and "
                    "status not in ('done','cancelled') returning id, title",
                    (status, Json(ev), (why or "")[:300]) + tuple(params))


def flag(rid: int, why: str, by: str = "inbox-tidy") -> None:
    """A guess that owed work is done or can wait: shown to the owner to confirm, never acted on."""
    from datetime import datetime, timezone
    from psycopg.types.json import Json
    db.execute("update reminders set suspect=%s where id=%s and status not in ('done','cancelled')",
               (Json({"why": (why or "")[:300], "by": by, "at": datetime.now(timezone.utc).isoformat()}), int(rid)))


def quote_in(quote: str, text: str) -> bool:
    """Code checks a model's proof: the quoted words really are in the message (spacing and case ignored)."""
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()     # noqa: E731
    q = norm(quote)
    return len(q.split()) >= 4 and q in norm(text)
_QUOTE_RX = re.compile(r"quotation|proposal|\bSEN-\d{4}-\d{4}\b", re.I)


def _open(deal_id: int) -> list[dict]:
    """Cortex's reminders on the deal that are still owed: pending or snoozed, and FIRED ones too (a fired nudge is
    still owed until the thing is done; settling it clears the notice it raised)."""
    return db.query("select id, title, created_by from reminders where target_type='deal' and target_id=%s and "
                    "status in ('pending','snoozed','fired') and coalesce(created_by,'') like 'cortex%%'", (str(int(deal_id)),))


def kind_of(r: dict) -> str | None:
    for k in KINDS:
        if k.get("prefix") and (r.get("title") or "").startswith(k["prefix"]):
            return k["key"]
        if k.get("created_by") and r.get("created_by") == k["created_by"]:
            return k["key"]
    return None


def _done(deal_id: int, r: dict, why: str, ev: dict) -> None:
    from . import pipeline
    if not close(int(r["id"]), "done", ev, why):
        return
    db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and id = "
               "(select last_notification_id from reminders where id=%s)", (int(r["id"]),))   # its nudge is answered too
    pipeline.log_deal(int(deal_id), "commitment_done", f"{r['title'][:160]} ({why})")


def on_sent(deal_id: int, body: str, to: str, subject: str = "", attachment_names: list | None = None) -> list[int]:
    """An email went out on the deal. Deterministic first (a quotation or proposal settles 'send the proposal'),
    then one judgement over everything an email can deliver. Returns the reminder ids settled."""
    settled = []
    rows = _open(deal_id)
    if not rows:
        return settled
    names = " ".join(str(a) for a in (attachment_names or []))
    _sent_ref = (subject or "(no subject)") + (f" | {names}" if names else "")
    if _QUOTE_RX.search(names) or _QUOTE_RX.search(subject or ""):
        for r in rows:
            if kind_of(r) == "proposal_owed":
                _done(deal_id, r, "the quotation or proposal went out", evidence("sent_email", ref=_sent_ref))
                settled.append(r["id"])
    judged = [r for r in rows if kind_of(r) in JUDGED and r["id"] not in settled]
    if judged and (body or "").strip():
        from . import provider
        listing = "\n".join(f"[{r['id']}] {r['title']}" for r in judged)
        try:
            out = provider.think_json(
                "An email was just sent on a deal. Below are things we OWE this client (promises we made, deadlines "
                "they set, next steps). Decide which THIS email genuinely completes: the promised or requested thing is "
                "actually delivered, attached or answered in it, not merely mentioned or promised again. For each, "
                "quote the exact words of the email (at least six words, copied character for character) that "
                'deliver it. Return {"done": [{"id": <int>, "quote": "<exact words>"}]}. Conservative: when '
                "unclear, leave it open.",
                f"OWED:\n{listing}\n\nEMAIL JUST SENT (to {to}, subject {subject}, attachments: {names or 'none'}):\n"
                f"{(body or '')[:3000]}", model=provider.MODEL_ROUTER, purpose="obligation-settle")
            # the model's proof is checked by code: the words it quotes must be in the email (or its file names)
            ids = {int(d.get("id")): d.get("quote") for d in ((out or {}).get("done") or [])
                   if isinstance(d, dict) and str(d.get("id")).isdigit()
                   and quote_in(str(d.get("quote") or ""), f"{body}\n{names}")}
        except Exception:  # noqa: BLE001
            ids = {}
        for r in judged:
            if r["id"] in ids:
                _done(deal_id, r, "delivered in the email that went out",
                      evidence("sent_email", ref=_sent_ref, quote=ids[r["id"]]))
                settled.append(r["id"])
    return settled


def on_reply_sent(deal_id: int) -> None:
    """We emailed them: notices that said a reply was owed on this deal are answered."""
    db.execute("update notifications set state='dismissed' where target_type='deal' and target_id=%s and "
               "state in ('unread','read','snoozed') and (title like 'Unanswered%%' or title like 'Not drafted%%' "
               "or title like '%%replied: no %%')", (str(int(deal_id)),))


def on_stage(deal_id: int, old: str | None, new: str) -> None:
    """The deal moved stage: what the stage itself completes is settled, and notices that asked about the stage
    (move it to Booked? to Lost? signed off?) are cleared, because the owner has answered them."""
    from . import stages
    for r in _open(deal_id):
        k = kind_of(r)
        if k == "proposal_owed" and new != "Opportunity":
            _done(deal_id, r, f"the deal moved to {new}", evidence("stage", stage=new))
        elif k == "tender_close" and (new in stages.WON or new == "Lost"):
            _done(deal_id, r, f"the deal moved to {new}", evidence("stage", stage=new))
    if old in stages.SALES and new not in stages.SALES:      # won, lost or parked: the personal touch track ends
        db.execute("update reminders set status='cancelled' where status in ('pending','snoozed') and "
                   "created_by='cortex-touch' and target_type='deal' and target_id=%s", (str(int(deal_id)),))
    if new != old:
        db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and "
                   "(dedup_key like %s or dedup_key like %s)", (f"closing-gate:{int(deal_id)}:%", f"signed-off:{int(deal_id)}"))


_MEANING_STOP = {"the", "a", "an", "to", "and", "of", "for", "with", "on", "in", "our", "their", "we", "will", "you",
                 "your", "them", "owed", "commitment", "next", "step", "check", "deal", "prepare", "provide", "send"}


def _words(s: str) -> set:
    s = re.sub(r"^(?:Commitment owed to|OWED to|Next step on [^:]+:|Next step:)\s*\S*:?", "", s or "", flags=re.I)
    return {w for w in re.findall(r"[a-z]{3,}", s.lower().split(" (check-in")[0].split(" (deal ")[0]) if w not in _MEANING_STOP}


def _same(a: str, b: str) -> bool:
    """The same piece of work: at least three meaningful words shared, and most of the shorter wording."""
    x, y = _words(a), _words(b)
    n = len(x & y)
    return n >= 3 and n / max(1, min(len(x), len(y))) >= 0.6


def cancel_matching(deal_id: int, words: str, why: str, except_task: int | None = None) -> list[str]:
    """NOT NEEDED CLOSES THE WHOLE CHAIN (owner, 14 Oct 2026: "if we ask them something and they don't request it, we
    shouldn't"). One false 'commitment' had become a reminder, a prep card and a promise in the next reply. When the
    owner says the work is not owed (skips its prep card, or says so on it or on the email card), every Cortex
    reminder for it is cancelled, the timeline entries that asserted it are voided (kept, no longer read back into
    drafts), the open prep cards for it are cancelled, and the decision is logged. Returns what was closed."""
    if not str(deal_id or "").isdigit() or len(_words(words)) < 2:
        return []
    from psycopg.types.json import Json
    did, done = int(deal_id), []
    for r in _open(did):
        if kind_of(r) in JUDGED and _same(r.get("title") or "", words):
            # only the owner's own decision reaches here (he skipped its prep card, or said it is not needed)
            if close(r["id"], "cancelled", evidence("owner", who="owner: not needed", quote=words[:300]), why):
                done.append(f"reminder #{r['id']}")
    p = db.one("select history from crm_projects where id=%s", (did,)) or {}
    hist, n = p.get("history") or [], 0
    for e in hist:
        if e.get("event") in ("commitment", "next_step", "deadline") and not e.get("voided") and _same(e.get("text") or "", words):
            e["voided"] = why
            n += 1
    if n:
        db.execute("update crm_projects set history=%s where id=%s", (Json(hist), did))
        done.append(f"{n} timeline entr{'y' if n == 1 else 'ies'} voided")
    for t in db.query("select id, request from tasks where deal_id=%s and kind='content' and status in "
                      "('new','drafting','awaiting_approval','awaiting_correction') and request->>'prep_action' is not null",
                      (did,)):
        if t["id"] != except_task and _same((t.get("request") or {}).get("title") or "", words):
            rq = dict(t.get("request") or {})
            rq["cancelled_reason"] = why
            db.execute("update tasks set status='cancelled', request=%s, updated_at=now() where id=%s", (Json(rq), t["id"]))
            done.append(f"card #{t['id']}")
    if done:
        from . import pipeline
        pipeline.log_deal(did, "commitment_cancelled", f"Not owed: {words[:160]} ({why}). Closed: {', '.join(done)}.")
    return done


def on_task_closed(task_id: int) -> None:
    """A card left the open states (sent, done, cancelled, rejected): notices about that card have done their job."""
    db.execute("update notifications set state='dismissed' where target_type='task' and target_id=%s and "
               "state in ('unread','read','snoozed')", (str(int(task_id)),))


def on_contact_added(deal_id: int) -> None:
    """A deal got a contact: the 'no contact, follow-ups can't start' notices are answered."""
    db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and "
               "dedup_key in (%s, %s)", (f"nocontact:{int(deal_id)}", f"no-contact:{int(deal_id)}"))


def on_deal_for_contact(email: str) -> None:
    """An opportunity now exists for someone we emailed by hand: the 'untracked sales email' notice is answered."""
    db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and "
               "lower(dedup_key)=%s", (f"untracked-sales:{(email or '').strip().lower()}",))


# ---------------------------------------------------------------------------------------------------------------------
# ONE RECORD OF WHAT IS OWED (owner, 14 Oct 2026: "we can't seem to fix these duplicates ... get the fundamental cause").
# Four writers set reminders for what we owe on a deal: meeting notes (twice per meeting: the summary email and the full
# document), every email we send, the client's stated deadlines, and next steps. Each had its own duplicate check, or
# none, by WORD OVERLAP, so one promise in other words became a twin: Shama #164 carried three reminders for "2 to 3
# creative options", Sampras #60 a deadline and a promise for the same service-charge change. The check now lives in
# the one place every Cortex reminder passes through (reminders.create -> check_new): the new item is compared by
# MEANING with everything still open on the deal, and a match updates that one reminder instead of adding another. The
# morning health check runs merge_twins over every deal as a safety net.
# A PROMISE TO DELIVER WORK GETS ITS LEAD TIME: the reminder fires `delivery_lead_working_days` (profile, default 2)
# working days before the date it is owed, and it only ever reminds: it never drafts the email that "delivers" it.
# ---------------------------------------------------------------------------------------------------------------------
OWED_PREFIXES = ("Commitment owed to", "Meeting commitment", "Next step on", "Client deadline")


def _open_on_deal(deal_id) -> list[dict]:
    return db.query("select id, title, due_at, created_by, deliver_by from reminders where target_type='deal' and "
                    "target_id=%s and status in ('pending','snoozed') and recurrence='none' and "
                    "coalesce(created_by,'') <> 'cortex-touch' order by id", (str(deal_id),))


def lead_days(company_id) -> int:
    try:
        from . import profile
        return max(0, int((profile.get(company_id) or {}).get("delivery_lead_working_days", 2)))
    except Exception:  # noqa: BLE001
        return 2


def working_days_before(dt, n: int):
    from datetime import timedelta
    d = dt
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:          # Monday to Friday
            n -= 1
    return d


def _judge(title: str, due_at, rows: list) -> dict:
    """The one question, asked the same way at creation and in the morning merge."""
    from . import provider
    listing = "\n".join(f"R{r['id']} [{'set by the owner' if not str(r.get('created_by') or '').startswith('cortex') else 'cortex'}] "
                        f"{r['title'][:220]} (due {r['due_at']:%d %b})" for r in rows[:30])
    try:
        out = provider.think_json(
            "A new reminder is about to be set on a sales opportunity. (1) Is it the SAME obligation as one already "
            "open (the same thing owed or due, whatever the wording, e.g. 'send 2 to 3 creative options' = 'deliver "
            "2-3 treatments with mood, script and images')? A different deliverable, a later stage or a different "
            "person is NOT the same. (2) If the same, does the new one state a CHANGED date that should replace the "
            "old one? (3) Is the new item a piece of work WE must produce and deliver (a treatment, proposal, "
            "quotation, deck, script, edit, document), rather than a reply, a call or an update? Return "
            '{"same": "R<id>" or null, "replaces": true|false, "deliverable": true|false}.',
            f"OPEN:\n{listing or '(nothing open)'}\n\nNEW: {title[:300]} (due {due_at:%d %b})",
            model=provider.MODEL_ROUTER, max_tokens=60, purpose="owed-dedup") or {}
    except Exception:  # noqa: BLE001
        return {}
    sid = str(out.get("same") or "").lstrip("Rr")
    same = next((r for r in rows if sid.isdigit() and int(sid) == r["id"]), None)
    return {"same": same, "replaces": bool(out.get("replaces")), "deliverable": bool(out.get("deliverable"))}


def check_new(title: str, due_at, company_id, deal_id) -> dict:
    """{"same": <open reminder row> | None, "replaces": bool, "deliverable": bool}. One narrow model question, only
    when there is something to compare with or the new item is a promise; code keeps only real ids."""
    rows = _open_on_deal(deal_id) if str(deal_id or "").isdigit() else []
    owed = (title or "").startswith(OWED_PREFIXES)
    if not rows and not owed:
        return {}
    v = _judge(title, due_at, rows)
    v["deliverable"] = v.get("deliverable", False) and owed
    return v


def merge_twins(rows: list) -> list:
    """Safety net (invariant duplicate_obligations): the SAME question as at creation (_judge), asked of each open
    reminder against the ones before it on the deal. A twin is cancelled (never deleted) and the kept one takes the
    earlier date; the owner's own reminders are never cancelled."""
    from . import pipeline
    merged = []
    for row in rows or []:
        did = row.get("deal_id")
        kept = []
        for r in _open_on_deal(did):
            same = _judge(r["title"], r["due_at"], kept).get("same") if kept else None
            if same and str(r.get("created_by") or "").startswith("cortex"):
                close(r["id"], "cancelled", evidence("duplicate", kept=same["id"]), f"same thing owed as #{same['id']}")
                if r["due_at"] < same["due_at"]:
                    db.execute("update reminders set due_at=%s where id=%s", (r["due_at"], same["id"]))
                pipeline.log_deal(int(did), "note", f"Reminder #{r['id']} merged into #{same['id']} (same thing owed): "
                                  f"{r['title'][:140]}")
                merged.append(r["id"])
            else:
                kept.append(r)
    return merged
