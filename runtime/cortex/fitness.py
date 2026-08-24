"""Fitness — server-side home for the training log that used to live only in phone localStorage.

Sync model: the app pushes its whole document (it is small, a few hundred rows) and the server
explodes it into the fitness.* tables and keeps the raw document in fitness.snapshots. Whole-doc
push beats per-row merge here because there is one operator and one device, so there is no real
conflict to resolve, and a snapshot per push means a bad sync is always recoverable.

Rows are upserted by the client's own uid and never hard-deleted, so a push that is missing rows
(a half-restored phone, say) cannot silently wipe history. Deletion is explicit: deleted=true.

Derived columns (kg, volume_load, minutes, m_per_beat) are computed HERE so Cortex can query
training data with plain SQL. The maths mirrors the app exactly; keep the two in step.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from psycopg.types.json import Json

from . import db

# ---------- parsing helpers (ports of the app's own maths) ----------


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _day(v) -> date | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_duration_min(v) -> float | None:
    """'1:07:36' -> 67.6, '36:29' -> 36.48, '67.5' -> 67.5.

    Two-part values are mm:ss in this log, never hh:mm. Reading '36:29' as 36 hours was a real bug
    in the app once and it broke every duration chart.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if ":" in s:
        p = [_num(x) or 0 for x in s.split(":")]
        if len(p) >= 3:
            return p[0] * 60 + p[1] + p[2] / 60
        return p[0] + p[1] / 60
    return _num(s)


def bodyweight_at(day: date | None, log: list[dict]) -> float | None:
    """The bodyweight that applied ON the session date (latest entry at or before it).

    This is the whole point of a dated log: a 6 kg swing must never rewrite historic records.
    Sessions predating the log fall back to the earliest entry, matching the app.
    """
    if not day or not log:
        return None
    match = None
    for e in log:                                   # arrives sorted ascending
        if e["day"] <= day:
            match = e
        else:
            break
    return float((match or log[0])["kg"])


def lift_kg(weight, day: date | None, bw_log: list[dict]) -> float | None:
    """Kg on the bar: a logged number, or the dated bodyweight for a 'BW' lift."""
    raw = "" if weight is None else str(weight).strip()
    n = _num(re.sub(r"[^0-9.]", "", raw))
    if n and n > 0:
        return n
    if raw.lower() in ("bw", "bodyweight", "", "—"):
        return bodyweight_at(day, bw_log)
    return None


def volume_load(kg: float | None, total_reps) -> float | None:
    """The lifting PR metric: total reps x kg. Reps alone are not comparable across loads."""
    reps = _num(total_reps) or 0
    if kg is None or not reps:
        return None
    return round(kg * reps, 1)


# ---------- read ----------


def _bw_log() -> list[dict]:
    return db.query("select day, kg from fitness.bodyweight order by day")


