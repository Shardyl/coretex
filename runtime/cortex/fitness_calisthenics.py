"""Calisthenics (owner, 14 Nov 2026): its own section of the fitness app, separate from Mobility (owner: "mobility is
more about flexibility, and calisthenics is different").

Starts with Matthew Smith's Handstand Toolkit, which he bought: the program sheets (Starter 1.0 to the one arm prep
programs), the skill tree's milestones and the wrist warm-up library are DATA (fitness_handstand.json -> setting
`fitness_cali_data`, seeded once), transcribed from his sheets; numbers are his, never invented. Lessons play in the
app from the private copies (course "handstand").

- The CURRENT program is his choice (setting `fitness_cali_current`), defaulting to the first Starter program. When the
  program's move-on milestone is logged, the app offers the next one; it never moves him on by itself.
- A session logs, per exercise, the sets done (hold seconds or reps) and a note. Handstands are non-linear (Smith), so
  progress is judged by milestones, not session to session; the app shows last time and best, nothing more clever.
- His wrist warm-up is his own pick from the library (setting `fitness_cali_wrist`), shown as slot A of every session.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import date

from psycopg.types.json import Json

from . import db

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fitness_handstand.json")


def data() -> dict:
    cur = db.setting_get("fitness_cali_data")
    if not cur:
        cur = json.load(open(DATA_FILE, encoding="utf-8"))
        db.setting_set("fitness_cali_data", cur)
    return cur


def programs(d: dict | None = None) -> list:
    d = d or data()
    out = []
    for st in sorted(d.get("stages") or [], key=lambda s: s.get("order", 99)):
        for p in st.get("programs") or []:
            out.append({**p, "stage": st["id"], "stage_name": st.get("name")})
    return out


def current_id() -> str | None:
    cur = db.setting_get("fitness_cali_current")
    ids = [p["id"] for p in programs()]
    if isinstance(cur, dict) and cur.get("program") in ids:
        return cur["program"]
    return ids[0] if ids else None


def set_current(program_id: str) -> dict:
    if program_id not in [p["id"] for p in programs()]:
        raise ValueError("unknown program")
    db.setting_set("fitness_cali_current", {"program": program_id, "since": date.today().isoformat()})
    return {"ok": True}


def set_wrist(ids: list) -> dict:
    lib = {w["id"] for w in data().get("wrist_library") or []}
    db.setting_set("fitness_cali_wrist", [i for i in ids if i in lib][:20])
    return {"ok": True}


def log_session(program_id: str, day: str, items: list, note: str = "") -> dict:
    if program_id not in [p["id"] for p in programs()]:
        raise ValueError("unknown program")
    clean = []
    for it in items or []:
        sets = []
        for s in (it.get("sets") or [])[:15]:
            row = {k: float(s[k]) for k in ("reps", "hold_s") if s.get(k) not in (None, "")}
            if row:
                sets.append(row)
        if not sets and not it.get("note"):
            continue
        clean.append({"code": str(it.get("code", ""))[:4], "exercise_id": str(it.get("exercise_id", ""))[:60],
                      "sets": sets, "note": str(it.get("note") or "")[:300]})
    if not clean:
        raise ValueError("nothing logged")
    uid = f"cs_{secrets.token_hex(6)}"
    db.execute("insert into fitness.cali_sessions (uid, day, program_id, items, note) values (%s,%s,%s,%s,%s)",
               (uid, date.fromisoformat(day), program_id, Json(clean), (note or "")[:300] or None))
    return {"ok": True, "id": uid}


def delete_session(uid: str) -> dict:
    db.execute("update fitness.cali_sessions set deleted=true where uid=%s", (uid,))
    return {"ok": True}


def log_milestone(milestone_id: str, day: str, value: float | None = None, note: str = "") -> dict:
    if milestone_id not in [m["id"] for m in data().get("milestones") or []]:
        raise ValueError("unknown milestone")
    uid = f"cm_{secrets.token_hex(6)}"
    db.execute("insert into fitness.cali_milestones (uid, milestone_id, day, value, note) values (%s,%s,%s,%s,%s)",
               (uid, milestone_id, date.fromisoformat(day), value, (note or "")[:300] or None))
    return {"ok": True, "id": uid}


def delete_milestone(uid: str) -> dict:
    db.execute("update fitness.cali_milestones set deleted=true where uid=%s", (uid,))
    return {"ok": True}


# ---------- Skill ladders (owner, 15 Nov 2026) ----------
# Five classic skills (HSPU, L-sit to press handstand, planche, front lever, muscle-up), each a ladder of rungs with
# PASS MARKS. The ladders, pass marks, drills, warm-ups and the draft week are DATA (fitness_cali_goals.json ->
# setting `fitness_cali_goals`, seeded once, never overwritten), ours from the 15 Nov chat except a pass mark carrying
# `smith`, which is his milestone number. A rung is PASSED only when every pass mark is logged at or above its number,
# on every side it needs, with "all sets" ticked where the mark has sets, and FILMED (owner: form first, checked on
# video). The current rung = the lowest rung not passed; logging pass tests IS the placement test.
GOALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fitness_cali_goals.json")
LADDER_PREFIX = "ladder:"
ROUTINE_PREFIX = "routine:"


def goals() -> dict:
    cur = db.setting_get("fitness_cali_goals")
    if not cur:
        cur = json.load(open(GOALS_FILE, encoding="utf-8"))
        db.setting_set("fitness_cali_goals", cur)
    return cur


def _sides(p: dict) -> list:
    return ["L", "R"] if p.get("sides") == "each" else ["both"]


def rung_passed(rung: dict, tests: list, skill: str) -> bool:
    """Every pass mark met on every side it needs, all sets where it has sets, and filmed."""
    for p in rung.get("pass") or []:
        for sd in _sides(p):
            ok = any(t["skill"] == skill and t["test"] == p["id"] and t["side"] == sd and t["filmed"]
                     and t["value"] is not None and t["value"] >= float(p["value"])
                     and (int(p.get("sets") or 1) <= 1 or t["all_sets"]) for t in tests)
            if not ok:
                return False
    return True


def ladders(tests: list | None = None) -> dict:
    """{skill: {current, passed: [n...], total}}: current = the lowest rung not passed (None = the goal is done)."""
    g = goals()
    tests = goal_tests() if tests is None else tests
    out = {}
    for s in g.get("skills") or []:
        rungs = sorted(s.get("rungs") or [], key=lambda r: r["n"])
        passed = [r["n"] for r in rungs if rung_passed(r, tests, s["id"])]
        cur = next((r["n"] for r in rungs if r["n"] not in passed), None)
        out[s["id"]] = {"current": cur, "passed": passed, "total": len(rungs)}
    return out


def goal_tests() -> list:
    return [{"id": r["uid"], "date": r["day"].isoformat(), "skill": r["skill"], "rung": r["rung"], "test": r["test"],
             "side": r["side"], "value": float(r["value"]) if r["value"] is not None else None,
             "all_sets": bool(r["all_sets"]), "filmed": bool(r["filmed"]), "note": r["note"]}
            for r in db.query("select * from fitness.cali_goal_tests where not deleted order by day, created_at")]


def log_goal_test(skill: str, rung: int, test: str, side: str, value: float, day: str, all_sets: bool = False,
                  filmed: bool = False, note: str = "") -> dict:
    s = next((x for x in goals().get("skills") or [] if x["id"] == skill), None)
    r = next((x for x in (s or {}).get("rungs") or [] if x["n"] == int(rung)), None)
    p = next((x for x in (r or {}).get("pass") or [] if x["id"] == test), None)
    if not p:
        raise ValueError("unknown pass mark")
    if side not in _sides(p):
        raise ValueError("wrong side for this pass mark")
    if value is None or float(value) < 0:
        raise ValueError("a value is needed")
    uid = f"cg_{secrets.token_hex(6)}"
    db.execute("insert into fitness.cali_goal_tests (uid, day, skill, rung, test, side, value, all_sets, filmed, note) "
               "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (uid, date.fromisoformat(day), skill, int(rung), test, side, float(value), bool(all_sets), bool(filmed),
                (note or "")[:300] or None))
    return {"ok": True, "id": uid}


def delete_goal_test(uid: str) -> dict:
    db.execute("update fitness.cali_goal_tests set deleted=true where uid=%s", (uid,))
    return {"ok": True}


def log_ladder_session(week_day: str, day: str, items: list, note: str = "", uid: str | None = None) -> dict:
    """A session from the draft week (warm-ups ticked, each skill's current-rung drills, extras), stored with the
    Smith sessions under program id `ladder:<day name>`, so it never shows as a Handstand Toolkit session."""
    # A ROUTINE (owner, 19 Nov 2026: "Full body calisthenics") logs as program `routine:<id>`, not `ladder:`.
    routine = week_day.startswith(ROUTINE_PREFIX)
    if routine:
        if week_day[len(ROUTINE_PREFIX):] not in [r["id"] for r in goals().get("routines") or []]:
            raise ValueError("unknown routine")
    elif week_day not in [w["day"] for w in goals().get("week") or []]:
        raise ValueError("unknown training day")
    clean = []
    for it in items or []:
        sets = []
        for s in (it.get("sets") or [])[:15]:
            row = {k: float(s[k]) for k in ("reps", "hold_s") if s.get(k) not in (None, "")}
            if row:
                sets.append(row)
        if not sets and not it.get("note"):
            continue
        clean.append({"code": str(it.get("code", ""))[:12], "exercise_id": str(it.get("exercise_id", ""))[:80],
                      "sets": sets, "note": str(it.get("note") or "")[:300]})
    if not clean:
        raise ValueError("nothing logged")
    program = week_day if routine else LADDER_PREFIX + week_day
    # SAVED AS YOU GO (owner, 22 Nov 2026: "I might close the app and lose the data"): each exercise's Save sends the
    # whole session so far with the uid the first save returned, and the same row is updated, so one workout stays one
    # session. A uid that is not this program's live session is ignored and a new row is made.
    if uid and db.one("select 1 from fitness.cali_sessions where uid=%s and program_id=%s and not deleted",
                      (uid, program)):
        db.execute("update fitness.cali_sessions set day=%s, items=%s, note=%s where uid=%s",
                   (date.fromisoformat(day), Json(clean), (note or "")[:300] or None, uid))
        return {"ok": True, "id": uid, "updated": True}
    uid = f"cs_{secrets.token_hex(6)}"
    db.execute("insert into fitness.cali_sessions (uid, day, program_id, items, note) values (%s,%s,%s,%s,%s)",
               (uid, date.fromisoformat(day), program, Json(clean), (note or "")[:300] or None))
    return {"ok": True, "id": uid}


def state() -> dict:
    d = data()
    progs = programs(d)
    cur = current_id()
    sess = [{"id": r["uid"], "date": r["day"].isoformat(), "program": r["program_id"], "items": r["items"],
             "note": r["note"]}
            for r in db.query("select * from fitness.cali_sessions where not deleted order by day desc, created_at desc "
                              "limit 200")]
    ms = [{"id": r["uid"], "milestone": r["milestone_id"], "date": r["day"].isoformat(),
           "value": float(r["value"]) if r["value"] is not None else None, "note": r["note"]}
          for r in db.query("select * from fitness.cali_milestones where not deleted order by day")]
    done = {m["milestone"] for m in ms}
    p = next((x for x in progs if x["id"] == cur), None)
    nxt = None
    if p and (p.get("move_on") or {}).get("milestone_id") in done:
        i = [x["id"] for x in progs].index(cur)
        nxt = progs[i + 1]["id"] if i + 1 < len(progs) else None
    tests = goal_tests()
    # owner, 22 Nov 2026 ("wipe my current data"): a session deleted on the server takes the phone's draft of it too
    gone = [r["uid"] for r in db.query("select uid from fitness.cali_sessions where deleted "
                                       "and day >= current_date - 7")]
    return {"data": d, "programs": progs, "current": cur, "next": nxt, "sessions": sess, "milestones": ms,
            "deleted_sessions": gone,
            "wrist": db.setting_get("fitness_cali_wrist") or [],
            "goals": goals(), "goal_tests": tests, "ladders": ladders(tests)}
