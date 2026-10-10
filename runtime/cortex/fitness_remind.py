"""Fitness app reminders (owner, 10 Oct 2026), pushed ONLY to the Fitness PWA's own subscription (push app='fitness'),
so they arrive branded as the Fitness app, never as Cortex.

  morning (05:30 Dubai, every day): weigh-in + creatine; on Sundays also waist + progress photo. Only what is not done.
  evening (19:00 Dubai): creatine, if still not logged.

Run by cortex-fitness-remind.timer: `python -m cortex.fitness_remind morning|evening`.
"""
from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db, push

TZ = ZoneInfo("Asia/Dubai")


def pending(today) -> dict:
    weighed = db.one("select 1 from fitness.bodyweight where day=%s", (today,)) is not None
    creatine = db.one("select 1 from fitness.supplements where day=%s and name='creatine' and not deleted",
                      (today,)) is not None
    waist = db.one("select 1 from fitness.readings where kind='waist' and not deleted and "
                   "(at at time zone 'Asia/Dubai')::date > %s::date - 7", (today,)) is not None
    photo = db.one("select 1 from fitness.progress_photos where not deleted and day > %s::date - 7",
                   (today,)) is not None
    return {"weigh-in": not weighed, "creatine": not creatine, "waist": not waist, "progress photo": not photo}


def run(slot: str) -> dict:
    now = datetime.now(TZ)
    today = now.date()
    p = pending(today)
    if slot == "evening":
        todo = ["creatine"] if p["creatine"] else []
        title, tag = "Fitness: creatine not logged", f"fit-ev-{today}"
    else:
        todo = [k for k in ("weigh-in", "creatine") if p[k]]
        if now.weekday() == 6:                                     # Sunday check-in
            todo += [k for k in ("waist", "progress photo") if p[k]]
        title = "Fitness: Sunday check-in" if now.weekday() == 6 else "Fitness: morning check-in"
        tag = f"fit-am-{today}"
    if not todo:
        return {"sent": 0, "todo": []}
    body = ", ".join(todo).capitalize() + (" still to do." if slot == "evening" else " to do today.")
    return {"sent": push.send_to_app("fitness", title, body, tag, url="/"), "todo": todo}


if __name__ == "__main__":
    print(run(sys.argv[1] if len(sys.argv) > 1 else "morning"))
