"""Physio (owner, 14 Nov 2026): the fitness app's section for old injuries and niggles.

The owner ranked five: calves (deep tenderness), the right foot lift (weakness after a nerve injury), the right
shoulder twinge, the right knee (behind the joint, settling) and the feet and toes. Each issue is DATA
(fitness_physio.json -> setting `fitness_physio`, seeded once, never overwritten): what it is, the likely cause as
discussed (never a diagnosis), its tests, its drills (Smith's where the course has one, otherwise standard physio
practice, marked), red flags and questions for the physio.

- Tests are logged per day and side (`fitness.physio_results`); a re-log on the same day replaces it, a new day adds
  history. A test of kind `mobility` lives in the Mobility section (the knee-to-wall test in the Squat pillar, with
  auto-measure) and is READ from `fitness.mobility_results`, never copied.
- Sessions (`fitness.physio_sessions`) record which drills were done; the week's count is shown against the target.
- An issue can be marked resolved (setting `fitness_physio_status`); its history is kept.
- An active issue's `caution` is served to the Mobility section, which shows it above its pillars and sessions.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import date, timedelta

from psycopg.types.json import Json

from . import db

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fitness_physio.json")
KINDS = ("number", "scale", "yesno", "mobility")


def data() -> dict:
    cur = db.setting_get("fitness_physio")
    if not cur:
        cur = json.load(open(DATA_FILE, encoding="utf-8"))
        db.setting_set("fitness_physio", cur)
    return cur


def status() -> dict:
    s = db.setting_get("fitness_physio_status")
    return s if isinstance(s, dict) else {}


def _item(item_id: str) -> dict | None:
    return next((i for i in data().get("items") or [] if i["id"] == item_id), None)


def _test(item_id: str, test_id: str) -> dict | None:
    it = _item(item_id)
    return next((t for t in (it or {}).get("tests") or [] if t["id"] == test_id), None) if it else None


def save_result(day: str, item: str, test: str, side: str, value) -> dict:
    t = _test(item, test)
    if not t or t.get("kind") == "mobility":
        raise ValueError("unknown test")
    if side not in t["sides"]:
        raise ValueError("unknown side")
    if t["kind"] == "yesno":
        if value not in (0, 1, True, False, "0", "1"):
            raise ValueError("yes or no")
        v = 1 if value in (1, True, "1") else 0
    else:
        v = float(value)
        if t["kind"] == "scale" and not 0 <= v <= 10:
            raise ValueError("0 to 10")
        if v < 0 or v > 100000:
            raise ValueError("out of range")
    d = date.fromisoformat(day)
    if d > date.today():
        raise ValueError("future date")
    db.execute("update fitness.physio_results set deleted=true where day=%s and item=%s and test=%s and side=%s "
               "and not deleted", (d, item, test, side))
    uid = f"pr_{secrets.token_hex(6)}"
    db.execute("insert into fitness.physio_results (uid, day, item, test, side, value) values (%s,%s,%s,%s,%s,%s)",
               (uid, d, item, test, side, v))
    return {"ok": True, "id": uid}


def delete_result(uid: str) -> dict:
    db.execute("update fitness.physio_results set deleted=true where uid=%s", (uid,))
    return {"ok": True}


def log_session(day: str, drills: list, minutes: float | None = None, note: str = "") -> dict:
    known = {(i["id"], d["id"]) for i in data().get("items") or [] for d in i.get("drills") or []}
    clean = [{"item": str(x.get("item")), "drill": str(x.get("drill"))} for x in drills or []
             if (str(x.get("item")), str(x.get("drill"))) in known][:60]
    if not clean:
        raise ValueError("tick at least one drill")
    d = date.fromisoformat(day)
    if d > date.today():
        raise ValueError("future date")
    uid = f"ps_{secrets.token_hex(6)}"
    db.execute("insert into fitness.physio_sessions (uid, day, drills, minutes, note) values (%s,%s,%s,%s,%s)",
               (uid, d, Json(clean), minutes if minutes and minutes > 0 else None, (note or "")[:300] or None))
    return {"ok": True, "id": uid}


def delete_session(uid: str) -> dict:
    db.execute("update fitness.physio_sessions set deleted=true where uid=%s", (uid,))
    return {"ok": True}


def set_status(item: str, state: str) -> dict:
    if not _item(item) or state not in ("active", "resolved"):
        raise ValueError("unknown item or state")
    s = status()
    s[item] = {"status": state, "since": date.today().isoformat()}
    db.setting_set("fitness_physio_status", s)
    return {"ok": True}


def cautions() -> list:
    """The active issues' cautions, for the Mobility section."""
    st = status()
    return [{"item": i["id"], "text": i["caution"]} for i in sorted(data().get("items") or [], key=lambda x: x.get("rank", 99))
            if i.get("caution") and (st.get(i["id"]) or {}).get("status") != "resolved"]


def state() -> dict:
    d = data()
    res = [{"id": r["uid"], "date": r["day"].isoformat(), "item": r["item"], "test": r["test"], "side": r["side"],
            "value": float(r["value"]) if r["value"] is not None else None}
           for r in db.query("select * from fitness.physio_results where not deleted order by day, created_at")]
    # tests that live in Mobility are read from there (the knee-to-wall test has auto-measure)
    for it in d.get("items") or []:
        for t in it.get("tests") or []:
            if t.get("kind") != "mobility":
                continue
            for r in db.query("select day, side, measure, value from fitness.mobility_results where not deleted and "
                              "pillar=%s and test=%s order by day", (t["pillar"], t["test"])):
                res.append({"id": None, "date": r["day"].isoformat(), "item": it["id"], "test": t["id"],
                            "side": r["side"], "measure": r["measure"],
                            "value": float(r["value"]) if r["value"] is not None else None, "from": "mobility"})
    sess = [{"id": r["uid"], "date": r["day"].isoformat(), "drills": r["drills"],
             "minutes": float(r["minutes"]) if r["minutes"] is not None else None, "note": r["note"]}
            for r in db.query("select * from fitness.physio_sessions where not deleted order by day desc, created_at desc "
                              "limit 300")]
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    week = sum(1 for s in sess if s["date"] >= monday.isoformat())
    # weeks since the first log of each issue (the shoulder's scan question is asked after N weeks of logging)
    first = {}
    for r in res:
        if r["date"] < first.get(r["item"], "9999"):
            first[r["item"]] = r["date"]
    weeks = {k: (today - date.fromisoformat(v)).days // 7 for k, v in first.items()}
    return {"data": d, "status": status(), "results": res, "sessions": sess, "week_sessions": week,
            "week_start": monday.isoformat(), "weeks_logged": weeks, "today": today.isoformat()}
