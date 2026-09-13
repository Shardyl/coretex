"""
golf.py - Rashad's personal golf tee booking (personal company 29, skill 'golf-booking').

The box drives the real Play Store Viya app on his phone over adb (integrations/android.py) at the moment
a new booking day opens. Everything behavioural (courses, fallback order, players, days ahead, release
time, phone address) is read from the skill craft's single `PLAN = {...}` line, never hardcoded here.
Nothing books without an approved card:

    python -m cortex.golf card 2026-10-05   -> 'golf_booking' approval card (awaiting_approval)
    (owner approves)                        -> engine._execute marks it 'armed'; nothing books yet
    python -m cortex.golf run <task_id>     -> timed runner: pre-check, wait for release, book, report

The runner is launched by a one-off systemd timer a few minutes before release, because the engine's
60-second tick cannot fire on the second.
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import notifications, store
from .integrations import android

COMPANY_ID = 29            # the personal company (kind='personal')
SKILL_KEY = "golf-booking"
PKG = "com.wasl.viya.app"
EVIDENCE = "/var/tmp/cortex-golf"


def _skill() -> dict:
    sk = store.get_skill_by_key(COMPANY_ID, SKILL_KEY)
    if not sk:
        raise RuntimeError("golf-booking skill is missing from the personal company")
    return sk


def plan() -> dict:
    m = re.search(r"^PLAN\s*=\s*(\{.*\})\s*$", _skill().get("craft") or "", re.M)
    if not m:
        raise RuntimeError("the golf-booking craft has no `PLAN = {...}` line")
    return json.loads(m.group(1))


def release_at(day: date, p: dict) -> datetime:
    tz = ZoneInfo(p["tz"])
    hh, mm = map(int, p["release_local"].split(":"))
    opens = day - timedelta(days=int(p["days_ahead"]))
    return datetime(opens.year, opens.month, opens.day, hh, mm, tzinfo=tz)


def _attempt_line(a: dict) -> str:
    return f"{a['course']} {a['exact']}" if a.get("exact") else f"{a['course']}, next slot up to {a['latest']}"


def post_card(day_str: str) -> dict:
    p, sk = plan(), _skill()
    day = date.fromisoformat(day_str)
    rel = release_at(day, p)
    local = rel.astimezone(ZoneInfo(p["tz"]))
    req = {"date": day_str, "release_at": rel.isoformat(), "plan": p}
    t = store.create_task(COMPANY_ID, sk["id"], "golf_booking", req)
    who = ", ".join(["you"] + p["players"])
    order = "\n".join(f"{i + 1}. {_attempt_line(a)}" for i, a in enumerate(p["attempts"]))
    body = (f"Book {day:%a %-d %b} for {1 + len(p['players'])} players: {who}.\n\n"
            f"Fires when the day opens, expected {local:%a %-d %b %H:%M} Dubai time "
            f"({'measured' if p.get('release_confirmed') else 'not yet confirmed, the run measures it'}).\n\n"
            f"Order:\n{order}\n\nIf none of these is free, nothing is booked. The result comes to Telegram.")
    title = f"Golf: book {day:%a %-d %b}, {_attempt_line(p['attempts'][0])} first"
    store.update_task(t["id"], title=title, draft=body, status="awaiting_approval")
    notifications.notify(title, "Approve before release to arm the booking.", priority="normal",
                         category="approval", company_id=COMPANY_ID, target_type="task", target_id=t["id"])
    return store.get_task(t["id"])


# ---------------------------------------------------------------- runner

class Run:
    def __init__(self, task: dict):
        self.task = task
        self.req = task.get("request") or {}
        self.p = self.req["plan"]
        self.day = date.fromisoformat(self.req["date"])
        self.rel = datetime.fromisoformat(self.req["release_at"])
        self.ph = android.Phone(self.p["phone"])
        self.dir = os.path.join(EVIDENCE, str(task["id"]))
        os.makedirs(self.dir, exist_ok=True)
        self.log_lines: list[str] = []
        self.n = 0

    def log(self, msg: str):
        line = f"{datetime.now(timezone.utc).astimezone(ZoneInfo(self.p['tz'])):%H:%M:%S.%f}"[:-3] + " " + msg
        self.log_lines.append(line)
        print(line, flush=True)

    def shot(self, tag: str):
        """Saved ON the phone (fast); pulled to self.dir after the run (a PNG across Dubai<->US takes 20s+)."""
        self.n += 1
        try:
            self.ph.snap(f"{self.n:02d}-{tag}")
        except Exception as e:  # noqa: BLE001 - evidence must never break the run
            self.log(f"screenshot failed: {e}")

    def pull_evidence(self):
        try:
            self.ph.pull_snaps(self.dir)
        except Exception as e:  # noqa: BLE001
            self.log(f"could not pull screenshots: {e}")

    def precheck(self) -> str | None:
        if not self.ph.connect():
            return f"phone not reachable over adb at {self.p['phone']} (Tailscale off, wireless debugging off?)"
        self.ph.wake()
        if self.ph.locked():
            return "phone is locked (Stay awake off, or it locked before charging)"
        self.ph.launch(PKG)
        time.sleep(3)
        if self.ph.foreground() != PKG:
            return f"Viya did not come to the front (foreground: {self.ph.foreground() or 'unknown'})"
        self.shot("precheck")
        return None

    # The Viya-specific screen steps live in viya_flow (filled from the live screen map).
    def book(self) -> dict:
        from . import viya_flow
        return viya_flow.book(self)


def _finish(task_id: int, ok: bool, summary: str, r: "Run | None"):
    if r:
        r.pull_evidence()
    tail =("\n\nRun log:\n" + "\n".join(r.log_lines[-60:])) if r else ""
    evid = f"\n\nScreenshots: {r.dir}" if r else ""
    store.update_task(task_id, status="done" if ok else "failed", last_status=summary[:300],
                      draft=((store.get_task(task_id) or {}).get("draft") or "") + "\n\nRESULT: " + summary + evid + tail)
    notifications.notify(("Golf booked: " if ok else "Golf booking failed: ") + summary[:120], summary,
                         priority="critical", category="system", company_id=COMPANY_ID,
                         target_type="task", target_id=task_id, dedup_key=f"golf-result-{task_id}")


def run(task_id: int):
    t = store.get_task(task_id)
    if not t or t.get("kind") != "golf_booking":
        sys.exit(f"#{task_id} is not a golf_booking card")
    if t["status"] != "armed":
        notifications.notify(f"Golf booking #{task_id} did not run", f"The card is '{t['status']}', not approved.",
                             priority="critical", category="system", company_id=COMPANY_ID,
                             target_type="task", target_id=task_id)
        return
    r = Run(t)
    store.update_task(task_id, status="sending")
    r.log(f"runner up for {r.day:%a %-d %b}, release expected {r.rel.astimezone(ZoneInfo(r.p['tz'])):%H:%M:%S}")
    alerted = False
    while True:   # pre-check, retrying every 30s until 60s before release
        err = r.precheck()
        if not err:
            r.log("pre-check ok")
            break
        r.log(f"pre-check failed: {err}")
        if datetime.now(timezone.utc) >= r.rel - timedelta(seconds=60):
            return _finish(task_id, False, f"pre-check never passed: {err}", r)
        if not alerted:
            notifications.notify("Golf pre-check failed, retrying", err, priority="critical", category="system",
                                 company_id=COMPANY_ID, target_type="task", target_id=task_id)
            alerted = True
        time.sleep(30)
    try:
        res = r.book()
    except Exception as e:  # noqa: BLE001 - any surprise is reported, never silent
        r.log(f"error: {type(e).__name__}: {e}")
        r.shot("error")
        return _finish(task_id, False, f"stopped on an unexpected screen ({type(e).__name__}: {str(e)[:150]})", r)
    return _finish(task_id, bool(res.get("booked")), res.get("summary", ""), r)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "card":
        c = post_card(sys.argv[2])
        print(c["id"], c["title"])
    elif cmd == "run":
        run(int(sys.argv[2]))
    elif cmd == "plan":
        p = plan()
        print(json.dumps(p, indent=1))
        if len(sys.argv) > 2:
            print("release:", release_at(date.fromisoformat(sys.argv[2]), p).isoformat())
    else:
        sys.exit("usage: python -m cortex.golf card YYYY-MM-DD | run TASK_ID | plan [YYYY-MM-DD]")
