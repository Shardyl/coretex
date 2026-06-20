"""Universal content scheduling + queue core.

ONE queue/slotting/bump/rolling-refill engine that every schedulable content type plugs into by
registering a `kind` in KINDS. Blog and newsletter register today; social posts (or anything else we
schedule) register one line and inherit the whole machinery for free — per-company monthly slotting on the
company's publishing day, stacking one per month, bump-to-front (push one up, everyone else slides back a
month), and the rolling-N refill reminder (when the queue drops to the threshold, nudge to ideate more).

This is deliberately content-agnostic: the only per-type knowledge here is the KINDS table (display nouns +
the skill key). The actual drafting/publishing of each kind stays in its own handler (engine
`_run_blog_scheduled_task` / `_run_newsletter_scheduled_task` / future social). See
[[project_cortex_roadmap]] (content publishing & calendar) and [[project_cortex_cockpit]] (the Calendar).
"""
from __future__ import annotations

from datetime import datetime

from . import db, notifications, profile, schedule

# The registry every scheduled-content type joins. `kind` = the tasks.kind for a scheduled item of this type.
KINDS: dict[str, dict] = {
    "blog_scheduled":       {"noun": "blog post",  "plural": "blog posts",  "skill": "content-blog-posts"},
    "newsletter_scheduled": {"noun": "newsletter",  "plural": "newsletters", "skill": "content-newsletter"},
    # future: "social_scheduled": {"noun": "social post", "plural": "social posts", "skill": "social-..."},
}

# Rolling-queue policy (universal defaults; each overridable live via db settings without a deploy).
_TARGET = 6      # keep this many queued ahead
_REFILL_AT = 3   # remind when the queue drops to / below this
_BATCH = 3       # ideate this many at a time


def _cfg() -> tuple[int, int, int]:
    g = db.setting_get
    return (int(g("queue_target") or _TARGET), int(g("queue_refill_at") or _REFILL_AT),
            int(g("queue_batch") or _BATCH))


def publish_day(company_id: int) -> int:
    """The company's fixed monthly publishing day (1-28), from company_profiles.data; default 1. The SAME day
    drives every content type for that company, so the per-company stagger applies to blog, newsletter, and
    anything else we schedule."""
    try:
        d = int((profile.get(company_id) or {}).get("publish_day") or 1)
    except Exception:  # noqa: BLE001
        d = 1
    return min(max(d, 1), 28)


def next_slot(company_id: int, kind: str, hour: int = 9) -> datetime:
    """Next free monthly slot for `kind` on the company's publishing day; stacks one item per month (if one is
    already queued, take the company's day in the month after the latest)."""
    now = datetime.now(schedule._GST)
    day = publish_day(company_id)
    row = db.one("select run_at from tasks where company_id=%s and kind=%s and schedule_kind='once' "
                 "and status='scheduled' and run_at is not null order by run_at desc limit 1", (company_id, kind))
    if row and row["run_at"] and row["run_at"] > now:
        return schedule.next_month_day(row["run_at"], day, hour)
    return schedule.day_of_month(now, day, hour)


def depth(company_id: int, kind: str) -> int:
    """How many items of `kind` are queued (scheduled, not yet published) for this company."""
    r = db.one("select count(*) n from tasks where company_id=%s and kind=%s and status='scheduled'",
               (company_id, kind))
    return int(r["n"]) if r else 0


def bump_to_front(task_id: int) -> dict:
    """Move a queued item to the FRONT of its company's queue for ITS kind: it takes the next publishing day,
    and every other queued item of the same kind slides back one month (in date order). Works for any
    registered content kind, so blog and newsletter (and future social) all bump through this one path."""
    t = db.one("select id, company_id, kind from tasks where id=%s", (task_id,))
    if not t or t.get("kind") not in KINDS:
        return {"ok": False, "error": "not a queued content task"}
    cid, kind = t["company_id"], t["kind"]
    day = publish_day(cid)
    front = schedule.day_of_month(datetime.now(schedule._GST), day)
    others = db.query("select id from tasks where company_id=%s and kind=%s and status='scheduled' "
                      "and id<>%s and run_at is not null order by run_at", (cid, kind, task_id))
    db.execute("update tasks set run_at=%s where id=%s", (front, task_id))
    prev = front
    for o in others:
        prev = schedule.next_month_day(prev, day)
        db.execute("update tasks set run_at=%s where id=%s", (prev, o["id"]))
    return {"ok": True, "front": front.strftime("%-d %b %Y"), "shifted": len(others), "kind": kind}


def check_refills() -> list[dict]:
    """Rolling-N refill. Once a day, for every company x content kind that is ALREADY running a program, if the
    queue has dropped to the threshold, fire ONE reminder to ideate the next batch. Guarded to at most one
    nudge per queue per calendar month, so it never nags daily. Returns the reminders fired (for logging)."""
    today = datetime.now(schedule._GST).strftime("%Y-%m-%d")
    if db.setting_get("refill_check_day") == today:   # daily gate (engine loop calls this every tick)
        return []
    db.setting_set("refill_check_day", today)
    target, refill_at, batch = _cfg()
    month = datetime.now(schedule._GST).strftime("%Y-%m")
    fired: list[dict] = []
    for co in db.query("select id, name from companies order by id"):
        cid = co["id"]
        for kind, meta in KINDS.items():
            # only nudge programs that are actually running (have ever produced this kind); never nag a
            # company that hasn't started publishing this content type.
            if not db.one("select 1 from tasks where company_id=%s and kind=%s limit 1", (cid, kind)):
                continue
            d = depth(cid, kind)
            if d > refill_at:
                continue
            guard = f"refill_fired:{kind}:{cid}"
            if db.setting_get(guard) == month:   # one nudge per queue per month
                continue
            db.setting_set(guard, month)
            need = max(batch, target - d)
            notifications.notify(
                f"Top up the {co['name']} {meta['plural']} queue",
                f"{d} {meta['plural']} scheduled (target {target}). Time to ideate {need} more.",
                priority="normal", category="queue", company_id=cid,
                target_type="queue", target_id=kind, dedup_key=f"refill:{kind}:{cid}")
            fired.append({"company": co["name"], "kind": kind, "depth": d, "need": need})
    return fired
