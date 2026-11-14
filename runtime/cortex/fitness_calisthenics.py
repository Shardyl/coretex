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
    return {"data": d, "programs": progs, "current": cur, "next": nxt, "sessions": sess, "milestones": ms,
            "wrist": db.setting_get("fitness_cali_wrist") or []}