def pull() -> dict:
    """Everything, in the shapes the app already uses, so the client needs no translation layer."""
    bw = [{"date": r["day"].isoformat(), "kg": float(r["kg"]), "notes": r["notes"]}
          for r in db.query("select day, kg, notes from fitness.bodyweight order by day")]
    plans = [{"id": r["uid"], "name": r["name"], "exercises": r["exercises"]}
             for r in db.query("select uid, name, exercises from fitness.plans "
                               "where not deleted order by name")]
    lifts = [{"id": r["uid"], "exercise": r["exercise"], "date": r["day"].isoformat(),
              "workoutId": r["plan_uid"], "weight": r["weight"], "sets": r["sets"],
              "totalReps": _num(r["total_reps"]), "bestSet": _num(r["best_set"]),
              "rest": r["rest"], "target": r["target"], "nextTarget": r["next_target"],
              "readings": r["readings"], "notes": r["notes"]}
             for r in db.query("select * from fitness.lift_sessions where not deleted "
                               "order by day, exercise")]
    presets = [{"id": r["uid"], "name": r["name"], "brand": r["brand"], "location": r["location"],
                "machine": r["machine"], "machineNote": r["machine_note"], "isHIIT": r["is_hiit"],
                "targetDuration": r["target_duration"], "manualFields": r["manual_fields"]}
               for r in db.query("select * from fitness.cardio_presets where not deleted order by name")]
    cardio = [{"id": r["uid"], "exerciseId": r["preset_uid"], "exerciseName": r["preset_name"],
               "date": r["day"].isoformat(), "duration": r["duration"], "avgHR": _num(r["avg_hr"]),
               "maxHR": _num(r["max_hr"]), "calories": _num(r["calories"]), "extra": r["extra"],
               "nextTarget": r["next_target"], "notes": r["notes"]}
              for r in db.query("select * from fitness.cardio_sessions where not deleted order by day")]
    vo2 = [{"id": r["uid"], "date": r["day"].isoformat(), "value": float(r["value"]),
            "method": r["method"], "notes": r["notes"]}
           for r in db.query("select * from fitness.vo2 where not deleted order by day")]
    return {"bodyweight": bw, "plans": plans, "liftSessions": lifts, "cardioPresets": presets,
            "cardioSessions": cardio, "vo2": vo2,
            "counts": {"bodyweight": len(bw), "plans": len(plans), "liftSessions": len(lifts),
                       "cardioPresets": len(presets), "cardioSessions": len(cardio), "vo2": len(vo2)}}


# ---------- write ----------


def _lift_uid(row: dict) -> str:
    """Stable id for rows that predate client ids: one exercise on one day is one session."""
    return str(row.get("id") or f"{row.get('exercise', '')}|{str(row.get('date'))[:10]}")


