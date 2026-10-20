"""ONE NEXT-STEPS LIST PER OPPORTUNITY: the single record of what happens next, whose move it is, by when, and the
words it came from (owner, 18 Oct 2026).

Why it exists. Before this, an opportunity had no "next step". It had a pile: an owed reminder minted by every meeting
row and by every sent email that restated a promise, an independent chase clock, and an Inbox tidy guessing "probably
done" from silence. Seven live cases in one morning (Shama #164, Heaven #150, Workspace #122, HBMSU #107, Brent #93,
Shamal #157, DSO #148): the real step buried under internal tasks and post-signing work, four copies of "send the
proposal", promises that wait on the CLIENT treated as ours, an offer ("if needed") treated as owed, our own words
quoted back in their reply filed as their deadline for us, a chase armed while WE owed them something, and a notice
"may already be done" on work nobody had started. The owner: "how do we stop this kind of hallucination and
assumptions ... it's just simple, it's not being done, this is the next step."

The rules, all enforced here:
  - ONE LIST, several items. Each item: what, WHOSE MOVE (us | them), kind (email = Cortex can write it; work = a
    proposal, quotation, treatments, a pilot...; theirs), due, the source QUOTE, and for theirs what we do after.
  - IT CHANGES ONLY ON EVIDENCE: the owner's word (cockpit, a note, Talk), an email WE sent that carries the item, the
    client's own new words, a meeting's notes. Every quote is checked by code against the material. Silence never
    closes anything; nothing is ever "probably done".
  - DISMISSED STAYS DISMISSED, DONE STAYS DONE: a later mention never re-adds an item that matches one already on the
    list in any state (the model sees them all; code checks word overlap as the backstop).
  - DO, DON'T REMIND: when an item of OURS falls due, an email item becomes the drafted email (approve to send); a work
    item becomes ONE plain notice "To do: ..." pointing at the opportunity, with the Talk tool that starts it.
  - THE CHASE FOLLOWS THE LIST: while any open item is OURS, no automatic chase goes (gate.check). When every open item
    is theirs, the chase runs on its normal rhythm.
Table `next_steps`; the old owed reminders were migrated into it on 18 Oct 2026 (migrate()).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import db

_GST = timezone(timedelta(hours=4))

_MIGRATE = """
create table if not exists next_steps (
  id bigserial primary key,
  deal_id bigint not null,
  company_id bigint,
  what text not null,
  whose text not null default 'us',
  kind text not null default 'work',
  tool text,
  due_at timestamptz,
  status text not null default 'open',
  source text,
  quote text,
  then_what text,
  card_id bigint,
  notified_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  closed_at timestamptz,
  closed_by text,
  closed_why text,
  closed_quote text,
  log jsonb not null default '[]'::jsonb
);
create index if not exists next_steps_deal on next_steps (deal_id, status);
"""
_READY = False

# Talk tools a WORK item can start, matched by code on its words (first match wins)
TOOLS = [
    (re.compile(r"\btreatment|\bconcept|\bcreative (?:idea|option|route|direction)|\bstoryboard", re.I), "creative_treatments"),
    (re.compile(r"\bpro ?forma|\binvoice", re.I), "issue_proforma"),
    (re.compile(r"\bquot", re.I), "create_quotation"),
    (re.compile(r"\bproposal|\bdeck\b|\bpitch", re.I), "create_proposal"),
]
TOOL_SAY = {"creative_treatments": "Say in Talk: \"build the treatments for this opportunity\"",
            "issue_proforma": "Say in Talk: \"issue the pro forma\"",
            "create_quotation": "Say in Talk: \"build the quotation\"",
            "create_proposal": "Say in Talk: \"build the proposal\""}


def ensure_schema() -> None:
    global _READY
    if _READY:
        return
    with db.connect() as c:
        c.execute(_MIGRATE)
    _READY = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(x) -> str:
    return x.isoformat() if hasattr(x, "isoformat") else str(x or "")


_MEETING_STEP = re.compile(r"^\s*(?:hold|have|attend|join|run|take)\b.{0,40}\b(?:call|meeting|catch-?up|session)\b", re.I)


def tool_for(what: str) -> str | None:
    """The Talk tool that does the step: the FIRST work word in it decides ("Prepare the two quotations ... covering
    concept" is a quotation, not treatments: RAFED DAWAK, 20 Oct 2026)."""
    if _MEETING_STEP.search(what or ""):      # "Hold the call to run through the quotation" builds nothing
        return None
    hits = [(m.start(), t) for rx, t in TOOLS for m in [rx.search(what or "")] if m]
    return min(hits)[1] if hits else None


def items(deal_id: int, include_closed: bool = False) -> list[dict]:
    ensure_schema()
    q = "select * from next_steps where deal_id=%s" + ("" if include_closed else " and status='open'")
    return db.query(q + " order by (whose='us') desc, due_at nulls last, id", (int(deal_id),))


def ours_open(deal_id: int) -> list[dict]:
    ensure_schema()
    return db.query("select * from next_steps where deal_id=%s and status='open' and whose='us' order by due_at nulls last",
                    (int(deal_id),))


def _log(item_id: int, text: str) -> None:
    db.execute("update next_steps set log = log || %s::jsonb, updated_at=now() where id=%s",
               (json.dumps([{"at": _now().isoformat(), "text": text[:300]}]), int(item_id)))


def _deal_log(deal_id: int, text: str) -> None:
    try:
        from . import pipeline
        pipeline.log_deal(int(deal_id), "next_steps", text[:600])
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import flow
        flow.bump_rev()
    except Exception:  # noqa: BLE001
        pass


_STOP = {"the", "a", "an", "to", "and", "of", "for", "with", "on", "in", "our", "their", "we", "will", "you", "your",
         "them", "send", "share", "deal", "client", "by", "from", "this", "that", "they", "it"}


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z]{3,}", (s or "").lower()) if w not in _STOP}


