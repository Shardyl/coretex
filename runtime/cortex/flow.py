"""ONE FLOW PER OPPORTUNITY: the Inbox is reconciled whenever anything happens on a deal (owner, 15 Oct 2026).

"I need the inbox to be intelligent and cleaned up ... when actions happen, if they trigger new actions, the inbox should
be checked for stale reminders ... so that everything is in a nice single flow for an opportunity." About half of what
sat in his Inbox on 15 Oct was stale: notices for reminders already done or cancelled, "task could not be created" for
a hold that was deliberate, a 12 Oct next step on a booked project, a promise that waits on the client, seven separate
call nudges. Each mechanism cleaned up after its OWN event only; nothing looked at the opportunity as a whole.

    mark(deal_id, why)       every event on a deal (email in or out, a card created or closed, a note, a stage change,
                             meeting notes) queues the deal; cheap, any process
    drain()                  the engine's flow thread: each queued deal, once its events have settled for SETTLE_SEC
    reconcile(deal_id)       1. RULES (certain): a notice whose reminder is done or cancelled, whose card closed, on a
                                closed deal, an "Unanswered"/"Not drafted" already answered by our email, anything on a
                                deal a manual person handles -> dismissed / cancelled
                             2. ONE JUDGEMENT (Haiku) over what is still open, against the deal's latest timeline: each
                                item keep / close / move, and the client's posture (engaged, weighing, on hold, quiet,
                                declined). On hold slows the chase to every two weeks; declined asks the owner once.
                             Every change is logged on the deal and the Inbox revision moves, so the cockpit refreshes.

WHAT IT NEVER TOUCHES: the owner's own reminders; a reply to their email waiting for approval (conversation.py keeps
those current); a card he has corrected; quotations and proposals (his work). Nothing is deleted: reminders and cards
are cancelled, notices dismissed, each with the reason on the deal.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone

from . import db

SETTLE_SEC = 15
_OPEN = ("queued", "new", "drafting", "awaiting_approval", "awaiting_correction")
_AUTO_FOLLOW = ("chase", "checkin", "followup", "revive", "nurture")
_SCHEMA = """
create table if not exists flow_queue (
  deal_id bigint primary key,
  due_at timestamptz not null default now(),
  why text
);
"""
_LOCK = threading.Lock()


def ensure_schema() -> None:
    db.execute(_SCHEMA)


def mark(deal_id, why: str = "") -> None:
    """An event happened on this deal: reconcile it once the burst has settled. Fail-soft, any process."""
    if not str(deal_id or "").isdigit():
        return
    try:
        ensure_schema()
        db.execute("insert into flow_queue (deal_id, due_at, why) values (%s, now() + make_interval(secs => %s), %s) "
                   "on conflict (deal_id) do update set due_at = excluded.due_at, why = left(coalesce(flow_queue.why,'') "
                   "|| '; ' || excluded.why, 400)", (int(deal_id), SETTLE_SEC, (why or "")[:120]))
    except Exception as e:  # noqa: BLE001
        print(f"[flow] mark {deal_id}: {type(e).__name__}: {e}", flush=True)


def drain(limit: int = 10) -> list:
    """Reconcile the deals whose events have settled. One drain at a time per process."""
    if not _LOCK.acquire(blocking=False):
        return []
    done = []
    try:
        ensure_schema()
        rows = db.query("delete from flow_queue where deal_id in (select deal_id from flow_queue where due_at <= now() "
                        "order by due_at limit %s) returning deal_id, why", (limit,))
        for r in rows:
            try:
                done.append([r["deal_id"], reconcile(int(r["deal_id"]), why=r.get("why") or "")])
            except Exception as e:  # noqa: BLE001
                print(f"[flow] reconcile {r['deal_id']}: {type(e).__name__}: {e}", flush=True)
    finally:
        _LOCK.release()
    return done


def bump_rev() -> None:
    """The Inbox changed underneath the cockpit: it reloads on its next check."""
    try:
        db.execute("insert into settings (key, value) values ('inbox_rev', '1'::jsonb) on conflict (key) do update "
                   "set value = to_jsonb(coalesce((settings.value)::text::bigint, 0) + 1)")
    except Exception:  # noqa: BLE001
        pass


def rev() -> int:
    try:
        return int(db.setting_get("inbox_rev") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _log(deal_id: int, text: str) -> None:
    from . import pipeline
    pipeline.log_deal(int(deal_id), "note", f"Inbox tidy: {text}"[:600])


def _manual_deal(deal: dict) -> bool:
    try:
        from . import profile, store
        co = store.get_company_by_slug(_slug(deal.get("company")))
        manual = {str(a).lower() for a in (profile.get(co["id"]) or {}).get("manual_people") or []} if co else set()
        return bool(manual) and str(deal.get("owner") or "").lower() in manual
    except Exception:  # noqa: BLE001
        return False


def _slug(org: str | None) -> str:
    from . import crm
    try:
        return crm._slug_for_org(org) or "sensa"
    except Exception:  # noqa: BLE001
        return "sensa"


def _cortex_card(t: dict) -> bool:
    """A card Cortex raised by itself, so Cortex may stand it down: chases, check-ins, revivals, nurture, prep and
    next-step cards, emails spawned from its own reminders. Never a reply to their email, never his own draft,
    never a quotation or proposal, never a card he has corrected."""
    r = t.get("request") or {}
    if t.get("kind") in ("quotation",) or r.get("kind") in ("proposal", "creative_proposal", "signed_quotation",
                                                             "proforma", "deal_pick"):
        return False
    if db.one("select 1 from decisions where task_id=%s and action='correct' and actor <> 'cortex' limit 1", (t["id"],)):
        return False
    if r.get("followup") in _AUTO_FOLLOW or r.get("lead_chase"):
        return True
    if r.get("prep_action") or str(r.get("title") or "").startswith("Next step"):
        return True
    return str(r.get("system_note") or "").startswith(("From reminder #", "From commitment reminder"))


def _open_items(deal_id: int) -> dict:
    did = str(int(deal_id))
    rems = db.query("select id, title, due_at, status, created_by, last_notification_id, deliver_by from reminders "
                    "where target_type='deal' and target_id=%s and status in ('pending','snoozed','fired') and "
                    "coalesce(created_by,'') like 'cortex%%' and coalesce(created_by,'') <> 'cortex-touch' and "
                    "recurrence='none' order by id", (did,))
    notes = db.query("select id, title, body, created_at, dedup_key from notifications where target_type='deal' and "
                     "target_id=%s and state in ('unread','snoozed') and category <> 'digest' order by id", (did,))
    cards = [t for t in db.query("select * from tasks where deal_id=%s and status = any(%s) order by id",
                                 (int(deal_id), list(_OPEN))) if _cortex_card(t)]
    return {"reminders": rems, "notices": notes, "cards": cards}


def _dismiss(nid, deal_id, why: str) -> None:
    db.execute("update notifications set state='dismissed' where id=%s and state in ('unread','read','snoozed')", (nid,))


def _cancel_rem(r: dict, deal_id: int, why: str) -> None:
    db.execute("update reminders set status='cancelled' where id=%s and status in ('pending','snoozed','fired')", (r["id"],))
    if r.get("last_notification_id"):
        _dismiss(r["last_notification_id"], deal_id, why)
    db.execute("update notifications set state='dismissed' where state in ('unread','read','snoozed') and "
               "dedup_key = %s", (f"reminder-fail:{r['id']}",))


def _cancel_card(t: dict, why: str) -> None:
    from . import store
    store.update_task(t["id"], status="cancelled", request={**(t.get("request") or {}), "cancelled_reason": why})


# ---------------------------------------------------------------------------------------------------------------------
def _rules(deal: dict, items: dict) -> list[str]:
    """The certain cases. Returns what was done, in words."""
    from . import stages
    did, done = int(deal["id"]), []
    hist = deal.get("history") or []
    stage = deal.get("stage") or ""
    # a closed deal owes nothing and needs nothing from him
    if stage in ("Lost", "Completed", "Dormant"):
        for r in items["reminders"]:
            _cancel_rem(r, did, f"the opportunity is {stage}")
            done.append(f"cancelled reminder #{r['id']} ({stage})")
        for n in items["notices"]:
            if not n.get("title", "").startswith("Reminder action"):
                _dismiss(n["id"], did, stage)
                done.append(f"dismissed notice #{n['id']} ({stage})")
        for t in items["cards"]:
            _cancel_card(t, f"Inbox tidy: the opportunity is {stage}")
            done.append(f"cancelled card #{t['id']} ({stage})")
        return done
    # notices whose reminder is finished
    for n in db.query("select n.id, r.id rid, r.status from notifications n join reminders r on r.last_notification_id=n.id "
                      "where n.state in ('unread','snoozed') and r.target_type='deal' and r.target_id=%s and "
                      "r.status in ('done','cancelled')", (str(did),)):
        _dismiss(n["id"], did, "its reminder is finished")
        done.append(f"dismissed notice #{n['id']} (reminder #{n['rid']} {n['status']})")
    # a reply owed is answered by our email or by his decision not to pursue it
    last_answer = max((str(e.get("ts")) for e in hist if str(e.get("event", "")).startswith("email_out")
                       or e.get("event") == "not_pursued"), default="")
    for n in items["notices"]:
        t = n.get("title") or ""
        if t.startswith(("Unanswered", "Not drafted")) and last_answer and last_answer > n["created_at"].isoformat():
            _dismiss(n["id"], did, "answered since")
            done.append(f"dismissed notice #{n['id']} (we have written since)")
        elif t.startswith(("Unanswered", "Not drafted")) and _manual_deal(deal):
            _dismiss(n["id"], did, "handled by hand")
            done.append(f"dismissed notice #{n['id']} (handled by hand)")
    # a promise that waits on THEM is not ours to chase (it is logged as context instead)
    from . import pipeline
    for r in items["reminders"]:
        if r["status"] in ("pending", "snoozed", "fired") and pipeline._CONDITIONAL.search(r.get("title") or ""):
            _cancel_rem(r, did, "waits on the client")
            pipeline.log_deal(did, "context", f"Waiting on the client (no reminder): {r['title'][:200]}")
            done.append(f"cancelled reminder #{r['id']} (waits on the client)")
    # a chase raised before a full rhythm step after our last email is too soon (Massar 1262: two days after the last)
    from . import gate
    floor = gate._rhythm_floor(db.one("select id, stage, history, cadence, followup_step, company from crm_projects "
                                      "where id=%s", (did,)) or {}, hist) if stage in stages.SALES else None
    for t in items["cards"]:
        if (t.get("request") or {}).get("followup") in _AUTO_FOLLOW and floor and t["created_at"] < floor - timedelta(hours=6):
            _cancel_card(t, f"Inbox tidy: too soon after our last email; the next chase is due from {floor:%a %d %b}")
            done.append(f"cancelled chase card #{t['id']} (too soon; next chase from {floor:%a %d %b})")
    return done


_JUDGE = (
    "You keep a sales team's Inbox clean. Below is one opportunity: its stage, its latest activity (newest first) and "
    "the items Cortex still has open on it (reminders, notices, cards it raised itself). For EACH item decide:\n"
    "- keep: still needed now, or soon;\n"
    "- close: already done, overtaken by later activity, a duplicate of another item, about something that no longer "
    "applies, or waiting on the CLIENT rather than on us;\n"
    "- move: still right but too early; give `when` as words (e.g. 'next Tuesday', '20 October').\n"
    "Be conservative: close only when the activity clearly shows it. Entries named not_pursued, Inbox tidy or a note "
    "are OUR internal decisions and bookkeeping, never the client's answer: 'not pursued' means we chose not to answer one "
    "particular message, and says nothing about this opportunity. Then read the CLIENT'S POSTURE from their own words "
    "and our exchange: engaged (talking, asking), weighing (has our quote, deciding), on_hold (they said it is paused or "
    "gave a later time), quiet (no answer to our recent emails), declined (the CLIENT wrote that they do not want THIS work), won (they said yes).\n"
    'Return {"posture": "...", "posture_why": "<one line>", "items": [{"id": "<id as given>", "verdict": '
    '"keep|close|move", "why": "<one short line>", "when": "<words, for move only>"}]}.')


def _timeline(hist: list, n: int = 14) -> str:
    skip = ("Inbox tidy", "Automatic follow-up re-armed", "No follow-up drafted", "No chase", "No commitment email")
    out = []
    for e in reversed(hist):
        if e.get("voided"):
            continue
        t = re.sub(r"\s+", " ", str(e.get("text") or ""))
        if e.get("event") == "note" and t.startswith(skip):
            continue
        out.append(f"[{str(e.get('ts') or '')[:16]}] {e.get('event')}: {t[:400]}")
        if len(out) >= n:
            break
    return "\n".join(out)


def _judge(deal: dict, items: dict) -> dict:
    from . import provider
    lines = []
    for r in items["reminders"]:
        lines.append(f"R{r['id']} reminder ({r['status']}, due {r['due_at']:%d %b}): {r['title'][:220]}")
    for n in items["notices"]:
        lines.append(f"N{n['id']} notice ({n['created_at']:%d %b}): {(n['title'] or '')[:160]} {(n.get('body') or '')[:160]}")
    for t in items["cards"]:
        r = t.get("request") or {}
        what = r.get("followup") or r.get("prep_action") or "card"
        lines.append(f"C{t['id']} {what} card ({t['created_at']:%d %b}, {t['status']}): "
                     f"{str(r.get('title') or (r.get('inquiry') or {}).get('subject') or '')[:120]} "
                     f"{(t.get('draft') or r.get('brief') or '')[:300]}")
    if not lines:
        return {}
    now = datetime.now(timezone.utc)
    try:
        return provider.think_json(
            _JUDGE,
            f"TODAY: {now:%a %d %b %Y}\nOPPORTUNITY #{deal['id']}: {deal.get('title')} (stage {deal.get('stage')})\n\n"
            f"LATEST ACTIVITY (newest first):\n{_timeline(deal.get('history') or [])}\n\nOPEN ITEMS:\n" + "\n".join(lines),
            model=provider.MODEL_ROUTER, max_tokens=900, purpose="inbox-tidy") or {}
    except Exception as e:  # noqa: BLE001
        print(f"[flow] judge {deal['id']}: {type(e).__name__}: {e}", flush=True)
        return {}


def _apply(deal: dict, items: dict, out: dict) -> list[str]:
    from . import engine
    did, done = int(deal["id"]), []
    by = {f"R{r['id']}": ("r", r) for r in items["reminders"]}
    by.update({f"N{n['id']}": ("n", n) for n in items["notices"]})
    by.update({f"C{t['id']}": ("c", t) for t in items["cards"]})
    for it in (out.get("items") or []):
        key = str(it.get("id") or "").strip().upper()
        if key not in by:
            continue
        kind, obj = by[key]
        verdict, why = str(it.get("verdict") or "keep"), str(it.get("why") or "")[:200]
        if verdict == "close":
            if kind == "r":
                _cancel_rem(obj, did, why)
            elif kind == "n":
                _dismiss(obj["id"], did, why)
            else:
                _cancel_card(obj, f"Inbox tidy: {why}")
            done.append(f"closed {key}: {why}")
        elif verdict == "move" and kind == "r" and it.get("when"):
            try:
                when = engine._resolve_their_date({"slug": _slug(deal.get("company"))}, str(it["when"]),
                                                  datetime.now(timezone.utc).strftime("%a %d %b %Y"))
            except Exception:  # noqa: BLE001
                when = None
            if when and when > datetime.now(timezone.utc):
                db.execute("update reminders set due_at=%s, status='pending', snooze_until=null where id=%s",
                           (when, obj["id"]))
                if obj.get("last_notification_id"):
                    _dismiss(obj["last_notification_id"], did, "moved")
                done.append(f"moved {key} to {when:%a %d %b}: {why}")
    posture = str(out.get("posture") or "")
    if posture == "on_hold":
        # ON HOLD SLOWS THE CHASE, it never stops it (owner: "we won't let people go ... every two weeks")
        p = db.one("select next_followup, automation from crm_projects where id=%s", (did,)) or {}
        last_out = max((str(e.get("ts")) for e in deal.get("history") or [] if str(e.get("event", "")).startswith("email_out")),
                       default="")
        if p.get("automation") == "auto" and last_out:
            floor = datetime.fromisoformat(last_out.replace("Z", "+00:00"))
            floor = (floor if floor.tzinfo else floor.replace(tzinfo=timezone.utc)) + timedelta(days=14)
            if not p.get("next_followup") or p["next_followup"] < floor:
                db.execute("update crm_projects set next_followup=%s where id=%s", (floor, did))
                done.append(f"client on hold ({out.get('posture_why') or ''}): next chase {floor:%a %d %b}")
    elif posture == "declined":
        from . import notifications
        notifications.notify(f"{deal.get('title')}: they said no, move it to Lost?",
                             str(out.get("posture_why") or "")[:300], priority="normal", category="approval",
                             target_type="deal", target_id=did, dedup_key=f"declined:{did}")
    return done


def reconcile(deal_id: int, why: str = "", judge: bool = True) -> list[str]:
    """One pass over everything open on the opportunity (see the module note)."""
    deal = db.one("select id, title, stage, company, owner, history from crm_projects where id=%s", (int(deal_id),))
    if not deal:
        return []
    done = _rules(deal, _open_items(deal_id))
    items = _open_items(deal_id)
    if judge and (items["reminders"] or items["notices"] or items["cards"]):
        out = _judge(deal, items)
        if out:
            done += _apply(deal, items, out)
    if done:
        _log(deal_id, "; ".join(done)[:560])
        bump_rev()
        print(f"[flow] deal {deal_id} ({why[:60]}): " + "; ".join(done)[:400], flush=True)
    return done


def reconcile_rows(rows: list) -> list:
    """Morning safety net (invariant stale_inbox_items) and the one-off clean-up."""
    out = []
    for r in rows or []:
        try:
            if reconcile(int(r.get("deal_id")), why="morning sweep"):
                out.append(r.get("deal_id"))
        except Exception as e:  # noqa: BLE001
            print(f"[flow] sweep {r.get('deal_id')}: {type(e).__name__}: {e}", flush=True)
    return out


def tidy_orphans() -> list:
    """Notices that point at nothing still live: a 'Reminder ... fired but its task could not be created' whose
    reminder is done or cancelled, or older than a day (a hold, not a failure)."""
    out = []
    for n in db.query("select id, title, created_at, dedup_key from notifications where state in ('unread','snoozed') "
                      "and title like 'Reminder \"%%could not be created%%'"):
        m = re.match(r'Reminder "(.+?)" fired', n["title"] or "")
        r = db.one("select id, status from reminders where title=%s order by id desc limit 1", (m.group(1),)) if m else None
        if (r and r["status"] in ("done", "cancelled")) or n["created_at"] < datetime.now(timezone.utc) - timedelta(days=1):
            _dismiss(n["id"], None, "stale")
            out.append(n["id"])
    if out:
        bump_rev()
    return out


def start_thread() -> None:
    """The engine's flow clock: drain every 10 seconds, beside the main loop."""
    import time

    def _loop() -> None:
        while True:
            try:
                drain()
            except Exception as e:  # noqa: BLE001
                print(f"[flow] drain: {type(e).__name__}: {e}", flush=True)
            time.sleep(10)
    threading.Thread(target=_loop, name="inbox-flow", daemon=True).start()


def preview(deal_id: int) -> dict:
    """What the judgement would decide for this deal, without changing anything (for checking the flow)."""
    deal = db.one("select id, title, stage, company, owner, history from crm_projects where id=%s", (int(deal_id),))
    if not deal:
        return {}
    items = _open_items(deal_id)
    return {"items": {k: [x.get("id") for x in v] for k, v in items.items()}, "judgement": _judge(deal, items)}