def push(doc: dict, source: str = "app") -> dict:
    """Upsert a whole client document. Additive by design: rows absent from the doc are left alone."""
    counts = {k: 0 for k in ("bodyweight", "plans", "liftSessions", "cardioPresets",
                             "cardioSessions", "vo2")}

    for r in doc.get("bodyweight") or []:
        d = _day(r.get("date") or r.get("day"))
        kg = _num(r.get("kg"))
        if not d or kg is None:
            continue
        db.execute("insert into fitness.bodyweight (day, kg, notes) values (%s,%s,%s) "
                   "on conflict (day) do update set kg=excluded.kg, notes=excluded.notes, "
                   "updated_at=now()", (d, kg, r.get("notes")))
        counts["bodyweight"] += 1

    for r in doc.get("plans") or doc.get("liftWorkouts") or []:
        if not r.get("id"):
            continue
        db.execute("insert into fitness.plans (uid, name, exercises, deleted) values (%s,%s,%s,%s) "
                   "on conflict (uid) do update set name=excluded.name, exercises=excluded.exercises, "
                   "deleted=excluded.deleted, updated_at=now()",
                   (r["id"], r.get("name") or "", Json(r.get("exercises") or []),
                    bool(r.get("deleted"))))
        counts["plans"] += 1

    bw_log = _bw_log()                              # read after the bodyweight upserts, so loads score now
    for r in doc.get("liftSessions") or []:
        d = _day(r.get("date"))
        if not d or not r.get("exercise"):
            continue
        kg = lift_kg(r.get("weight"), d, bw_log)
        db.execute(
            "insert into fitness.lift_sessions (uid, exercise, day, plan_uid, weight, kg, sets, "
            "total_reps, best_set, volume_load, rest, target, next_target, readings, notes, deleted) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set exercise=excluded.exercise, day=excluded.day, "
            "plan_uid=excluded.plan_uid, weight=excluded.weight, kg=excluded.kg, sets=excluded.sets, "
            "total_reps=excluded.total_reps, best_set=excluded.best_set, "
            "volume_load=excluded.volume_load, rest=excluded.rest, target=excluded.target, "
            "next_target=excluded.next_target, readings=excluded.readings, notes=excluded.notes, "
            "deleted=excluded.deleted, updated_at=now()",
            (_lift_uid(r), r["exercise"], d, r.get("workoutId"), r.get("weight"), kg,
             Json(r.get("sets") or []), _num(r.get("totalReps")), _num(r.get("bestSet")),
             volume_load(kg, r.get("totalReps")), r.get("rest"), r.get("target"),
             Json(r.get("nextTarget")) if r.get("nextTarget") is not None else None,
             Json(r.get("readings")) if r.get("readings") is not None else None,
             r.get("notes"), bool(r.get("deleted"))))
        counts["liftSessions"] += 1

    fields_by_preset = {}
    for r in doc.get("cardioPresets") or doc.get("cardioExercises") or []:
        if not r.get("id"):
            continue
        fields = r.get("manualFields") or []
        fields_by_preset[r["id"]] = fields
        db.execute(
            "insert into fitness.cardio_presets (uid, name, brand, location, machine, machine_note, "
            "is_hiit, target_duration, manual_fields, deleted) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set name=excluded.name, brand=excluded.brand, "
            "location=excluded.location, machine=excluded.machine, machine_note=excluded.machine_note, "
            "is_hiit=excluded.is_hiit, target_duration=excluded.target_duration, "
            "manual_fields=excluded.manual_fields, deleted=excluded.deleted, updated_at=now()",
            (r["id"], r.get("name") or "", r.get("brand"), r.get("location"), r.get("machine"),
             r.get("machineNote"), bool(r.get("isHIIT")), r.get("targetDuration"),
             Json(fields), bool(r.get("deleted"))))
        counts["cardioPresets"] += 1

    for r in doc.get("cardioSessions") or []:
        d = _day(r.get("date"))
        if not d or not r.get("id"):
            continue
        mins = parse_duration_min(r.get("duration"))
        extra = r.get("extra") or {}
        # extra{} is keyed by the preset's manualFields INDEX, so a distance value only has meaning
        # alongside the preset that names the labels.
        fields = fields_by_preset.get(r.get("exerciseId"))
        if fields is None:
            row = db.one("select manual_fields from fitness.cardio_presets where uid=%s",
                         (r.get("exerciseId"),))
            fields = (row or {}).get("manual_fields") or []
        dist = None
        for i, f in enumerate(fields):
            if re.search(r"distance", str(f.get("label", "")), re.I):
                dist = _num(extra.get(str(i)))
        avg_hr = _num(r.get("avgHR"))
        # m/beat = metres per heartbeat, the aerobic efficiency metric.
        mpb = round(dist * 1000 / (avg_hr * mins), 3) if (dist and avg_hr and mins) else None
        db.execute(
            "insert into fitness.cardio_sessions (uid, preset_uid, preset_name, day, duration, "
            "minutes, avg_hr, max_hr, calories, distance_km, m_per_beat, extra, next_target, notes, "
            "deleted) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (uid) do update set preset_uid=excluded.preset_uid, "
            "preset_name=excluded.preset_name, day=excluded.day, duration=excluded.duration, "
            "minutes=excluded.minutes, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, "
            "calories=excluded.calories, distance_km=excluded.distance_km, "
            "m_per_beat=excluded.m_per_beat, extra=excluded.extra, next_target=excluded.next_target, "
            "notes=excluded.notes, deleted=excluded.deleted, updated_at=now()",
            (r["id"], r.get("exerciseId"), r.get("exerciseName"), d, r.get("duration"), mins,
             avg_hr, _num(r.get("maxHR")), _num(r.get("calories")), dist, mpb, Json(extra),
             Json(r.get("nextTarget")) if r.get("nextTarget") is not None else None,
             r.get("notes"), bool(r.get("deleted"))))
        counts["cardioSessions"] += 1

    for r in doc.get("vo2") or doc.get("vo2Records") or []:
        d = _day(r.get("date"))
        val = _num(r.get("value") if r.get("value") is not None else r.get("vo2"))
        if not d or val is None:
            continue
        db.execute("insert into fitness.vo2 (uid, day, value, method, notes) values (%s,%s,%s,%s,%s) "
                   "on conflict (uid) do update set day=excluded.day, value=excluded.value, "
                   "method=excluded.method, notes=excluded.notes, updated_at=now()",
                   (str(r.get("id") or f"vo2|{d.isoformat()}"), d, val, r.get("method"),
                    r.get("notes")))
        counts["vo2"] += 1

    total = sum(counts.values())
    db.execute("insert into fitness.snapshots (source, rows, doc) values (%s,%s,%s)",
               (source, total, Json(doc)))
    return {"ok": True, "written": counts, "total": total}