def _same(a: str, b: str) -> bool:
    x, y = _words(a), _words(b)
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.6


def _in(quote: str, text: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()     # noqa: E731
    q = norm(quote)
    return len(q.split()) >= 3 and q in norm(text)


def _due(hint, *, whose: str) -> datetime | None:
    """Code computes the date. An ISO date as stated; a vague hint maps to a window; none = no date for THEIR move,
    three days for ours (something we owe always has a date to answer to)."""
    from . import pipeline
    h = str(hint or "none").strip().lower()
    if h in ("", "none", "null") and whose == "them":
        return None
    return pipeline._commitment_due(h)


# ------------------------------------------------------------------ writing to the list

def add(deal_id: int, what: str, *, whose: str = "us", kind: str | None = None, due_at=None, source: str = "owner",
        quote: str = "", then_what: str = "", company_id=None, by: str = "") -> dict | None:
    """Put one item on the list. An item matching anything already on it (open, done or DISMISSED) is not added."""
    ensure_schema()
    what = re.sub(r"\s+", " ", (what or "").strip())[:300]
    if not what:
        return None
    whose = "them" if whose == "them" else "us"
    for it in items(deal_id, include_closed=True):
        if it["status"] == "superseded" or not _same(it["what"], what):
            continue
        if source == "owner":        # HIS word: an item he asks for again is reopened, never refused
            return edit(it["id"], due_at=due_at, by=by or "owner", reopen=it["status"] != "open")
        if it["status"] != "done" or (it.get("closed_at") and it["closed_at"] > _now() - timedelta(days=30)):
            return None              # already on the list, or dismissed: a later mention never re-adds it
    if not company_id:
        p = db.one("select company from crm_projects where id=%s", (int(deal_id),)) or {}
        from . import crm
        company_id = next((c["id"] for c in db.query("select id, slug from companies where kind='owned'")
                           if crm._org(c["slug"]).lower() == (p.get("company") or "").lower()), None)
    kind = kind or ("theirs" if whose == "them" else "work")
    if whose == "them":
        kind = "theirs"
    elif kind == "email" and tool_for(what):      # a proposal, quotation or treatments is never "just an email"
        kind = "work"
    row = db.execute(
        "insert into next_steps (deal_id, company_id, what, whose, kind, tool, due_at, source, quote, then_what) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
        (int(deal_id), company_id, what, whose, kind, tool_for(what) if kind == "work" else None, due_at, source,
         (quote or "")[:600], (then_what or "")[:300]))
    _deal_log(deal_id, f"Next step added ({'ours' if whose == 'us' else 'theirs'}, from {source}): {what}"
              + (f", due {due_at.astimezone(_GST):%a %d %b}" if due_at else ""))
    return row


def close(item_id: int, status: str, *, by: str, why: str = "", quote: str = "") -> dict | None:
    """Done or dismissed. The proof is who said so (`by`) and, from an email or meeting, the words that show it."""
    ensure_schema()
    status = status if status in ("dismissed", "superseded") else "done"
    r = db.execute("update next_steps set status=%s, closed_at=now(), closed_by=%s, closed_why=%s, closed_quote=%s, "
                   "updated_at=now() where id=%s and status='open' returning *",
                   (status, by[:80], (why or "")[:300], (quote or "")[:600], int(item_id)))
    if r:
        _deal_log(r["deal_id"], f"Next step {status} ({by}): {r['what']}" + (f". \"{quote[:160]}\"" if quote else ""))
        if r.get("notified_at"):
            db.execute("update notifications set state='dismissed' where dedup_key=%s and state in ('unread','read','snoozed')",
                       (f"nextstep:{r['id']}",))
    return r


def edit(item_id: int, *, what: str | None = None, whose: str | None = None, due_at=None, by: str = "owner",
         reopen: bool = False) -> dict | None:
    ensure_schema()
    r = db.one("select * from next_steps where id=%s", (int(item_id),))
    if not r:
        return None
    sets, vals, said = [], [], []
    if what and what.strip() != r["what"]:
        sets += ["what=%s", "tool=%s"]
        vals += [what.strip()[:300], tool_for(what) if (whose or r["whose"]) == "us" else None]
        said.append(f"now \"{what.strip()[:120]}\"")
    if whose in ("us", "them") and whose != r["whose"]:
        sets += ["whose=%s", "kind=%s"]
        vals += [whose, "theirs" if whose == "them" else ("work" if r["kind"] == "theirs" else r["kind"])]
        said.append("ours" if whose == "us" else "theirs")
    if due_at is not None:
        sets += ["due_at=%s", "notified_at=null", "card_id=null"]
        vals += [due_at]
        said.append(f"due {due_at.astimezone(_GST):%a %d %b}")
    if reopen and r["status"] != "open":
        sets += ["status='open'", "closed_at=null", "closed_by=null", "closed_why=null", "closed_quote=null"]
        said.append("reopened")
    if not sets:
        return r
    out = db.execute(f"update next_steps set {', '.join(sets)}, updated_at=now() where id=%s returning *",
                     tuple(vals) + (int(item_id),))
    _log(item_id, f"{by}: " + ", ".join(said))
    _deal_log(r["deal_id"], f"Next step changed by {by}: {r['what'][:120]} -> " + ", ".join(said))
    return out


# ------------------------------------------------------------------ reading what happened

REPLAN_MODEL_DEFAULT = "claude-opus-5"     # owner, 18 Oct 2026: the best model; setting `nextsteps_model` overrides

_PLAN = """You RE-PLAN one sales opportunity for a production company after something has just happened (an email WE
sent, an email or WhatsApp THEY sent, a meeting's notes, or the OWNER's note). You see the whole opportunity: its
timeline, its current NEXT-STEPS LIST, and the new event. Decide what the next steps are NOW and return the changes.
Return JSON only:
{"close": [{"id": <id>, "status": "done" | "superseded" | "dismissed", "quote": "<exact words, copied character for
             character from the NEW EVENT or the TIMELINE, that show it>"}],
 "add": [{"what": "<short, concrete, plain words>", "whose": "us" | "them", "kind": "email" | "work" | "theirs",
          "due_hint": "<ISO date ONLY if stated; else today | tomorrow | week | days:N | none>",
          "quote": "<exact words from the NEW EVENT or the TIMELINE it comes from>",
          "then": "<for THEIR item: what we do once they have, or ''>"}],
 "due": [{"id": <id>, "due_hint": "<as above>", "quote": "<exact words>"}],
 "why": "<one sentence: the state of the opportunity now>"}
HOW TO DECIDE, in order:
1. WHOSE MOVE IS IT NOW? Read the newest exchange. If our latest email asked them something, offered a meeting or call,
   or sent them something for their decision, the next move is THEIRS: one THEIRS item ("confirm a time for the call to
   discuss requirements", "decide on the proposal"), with the work we will do after it in "then". Our follow-up emails
   are sent by the follow-up system: never make a chase, a follow-up or a reminder an item.
2. OURS only for what we must do now that does not wait on them: something we promised to send, something they asked
   for that we have everything to produce, a call we agreed to arrange. kind "email" when the whole step is an email
   Cortex can write; kind "work" when it needs real work (a proposal, quotation, treatments, deck, pilot, edit).
   A proposal or quotation is OURS only when we have the scope to write it: if we are still asking what they need, the
   step is to get the requirements (theirs to answer), and the proposal goes in "then".
3. The plan CHANGED? Close the overtaken item as "superseded" (with the words that changed it) and add the right one.
   "done" only when the item actually happened. "dismissed" only on the OWNER's note saying it is not needed.
4. Several items at once are normal (ours: the proposal; theirs: confirm the meeting). Keep every item that still stands.
5. NEVER: an OFFER ("happy to", "if needed", "we could"); politeness; internal tasks (a team brainstorm); work that only
   happens after they sign while the deal is still being sold; anything matching a DISMISSED item; an item marked
   [owner] unless the event is the OWNER's note.
6. Only what the material SAYS. Silence closes nothing, nothing is "probably done", nothing is assumed. Every change
   carries its exact quote. When nothing changes, return empty lists.
7. Plain words in "what": "Confirm a time for the call to discuss the filming requirements", not a title."""

_LOCKS: dict = {}


def _deal_lock(deal_id: int):
    import threading
    return _LOCKS.setdefault(int(deal_id), threading.Lock())


def _timeline(deal_id: int, limit: int = 40) -> str:
    d = db.one("select history from crm_projects where id=%s", (int(deal_id),)) or {}
    hist = [h for h in (d.get("history") or []) if not h.get("voided")
            and h.get("event") not in ("next_steps", "touch_started", "research", "cast")]
    return "\n".join(f"- {str(h.get('ts', ''))[:16]} [{h.get('event', '')}] {str(h.get('text', ''))[:1500]}"
                     for h in hist[-limit:])


def observe(deal_id: int, source: str, material: str, *, by: str = "", company_id=None, meeting: bool = False) -> list[str]:
    """RE-PLAN THE WHOLE OPPORTUNITY after one event (owner, 18 Oct 2026: "when I've sent and approved that email, should
    Cortex not read it and then look at the entire opportunity, clean up any inappropriate next steps and create the
    correct next step"). source: email_out | email_in | whatsapp_in | meeting | note. Serialised per deal; never raises.
    Returns the changes made."""
    with _deal_lock(deal_id):
        return _replan(deal_id, source, material, by=by, company_id=company_id)


def _replan(deal_id: int, source: str, material: str, *, by: str = "", company_id=None) -> list[str]:
    try:
        ensure_schema()
        material = (material or "").strip()
        if len(material) < 12:
            return []
        d = db.one("select id, title, stage, company from crm_projects where id=%s", (int(deal_id),))
        if not d or d["stage"] in ("Lost", "Completed"):
            return []
        from . import provider, worker
        allit = [i for i in items(deal_id, include_closed=True) if i["status"] != "superseded"]
        listing = "\n".join(
            f"#{i['id']} [{i['status']}{', owner' if i.get('source') == 'owner' else ''}] "
            f"{'OURS' if i['whose'] == 'us' else 'THEIRS'}: {i['what']}"
            + (f" (due {i['due_at'].astimezone(_GST):%a %d %b})" if i.get("due_at") else "")
            + (f"; then {i['then_what']}" if i.get("then_what") else "")
            for i in allit[-40:]) or "(empty)"
        timeline = _timeline(deal_id)
        label = {"email_out": "AN EMAIL WE SENT", "email_in": "AN EMAIL THEY SENT (their new words only)",
                 "whatsapp_in": "A WHATSAPP THEY SENT", "meeting": "THE MEETING'S NOTES",
                 "note": "THE OWNER'S OWN NOTE", "review": "A FULL REVIEW"}.get(source, source.upper())
        model = str(db.setting_get("nextsteps_model") or REPLAN_MODEL_DEFAULT)
        out = provider.think_json(
            _PLAN,
            f"{worker._now_line()}\nOPPORTUNITY #{d['id']}: {d['title']} (stage {d['stage']})\n\nTIMELINE (oldest first):\n"
            f"{timeline or '(none)'}\n\nCURRENT NEXT-STEPS LIST:\n{listing}\n\nTHE NEW EVENT, {label}"
            f"{(' (from ' + by + ')') if by else ''}:\n{material[:12000]}",
            fast=False, model=model, max_tokens=8000, purpose="next-steps-replan") or {}
    except Exception as e:  # noqa: BLE001
        print(f"[nextsteps] replan {deal_id}: {type(e).__name__}: {e}", flush=True)
        return []
    done = []
    corpus = material + "\n" + timeline          # every quote must be in what the model was shown, checked by code
    try:
        byid = {i["id"]: i for i in allit}
        who = by or {"email_out": "our email", "email_in": "their email", "whatsapp_in": "their WhatsApp",
                     "meeting": "the meeting", "note": "owner"}.get(source, source)
        for c in out.get("close") or []:
            it = byid.get(int(c.get("id") or 0)) if str(c.get("id") or "").isdigit() else None
            if not it or it["status"] != "open":
                continue
            q = str(c.get("quote") or "")
            st = str(c.get("status") or "done")
            st = st if st in ("done", "superseded", "dismissed") else "done"
            if source != "note":
                if it.get("source") == "owner" or st == "dismissed" or not _in(q, corpus):
                    continue           # his items are his; only his note dismisses; no proof, no change
            if close(it["id"], st, by=who, why=f"re-plan after {source}", quote=q if source != "note" else ""):
                done.append(f"{st}: {it['what']}")
        from . import pipeline
        for a in out.get("add") or []:
            what, q = str(a.get("what") or "").strip(), str(a.get("quote") or "")
            if not what or (source != "note" and not _in(q, corpus)):
                continue
            whose = "them" if a.get("whose") == "them" else "us"
            if whose == "us" and source != "note" and pipeline._is_offer(q):
                continue
            kind = "theirs" if whose == "them" else ("email" if a.get("kind") == "email" else "work")
            r = add(deal_id, what, whose=whose, kind=kind, due_at=_due(a.get("due_hint"), whose=whose), source=source,
                    quote=q, then_what=str(a.get("then") or ""), company_id=company_id, by=who)
            if r:
                done.append(f"added ({'ours' if whose == 'us' else 'theirs'}): {what}")
        for u in out.get("due") or []:
            it = byid.get(int(u.get("id") or 0)) if str(u.get("id") or "").isdigit() else None
            if not it or it["status"] != "open" or (source != "note" and (it.get("source") == "owner"
                                                                          or not _in(str(u.get("quote") or ""), corpus))):
                continue
            w = _due(u.get("due_hint"), whose=it["whose"])
            if w:
                edit(it["id"], due_at=w, by=who)
                done.append(f"new date {w.astimezone(_GST):%a %d %b}: {it['what']}")
        if done:
            _deal_log(deal_id, f"Next steps re-planned after {source}: " + "; ".join(done)[:500]
                      + (f". {str(out.get('why') or '')[:200]}" if out.get("why") else ""))
    except Exception as e:  # noqa: BLE001
        print(f"[nextsteps] apply {deal_id}: {type(e).__name__}: {e}", flush=True)
    return done


def observe_async(*a, **k) -> None:
    import threading
    threading.Thread(target=observe, args=a, kwargs=k, daemon=True).start()


def replan_all(stages: tuple = ("Opportunity", "Quote"), apply: bool = True) -> list:
    """One pass over every open opportunity: re-planned from its whole timeline (the newest event as the trigger)."""
    out = []
    for d in db.query("select id, title from crm_projects where stage = any(%s) order by id", (list(stages),)):
        tl = _timeline(d["id"], limit=6)
        if not tl:
            continue
        ch = observe(d["id"], "review", "A FULL REVIEW, no new event: re-plan from the whole timeline. The newest "
                     "entries:\n" + tl) if apply else []
        out.append({"deal": d["id"], "title": d["title"], "changes": ch})
    return out


def on_closed(deal_id: int, stage: str) -> None:
    """A deal that is Lost, Dormant or Completed owes nothing and expects nothing."""
    ensure_schema()
    for it in items(deal_id):
        close(it["id"], "dismissed", by=f"deal {stage}", why=f"the opportunity moved to {stage}")


# ------------------------------------------------------------------ when something of ours falls due

TODO_AHEAD_DAYS = 7      # OUR work shows in the Inbox from a week before it is due, and stays until Done / Not needed


def tick() -> list[str]:
    """Engine loop. THE INBOX SHOWS WHAT WE OWE (owner, 18 Oct 2026: "where's all the stuff on the inbox that should
    still be there"): every open item of OURS that is overdue or due within a week is ONE "To do" card in the Inbox,
    with Done / Not needed on it, until it is closed. An EMAIL item that falls due is drafted instead (approve to send);
    one already more than a day late is a To do card (a late email is worth a look before it is written)."""
    ensure_schema()
    out = []
    try:
        out += their_dates()
    except Exception as e:  # noqa: BLE001
        print(f"[nextsteps] their_dates: {type(e).__name__}: {e}", flush=True)
    now = _now()
    for it in db.query("select n.*, p.title deal_title, p.stage from next_steps n join crm_projects p on p.id=n.deal_id "
                       "where n.status='open' and n.whose='us' and n.card_id is null and n.notified_at is null "
                       "and p.stage not in ('Lost','Dormant','Completed') order by n.due_at nulls first limit 40"):
        try:
            due = it.get("due_at")
            if due and due > now + timedelta(days=TODO_AHEAD_DAYS):
                continue
            if (it["kind"] == "email" and due and due <= now and due > now - timedelta(days=1)
                    and it.get("source") != "migration" and _draft_email(it)):
                out.append(f"drafted: {it['what']}")
                continue
            if it["kind"] == "email" and due and due > now:
                continue                       # an email waits for its day, then it is drafted
            _todo_notice(it)
            out.append(f"to do: {it['what']}")
        except Exception as e:  # noqa: BLE001
            print(f"[nextsteps] tick {it['id']}: {type(e).__name__}: {e}", flush=True)
    return out


def their_dates() -> list[str]:
    """THEIR DATE WINS (owner, 19 Oct 2026, DSO: "feedback by 19 October", chase set for the 22nd). When something we
    are waiting on from them passes its date and nothing has come, the chase goes the next working day at 09:00 Dubai
    (or now, when that has passed too), never later. Each item pulls the clock once."""
    out = []
    for it in db.query("select n.id, n.deal_id, n.what, n.due_at, p.next_followup, p.automation, p.stage from next_steps n "
                       "join crm_projects p on p.id=n.deal_id where n.status='open' and n.whose='them' and n.due_at is "
                       "not null and n.due_at < now() and n.notified_at is null and p.automation='auto' and "
                       "p.stage in ('Opportunity','Quote')"):
        d = it["due_at"].astimezone(_GST)
        nxt = (d + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        nxt = max(nxt, _now() + timedelta(minutes=2))
        if not it["next_followup"] or it["next_followup"] > nxt:
            db.execute("update crm_projects set next_followup=%s, updated_at=now() where id=%s", (nxt, it["deal_id"]))
            _deal_log(it["deal_id"], f"Their date passed ({d:%a %d %b}) with nothing from them: the chase moves to "
                                     f"{nxt:%a %d %b %H:%M} ({it['what'][:100]}).")
            out.append(f"#{it['deal_id']} chase {nxt:%a %d %b}")
        db.execute("update next_steps set notified_at=now() where id=%s", (it["id"],))
    return out


def _todo_notice(it: dict) -> None:
    from . import notifications
    say = TOOL_SAY.get(it.get("tool") or "")
    due, now = it.get("due_at"), _now()
    late = (now - due).days if due and due < now else 0
    when = (f"{late} day{'s' if late != 1 else ''} overdue (due {due.astimezone(_GST):%a %d %b})" if late
            else (f"Due {due.astimezone(_GST):%a %d %b}" if due else "No date"))
    notifications.notify(
        f"To do: {it['what']}"[:150],
        f"{it['deal_title']}. {when}." + (f" {say}." if say else ""),
        priority="normal", category="todo", company_id=it.get("company_id"),
        target_type="deal", target_id=it["deal_id"], dedup_key=f"nextstep:{it['id']}")
    db.execute("update next_steps set notified_at=now() where id=%s", (it["id"],))


# Talk tools that do an item's work, by the item's tool (owner, 18 Oct 2026: "tap to talk ... do the actual job")
_TOOL_DOES = {"creative_treatments": "creative_treatments", "create_creative_proposal": "creative_treatments",
              "creative_quote": "create_quotation", "create_quotation": "create_quotation",
              "reissue_quotation": "create_quotation", "create_proposal": "create_proposal",
              "issue_proforma": "issue_proforma"}


def started_from_tool(name: str, inp: dict, out) -> None:
    """A Talk tool has started an item's work: the item and its To do card say it is in progress, with the card. The
    item still closes only on evidence (the work sent), through the re-plan. Never raises."""
    try:
        tool = _TOOL_DOES.get(name)
        did = (inp or {}).get("deal_id")
        if not tool or not str(did or "").isdigit() or not isinstance(out, str):
            return
        cards = re.findall(r"card #(\d{3,7})", out)
        if not cards or re.match(r"\s*(NOT|Quotation NOT|unknown|no |There is no|Which)", out, re.I):
            return
        ensure_schema()
        for it in db.query("select * from next_steps where deal_id=%s and status='open' and whose='us' and tool=%s",
                           (int(did), tool)):
            db.execute("update next_steps set card_id=%s, notified_at=coalesce(notified_at, now()) where id=%s",
                       (int(cards[0]), it["id"]))
            _log(it["id"], f"in progress: {name} started card #{cards[0]}")
            db.execute("update notifications set body = %s || body where dedup_key=%s and state in ('unread','read','snoozed') "
                       "and body not like 'In progress%%'", (f"In progress: card #{cards[0]}. ", f"nextstep:{it['id']}"))
    except Exception as e:  # noqa: BLE001
        print(f"[nextsteps] started_from_tool {name}: {type(e).__name__}: {e}", flush=True)


def _draft_email(it: dict) -> bool:
    """The email that does this step, to the opportunity's lead, as an ordinary card for approval."""
    from . import reminders, store
    co = store.get_company(it["company_id"]) if it.get("company_id") else None
    if not co:
        return False
    sk = store.get_skill_by_key(co["id"], "sales-followup") or store.get_skill_by_key(co["id"], "email-handling")
    if not sk:
        return False
    lead = ""
    try:
        from . import cast
        lead = cast.lead(it["deal_id"]) or ""
    except Exception:  # noqa: BLE001
        lead = ""
    req = {"brief": (f"NEXT STEP ON THIS OPPORTUNITY, now due: {it['what']}. Write the email that does exactly this "
                     "(a call: propose specific times from the availability list; a document we hold or an answer: give "
                     "it). Do not re-promise it, do not apologise at length, do not add a chase."
                     + (f" It comes from: \"{it['quote'][:300]}\"" if it.get("quote") else "")),
           "next_step": it["id"], "system_note": f"From next step #{it['id']}.",
           "inquiry": {"name": "", "email": lead, "subject": ""} if lead else {}}
    req = reminders._with_deal({"target_type": "deal", "target_id": it["deal_id"], "title": it["what"]}, "email_reply", req)
    if "@" not in ((req.get("inquiry") or {}).get("email") or ""):
        return False
    # ONE EMAIL PER PERSON (owner, 18 Oct 2026): an email already open to them carries this step too, never a second card
    to = req["inquiry"]["email"].lower()
    open_card = db.one("select id, request from tasks where company_id=%s and kind in ('email_reply','email_draft') and "
                       "status in ('new','drafting','queued','awaiting_approval','awaiting_correction') and "
                       "lower(request->'inquiry'->>'email')=%s order by id desc limit 1", (co["id"], to))
    if open_card:
        from . import engine
        r2 = dict(open_card.get("request") or {})
        r2["brief"] = (r2.get("brief") or "").rstrip() + f"\n\nALSO COVER IN THIS EMAIL (a next step now due): {it['what']}."
        store.update_task(open_card["id"], request=r2)
        try:
            engine.refresh_from_notes(open_card["id"], f"A next step is now due and belongs in this email: {it['what']}")
        except Exception as _e:  # noqa: BLE001
            print(f"[nextsteps] fold into #{open_card['id']}: {type(_e).__name__}: {_e}", flush=True)
        db.execute("update next_steps set card_id=%s, notified_at=now() where id=%s", (open_card["id"], it["id"]))
        _log(it["id"], f"due: folded into open email card #{open_card['id']}")
        return True
    t = store.create_card(co["id"], sk["id"], "email_reply", req, contact=req["inquiry"]["email"], deal_id=it["deal_id"])
    if not t:
        return False
    db.execute("update next_steps set card_id=%s, notified_at=now() where id=%s", (t["id"], it["id"]))
    _log(it["id"], f"due: drafted as card #{t['id']}")
    return True


# ------------------------------------------------------------------ for the screens and Talk

def when_from_words(words: str, deal_id: int | None = None) -> datetime | None:
    """His words for a date ('Monday', 'after Eid', '1 Nov') -> a datetime, by code."""
    if not (words or "").strip():
        return None
    from . import reminders
    try:
        w = reminders.parse_when(words)
    except Exception:  # noqa: BLE001
        w = None
    if not w:
        try:
            from . import engine
            w = engine._resolve_their_date({"slug": "sensa"}, words, _now().strftime("%a %d %b %Y"))
        except Exception:  # noqa: BLE001
            w = None
    return w


def owner_action(deal_id: int, action: str, *, item_id=None, what: str = "", whose: str = "", when: str = "",
                 by: str = "owner") -> dict:
    """The cockpit's and Talk's one door: add | done | dismiss | reopen | edit. His word is the proof."""
    ensure_schema()
    action = (action or "").lower()
    due = when_from_words(when, deal_id) if when else None
    if when and not due:
        return {"ok": False, "error": f"could not read a date from '{when}'"}
    if action == "add":
        if not what.strip():
            return {"ok": False, "error": "say what the step is"}
        r = add(deal_id, what, whose="them" if whose == "them" else "us",
                kind="email" if re.search(r"\b(email|reply|send (?:them|her|him) )", what, re.I) and whose != "them"
                else None, due_at=due or (None if whose == "them" else _now() + timedelta(days=1)),
                source="owner", by=by)
        return {"ok": True, "item": r}
    it = db.one("select * from next_steps where id=%s and deal_id=%s", (int(item_id or 0), int(deal_id)))
    if not it:
        return {"ok": False, "error": "that step is not on this opportunity"}
    if action in ("done", "dismiss", "dismissed"):
        return {"ok": True, "item": close(it["id"], "done" if action == "done" else "dismissed", by=by,
                                          why="the owner said so")}
    return {"ok": True, "item": edit(it["id"], what=what or None, whose=whose or None, due_at=due, by=by,
                                     reopen=action == "reopen")}


def view(deal_id: int) -> list[dict]:
    now = _now()
    out = []
    for it in items(deal_id):
        due = it.get("due_at")
        out.append({"id": it["id"], "what": it["what"], "whose": it["whose"], "kind": it["kind"], "tool": it.get("tool"),
                    "say": TOOL_SAY.get(it.get("tool") or ""), "quote": it.get("quote") or "", "then": it.get("then_what") or "",
                    "source": it.get("source") or "", "card": it.get("card_id"),
                    "due": due.isoformat() if due else None,
                    "date": due.astimezone(_GST).strftime("%a %d %b") if due else "",
                    "overdue_days": max(0, (now - due).days) if due and due < now and it["whose"] == "us" else 0})
    return out


def describe(deal_id: int) -> str:
    """One line per open item, for Talk and the drafters."""
    lines = []
    for v in view(deal_id):
        lines.append(f"#{v['id']} {'OURS' if v['whose'] == 'us' else 'THEIRS'}: {v['what']}"
                     + (f" (due {v['date']}{', ' + str(v['overdue_days']) + ' days overdue' if v['overdue_days'] else ''})"
                        if v["date"] else "") + (f"; then we {v['then']}" if v["then"] else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------ the one-off move from owed reminders

_MIGRATE_SYS = """You turn ONE opportunity's pile of owed reminders into its NEXT-STEPS LIST. The pile is full of
duplicates (the same promise minted by several emails and meetings), internal tasks, post-signing work, offers, things
already done, and promises that really wait on the CLIENT. Read the reminders and the deal timeline and return JSON:
{"items": [{"what": "<plain words>", "whose": "us" | "them", "kind": "email" | "work" | "theirs",
            "due": "<YYYY-MM-DD from a reminder or the timeline, or ''>", "then": "<for theirs: what we do after>",
            "from": [<reminder ids it replaces>]}],
 "drop": [{"id": <reminder id>, "why": "<done in the timeline / internal / post-signing / an offer / a duplicate>"}]}
Every reminder id appears in exactly one item's "from" or in "drop". Merge duplicates into ONE item.
THEIRS: what waits on the client (data to send, a cast size to confirm, a signed quotation to return, feedback by a
date), with what we do after in "then".
DROP, with the reason:
- done: the timeline shows it happened (sent, presented in a meeting that took place, answered);
- internal: our own team task the client never sees (a brainstorm, an internal review);
- post-signing: production work while the deal is still being sold (Opportunity or Quote stage): "finalize all visual
  assets", "have the final content ready", "begin production". It only starts once they sign, which is THEIR move;
- offer: we only offered it ("if needed", "if you would like", "happy to");
- a chase of something already sent ("follow up on the questions already sent") is not ours: drop it.
Never drop real owed work because it is old or overdue: it stays, with its date.
kind: "work" whenever it needs real work first (a proposal, quotation, treatments or concepts, a deck, a pilot, an
edit, a shoot); "email" only when the whole step is an email we can write now. Plain words in "what"."""


def dismiss_record(deal_id: int, title: str, why: str) -> None:
    """A dropped owed item recorded as DISMISSED (never shown, never raised), so a re-mention is matched and ignored."""
    what = re.sub(r"^(Commitment owed to \S+:|Meeting commitment:|Next step on [^:]+:|Client deadline \S+:)\s*", "",
                  title or "").split(" (deal ")[0].strip()[:300]
    if what:
        db.execute("insert into next_steps (deal_id, what, whose, kind, status, source, quote, closed_at, closed_by, "
                   "closed_why) values (%s,%s,'us','work','dismissed','migration',%s,now(),'migration',%s)",
                   (int(deal_id), what, (title or "")[:600], (why or "")[:300]))


def migrate(apply: bool = False, deal_ids: list | None = None) -> list[dict]:
    """Move every open owed reminder (Cortex-created) into the next-steps list, deal by deal. Dry run by default."""
    from . import obligations, pipeline, provider, worker
    ensure_schema()
    obligations.ensure_guard()
    rows = db.query("select r.*, r.target_id::bigint deal_id from reminders r where r.owed and r.status in "
                    "('pending','snoozed','fired') and r.target_type='deal' and coalesce(r.created_by,'') like 'cortex%%' "
                    "order by r.target_id, r.id")
    by: dict = {}
    for r in rows:
        if deal_ids and int(r["deal_id"]) not in [int(x) for x in deal_ids]:
            continue
        by.setdefault(int(r["deal_id"]), []).append(r)
    report = []
    for did, rs in by.items():
        d = db.one("select id, title, stage from crm_projects where id=%s", (did,)) or {}
        pile = "\n".join(f"R{r['id']} (due {r['due_at']:%Y-%m-%d}, {r['status']}): {r['title']}" for r in rs)
        try:
            out = provider.think_json(_MIGRATE_SYS, f"{worker._now_line()}\nOPPORTUNITY #{did}: {d.get('title')} "
                                      f"(stage {d.get('stage')})\n\nOWED REMINDERS:\n{pile}\n\nTIMELINE:\n"
                                      + pipeline.deal_context(did, limit=40)[:14000],
                                      fast=True, max_tokens=3000, purpose="next-steps-migrate") or {}
        except Exception as e:  # noqa: BLE001
            report.append({"deal": did, "error": f"{type(e).__name__}: {e}"})
            continue
        ids = {r["id"] for r in rs}
        num = lambda x: int(str(x).strip().lstrip("Rr")) if str(x).strip().lstrip("Rr").isdigit() else None   # noqa: E731
        covered = {num(x) for it in out.get("items") or [] for x in (it.get("from") or [])}
        covered |= {num(x.get("id")) for x in out.get("drop") or []}
        rep = {"deal": did, "title": d.get("title"), "items": out.get("items") or [], "drop": out.get("drop") or [],
               "uncovered": sorted(ids - covered)}
        report.append(rep)
        if not apply or rep["uncovered"]:
            continue
        for it in rep["items"]:
            due = None
            if it.get("due"):
                try:
                    due = datetime.fromisoformat(str(it["due"])[:10]).replace(hour=6, tzinfo=timezone.utc)
                except ValueError:
                    due = None
            if it.get("whose") != "them" and not due:
                due = _now() + timedelta(days=1)
            _new = add(did, it.get("what") or "", whose="them" if it.get("whose") == "them" else "us",
                kind=it.get("kind"), due_at=due, source="migration", then_what=it.get("then") or "",
                quote="from reminders " + ", ".join(f"R{str(x).lstrip('R')}" for x in it.get("from") or []))
            if _new and due and due < _now():      # moved, not new: an overdue item shows as overdue, no burst of drafts
                db.execute("update next_steps set notified_at=now() where id=%s", (_new["id"],))
        for x in rep["drop"]:         # kept as DISMISSED, so a later mention never brings it back
            rr = next((r for r in rs if r["id"] == num(x.get("id"))), None)
            if rr:
                dismiss_record(did, rr["title"], f"moved: {x.get('why') or 'dropped'}")
        for r in rs:
            obligations.close(r["id"], "cancelled", obligations.evidence("rule", ref=f"next-steps list, deal {did}"),
                              "moved to the opportunity's next-steps list (18 Oct 2026)")
            if r.get("last_notification_id"):
                db.execute("update notifications set state='dismissed' where id=%s and state in ('unread','read','snoozed')",
                           (r["last_notification_id"],))
        _deal_log(did, f"Owed reminders R{', R'.join(str(r['id']) for r in rs)} moved to the next-steps list.")
    return report