def rescore_bodyweight_lifts() -> int:
    """Recompute kg/volume_load for every BW lift after the bodyweight log changes.

    Logging a weight for a past date retro-scores the sessions it covers, which is the entire point
    of the dated log. Only rows whose logged weight is not a number are touched.
    """
    bw_log = _bw_log()
    rows = db.query("select uid, day, weight, total_reps from fitness.lift_sessions where not deleted")
    n = 0
    for r in rows:
        raw = "" if r["weight"] is None else str(r["weight"]).strip()
        if _num(re.sub(r"[^0-9.]", "", raw)):
            continue                                # numeric load, nothing to resolve
        kg = lift_kg(r["weight"], r["day"], bw_log)
        db.execute("update fitness.lift_sessions set kg=%s, volume_load=%s, updated_at=now() "
                   "where uid=%s", (kg, volume_load(kg, r["total_reps"]), r["uid"]))
        n += 1
    return n


def summary() -> dict:
    """Compact training picture for Cortex: recent volume, streak, PRs, aerobic drift."""
    return {
        "last_lift": db.one("select day, exercise, volume_load from fitness.lift_sessions "
                            "where not deleted order by day desc limit 1"),
        "last_cardio": db.one("select day, preset_name, avg_hr, minutes from fitness.cardio_sessions "
                              "where not deleted order by day desc limit 1"),
        "sessions_28d": db.one("select count(distinct day) as days from fitness.lift_sessions "
                               "where not deleted and day > current_date - 28")or {},
        "bodyweight": db.one("select day, kg from fitness.bodyweight order by day desc limit 1"),
        "unscored_bw_lifts": (db.one("select count(*) as n from fitness.lift_sessions "
                                     "where not deleted and kg is null and total_reps > 0") or {}).get("n", 0),
    }


# ---------- screenshot scanning ----------

SCAN_SYSTEM = ("You read fitness machine and health-app screenshots and return the numbers on them. "
               "Report only what is visibly printed. Never estimate, infer or fill a gap: a field you "
               "cannot read is null. Getting a session's real numbers wrong is worse than leaving them "
               "blank for the operator to type.")

SCAN_PROMPT = """Extract the workout data from this screenshot (Samsung Health, a treadmill or a
cross-trainer console). Return ONLY this JSON, null for anything not clearly visible:
{"duration":"MM:SS or HH:MM:SS","avgHR":number,"maxHR":number,"calories":number,
 "distance":number,"activityType":"Treadmill|Elliptical|Running|Cycling|other"}
distance is in kilometres. Do not convert or round anything else."""


def scan_screenshot(data_url: str) -> dict:
    """Read a workout screenshot into form fields.

    Runs on the box with the Cortex API key, so no key is ever stored on the phone. Haiku: this is
    mechanical extraction from a clear screen, not reasoning, and it runs every logged session.
    """
    from . import provider
    out = provider.think_json(SCAN_SYSTEM, SCAN_PROMPT, model=provider.MODEL_ROUTER,
                              max_tokens=400, purpose="fitness_scan", images=[data_url])
    return {k: out.get(k) for k in ("duration", "avgHR", "maxHR", "calories", "distance",
                                    "activityType")}
